from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from cs2_assistant.accounts import AccountStore
from cs2_assistant.config import PROJECT_ROOT, Settings
from cs2_assistant.services.c5_case_sweeper import C5CaseSweeper
from cs2_assistant.services.c5_catalog_taxonomy import (
    build_c5_catalog_taxonomy,
    estimate_c5_catalog_filter,
)
from cs2_assistant.services.c5_research_scan import (
    list_c5_research_results,
)
from cs2_assistant.services.case_monitor_runtime import (
    CaseMonitorBusyError,
    CaseMonitorRuntimeController,
)
from cs2_assistant.services.guadao_logging import get_guadao_event_logger
from cs2_assistant.services.guadao_audit import (
    export_guadao_audit,
    list_guadao_audit_rows,
)
from cs2_assistant.services.profit_trade_logging import get_profit_trade_event_logger
from cs2_assistant.services.public_payload import sanitize_public_payload
from cs2_assistant.services.runtime_controller import UnifiedRuntimeController
from cs2_assistant.services.steam_balances import (
    load_steam_account_balances,
    refresh_steam_account_balances,
)
from cs2_assistant.services.steam_request_scheduler import (
    DEFAULT_ACCOUNT_ROUTE_COOLDOWN_SECONDS,
    DEFAULT_GLOBAL_COOLDOWN_SECONDS,
    DEGRADED_GLOBAL_PROBE_SECONDS,
    GLOBAL_DEGRADED_AFTER_SECONDS,
)
from cs2_assistant.services.profit_trade import (
    build_profit_trade_completed_payload,
    build_profit_trade_dashboard_payload,
    build_profit_trade_interruption_timeline_payload,
    build_profit_trade_interruptions_payload,
    build_profit_trade_roi_history_payload,
    build_profit_trade_roi_watch_payload,
    build_profit_trade_selection_history_payload,
    build_profit_trade_selection_watch_payload,
    create_manual_profit_trade_record,
    dismiss_profit_trade,
    execute_profit_trade_buy,
    execute_profit_trade_list_c5,
    lock_profit_trade,
    manual_settle_profit_trade,
    refresh_profit_trade_sales,
    run_profit_trade_once,
    scan_profit_trade_opportunities,
    search_profit_trade_catalog_items,
    send_profit_trade_daily_report,
    set_profit_trade_config,
    set_profit_trade_interruption_acknowledged,
    update_profit_trade_protection,
    update_manual_profit_trade_record,
    update_profit_trade_selection_watch,
)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    return str((query.get(key) or [default])[0]).strip()


def _query_int(
    query: dict[str, list[str]],
    key: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 500,
) -> int:
    raw = _query_value(query, key)
    value = int(raw) if raw else int(default)
    return max(minimum, min(value, maximum))


def _query_int_strict(
    query: dict[str, list[str]],
    key: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 500,
) -> int:
    """Parse a bounded integer without silently changing invalid client input."""

    raw = _query_value(query, key)
    value = int(raw) if raw else int(default)
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _profit_trade_log_filters(query: dict[str, list[str]]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    mappings = {
        "from": "from",
        "to": "to",
        "level": "level",
        "provider": "provider",
        "component": "component",
        "operation": "operation",
        "steamId": "steamId",
        "accountId": "accountId",
        "tradeNo": "tradeNo",
        "requestId": "requestId",
        "marketHashName": "marketHashName",
        "keyword": "keyword",
        "cursor": "cursor",
    }
    for query_key, filter_key in mappings.items():
        value = _query_value(query, query_key)
        if value:
            filters[filter_key] = value
    page_size = _query_value(query, "pageSize") or _query_value(query, "limit")
    if page_size:
        filters["pageSize"] = max(1, min(int(page_size), 1000))
    return filters


def _guadao_log_filters(query: dict[str, list[str]]) -> dict[str, Any]:
    """Translate the S4 user-facing query names to the shared logger contract."""

    filters = _profit_trade_log_filters(query)
    aliases = {
        "startAt": "from",
        "endAt": "to",
        "service": "component",
        "account": "accountId",
        "q": "keyword",
    }
    for query_key, filter_key in aliases.items():
        value = _query_value(query, query_key)
        if value:
            filters[filter_key] = value
    market_hash_name = _query_value(query, "marketHashName")
    if market_hash_name:
        filters["marketHashName"] = market_hash_name
    operation_id = _query_value(query, "operationId")
    if operation_id:
        filters["operationId"] = operation_id
    http_status = _query_value(query, "httpStatus")
    if http_status:
        filters["httpStatus"] = http_status
    return filters


def _guadao_settings_payload(
    runtime: UnifiedRuntimeController,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add the immutable shared-Steam policy used by this API process.

    The runtime configures the scheduler with its production defaults.  Expose
    those real defaults instead of presenting editable per-priority intervals
    that do not exist in the scheduler.
    """

    payload = dict(payload if payload is not None else runtime.settings_payload())
    settings = dict(payload.get("settings") or {})
    scheduler = dict(settings.get("steamScheduler") or {})
    scheduler.update(
        {
            "mode": "single_channel",
            "accountRouteCooldownSeconds": DEFAULT_ACCOUNT_ROUTE_COOLDOWN_SECONDS,
            "globalCooldownSeconds": DEFAULT_GLOBAL_COOLDOWN_SECONDS,
            "degradedAfterSeconds": GLOBAL_DEGRADED_AFTER_SECONDS,
            "degradedProbeSeconds": DEGRADED_GLOBAL_PROBE_SECONDS,
            "quietWindowEnabled": True,
        }
    )
    settings["steamScheduler"] = scheduler
    payload["settings"] = settings
    return payload


def _profit_trade_log_event_matches(event: dict[str, Any], filters: dict[str, Any]) -> bool:
    exact_fields = {
        "level": "level",
        "provider": "provider",
        "component": "component",
        "operation": "operation",
        "steamId": "steam_id64",
        "accountId": "account_id",
        "tradeNo": "trade_no",
        "requestId": "request_id",
        "marketHashName": "market_hash_name",
    }
    for filter_key, event_key in exact_fields.items():
        expected = str(filters.get(filter_key) or "").strip()
        if not expected:
            continue
        actual = str(event.get(event_key) or "").strip()
        if filter_key in {"level", "provider", "component", "operation"}:
            if actual.lower() != expected.lower():
                return False
        elif actual != expected:
            return False
    keyword = str(filters.get("keyword") or "").strip().lower()
    if keyword and keyword not in json.dumps(event, ensure_ascii=False).lower():
        return False
    return True


def _public_guadao_log_matches(
    event: dict[str, Any],
    query: dict[str, list[str]],
) -> bool:
    exact = {
        "level": "level",
        "service": "service",
        "operation": "operation",
    }
    for query_key, event_key in exact.items():
        expected = _query_value(query, query_key).lower()
        if expected and str(event.get(event_key) or "").lower() != expected:
            return False
    for query_key, event_key in (
        ("account", "accountName"),
        ("marketHashName", "marketHashName"),
    ):
        expected = _query_value(query, query_key).lower()
        if expected and expected not in str(event.get(event_key) or "").lower():
            return False
    keyword = _query_value(query, "q").lower()
    if keyword and keyword not in json.dumps(event, ensure_ascii=False).lower():
        return False

    operation_id = _query_value(query, "operationId").lower()
    if operation_id:
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        operation_candidates = (
            event.get("operationId"),
            event.get("tradeNo"),
            detail.get("operationId"),
            detail.get("operation_id"),
            detail.get("tradeNo"),
            detail.get("trade_no"),
        )
        normalized_candidates = {
            str(value).strip().lower()
            for value in operation_candidates
            if value not in (None, "")
        }
        normalized_expected = operation_id.removeprefix("gd-")
        if not any(
            operation_id in candidate
            or normalized_expected == candidate.removeprefix("gd-")
            for candidate in normalized_candidates
        ):
            return False

    http_status = _query_value(query, "httpStatus").lower()
    if http_status:
        try:
            actual_status = int(event.get("httpStatus"))
        except (TypeError, ValueError):
            return False
        if http_status.endswith("xx") and len(http_status) == 3:
            if actual_status // 100 != int(http_status[0]):
                return False
        elif http_status == "error":
            if actual_status < 400:
                return False
        elif actual_status != int(http_status):
            return False

    def parse(value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    timestamp = parse(str(event.get("timestamp") or ""))
    start = parse(_query_value(query, "startAt"))
    end = parse(_query_value(query, "endAt"))
    if start is not None and (timestamp is None or timestamp < start):
        return False
    if end is not None and (timestamp is None or timestamp > end):
        return False
    return True


def run_profit_trade_api_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    guadao_report_builder: Callable[
        [Settings, str, str | None, str | None, bool],
        dict[str, Any],
    ] | None = None,
    runtime_controller: UnifiedRuntimeController | None = None,
    case_monitor_controller: CaseMonitorRuntimeController | None = None,
) -> None:
    c5_sweeper = C5CaseSweeper(settings)
    profit_trade_logger = get_profit_trade_event_logger()
    guadao_logger = get_guadao_event_logger()
    owns_runtime_controller = runtime_controller is None
    runtime = runtime_controller or UnifiedRuntimeController(settings)
    runtime.start()
    owns_case_monitor_controller = case_monitor_controller is None
    case_monitor = case_monitor_controller or CaseMonitorRuntimeController(settings)
    case_monitor.start()

    class Handler(BaseHTTPRequestHandler):
        server_version = "CS2AssistantAPI/0.2"
        protocol_version = "HTTP/1.1"

        def _send_json(self, status: int, payload: Any) -> None:
            body = _json_bytes(sanitize_public_payload(payload))
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{path.name}"',
            )
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}

        def _read_json_body_strict(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("request body must be valid UTF-8 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _send_download(
            self,
            *,
            filename: str,
            content_type: str,
            content: str,
        ) -> None:
            body = str(content).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", str(content_type))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{str(filename).replace(chr(34), "")}"',
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _send_profit_trade_log_export(
            self,
            *,
            filters: dict[str, Any],
            export_format: str,
        ) -> None:
            safe_format = str(export_format or "jsonl").strip().lower()
            if safe_format not in {"jsonl", "log"}:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "format must be jsonl or log"},
                )
                return
            filename = f"profit-trade-logs.{safe_format}"
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                "application/x-ndjson; charset=utf-8"
                if safe_format == "jsonl"
                else "text/plain; charset=utf-8",
            )
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for chunk in profit_trade_logger.export_iter(filters, format=safe_format):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _send_profit_trade_log_stream(
            self,
            *,
            query: dict[str, list[str]],
        ) -> None:
            filters = _profit_trade_log_filters(query)
            filters.pop("cursor", None)
            filters.pop("pageSize", None)
            last_event_id = str(
                self.headers.get("Last-Event-ID")
                or _query_value(query, "lastEventId")
                or ""
            ).strip() or None
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    events = profit_trade_logger.wait_after(
                        last_event_id,
                        timeout=15.0,
                        limit=250,
                    )
                    if not events:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        continue
                    last_event_id = str(events[-1].get("event_id") or last_event_id or "") or None
                    for event in events:
                        if not _profit_trade_log_event_matches(event, filters):
                            continue
                        event_id = str(event.get("event_id") or "")
                        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
                        self.wfile.write(b"event: profit_trade_log\n")
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _send_guadao_log_export(
            self,
            *,
            filters: dict[str, Any],
            export_format: str,
        ) -> None:
            safe_format = str(export_format or "jsonl").strip().lower()
            if safe_format not in {"jsonl", "log", "csv"}:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "format must be jsonl, log or csv"},
                )
                return
            filename = f"guadao-logs.{safe_format}"
            content_type = {
                "jsonl": "application/x-ndjson; charset=utf-8",
                "log": "text/plain; charset=utf-8",
                "csv": "text/csv; charset=utf-8",
            }[safe_format]
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for chunk in guadao_logger.export_iter(filters, format=safe_format):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _send_guadao_log_stream(self, *, query: dict[str, list[str]]) -> None:
            include_scheduler = _query_value(
                query, "includeSteamScheduler", "false"
            ).lower() in {"1", "true", "yes", "include"}
            last_event_id = str(
                self.headers.get("Last-Event-ID")
                or _query_value(query, "lastEventId")
                or ""
            ).strip() or None
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    events = guadao_logger.wait_after(last_event_id, timeout=15.0, limit=250)
                    if not events:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        continue
                    last_event_id = str(events[-1].get("event_id") or last_event_id or "") or None
                    for event in events:
                        if (
                            not include_scheduler
                            and str(event.get("component") or "")
                            == "shared_steam_request_scheduler"
                        ):
                            continue
                        public_event = runtime._public_guadao_log(event)
                        if not _public_guadao_log_matches(public_event, query):
                            continue
                        event_id = str(event.get("event_id") or "")
                        payload = json.dumps(
                            public_event,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
                        self.wfile.write(b"event: guadao_log\n")
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send_json(HTTPStatus.NO_CONTENT, {})

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/api/guadao-audit/presets":
                account_error: str | None = None
                try:
                    accounts = [
                        {
                            "id": account.id,
                            "name": account.name,
                            "label": account.name,
                            "steamId": account.steam_id64,
                        }
                        for account in AccountStore(PROJECT_ROOT / "config").list_accounts()
                    ]
                except Exception as exc:
                    accounts = []
                    account_error = str(exc)
                preset = {
                    "id": "guadao-audit-2026-07-19",
                    "name": "2026-07-19 对账基准",
                    "startAt": "2026-07-19T15:20:00+08:00",
                    "endAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "initialBalance": "2502.92",
                    "initialRealValue": "1755.474",
                    "initialComprehensiveRatio": "0.70137040",
                    "accounts": accounts,
                }
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "defaultPreset": preset,
                        "presets": [preset],
                        "accountError": account_error,
                        "readOnly": True,
                        "canExecute": False,
                    },
                )
                return
            if path.startswith("/api/guadao-audit/runs/"):
                parts = [part for part in path.split("/") if part]
                request_id = parts[3] if len(parts) >= 4 else ""
                try:
                    if len(parts) == 4:
                        payload = runtime.guadao_audit_run_status(request_id)
                        self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                        return
                    if len(parts) == 5 and parts[4] == "rows":
                        section = _query_value(query, "section") or None
                        page = _query_int_strict(
                            query,
                            "page",
                            1,
                            maximum=1_000_000_000,
                        )
                        page_size = _query_int_strict(
                            query,
                            "pageSize",
                            50,
                            maximum=200,
                        )
                        rows = list_guadao_audit_rows(settings, request_id, table=section)
                        if not isinstance(rows, list):
                            raise ValueError("section is required for paginated audit rows")
                        total = len(rows)
                        start = (page - 1) * page_size
                        items = rows[start : start + page_size]
                        self._send_json(
                            HTTPStatus.OK,
                            {
                                "ok": True,
                                "requestId": request_id,
                                "section": section,
                                "page": page,
                                "pageSize": page_size,
                                "total": total,
                                "hasMore": start + len(items) < total,
                                "rows": items,
                                "readOnly": True,
                                "canExecute": False,
                            },
                        )
                        return
                    if len(parts) == 5 and parts[4] == "export":
                        exported = export_guadao_audit(
                            settings,
                            request_id,
                            _query_value(query, "format", "json"),
                        )
                        self._send_download(
                            filename=exported["filename"],
                            content_type=exported["contentType"],
                            content=exported["content"],
                        )
                        return
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "guadao_audit_route_not_found"},
                    )
                except (KeyError, LookupError) as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                return
            if path == "/api/c5-research/taxonomy":
                try:
                    payload = build_c5_catalog_taxonomy(settings)
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path.startswith("/api/c5-research/scans/"):
                parts = [part for part in path.split("/") if part]
                request_id = parts[3] if len(parts) >= 4 else ""
                try:
                    if len(parts) == 4:
                        payload = runtime.c5_research_scan_status(request_id)
                    elif len(parts) == 5 and parts[4] == "results":
                        payload = list_c5_research_results(
                            settings,
                            request_id,
                            page=_query_int(
                                query,
                                "page",
                                1,
                                maximum=1_000_000_000,
                            ),
                            page_size=_query_int(query, "pageSize", 50, maximum=200),
                            sort=_query_value(query, "sort", "roi_desc"),
                        )
                    else:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {"ok": False, "error": "c5_research_route_not_found"},
                        )
                        return
                except (KeyError, LookupError) as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/case-monitor/status":
                self._send_json(HTTPStatus.OK, case_monitor.status())
                return
            if path == "/api/case-monitor/report/latest":
                try:
                    payload = case_monitor.latest_report()
                except FileNotFoundError as exc:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/case-monitor/report/export":
                try:
                    export_path, content_type = case_monitor.export_file(
                        _query_value(query, "format", "json"),
                        report_id=_query_value(query, "reportId") or None,
                    )
                    self._send_file(export_path, content_type)
                except FileNotFoundError as exc:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": str(exc)},
                    )
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                return
            if path == "/api/runtime/cookies":
                self._send_json(HTTPStatus.OK, {"ok": True, **runtime.cookie_snapshot()})
                return
            if path == "/api/runtime/state":
                try:
                    payload = runtime.runtime_states(_query_value(query, "executor") or None)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/steam-balances":
                try:
                    payload = load_steam_account_balances(settings)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/guadao/dashboard":
                payload = runtime.dashboard()
                # `sanitize_public_payload()` intentionally removes keys that
                # contain "cookie".  Publish the already-safe health projection
                # under a neutral name so S1 can render account auth state
                # without exposing authentication material.
                payload["steamAuthHealth"] = payload.get("cookieGate")
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/guadao/operations":
                try:
                    payload = runtime.operations(
                        limit=_query_int(query, "limit", 50_000, maximum=50_000),
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "pageSize", 10, maximum=100),
                        keyword=_query_value(query, "q") or None,
                        account_name=_query_value(query, "account") or None,
                        market_hash_name=_query_value(query, "marketHashName") or None,
                        status=_query_value(query, "status") or None,
                        start_at=_query_value(query, "startAt") or None,
                        end_at=_query_value(query, "endAt") or None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/guadao/issues":
                include_ack = _query_value(query, "acknowledged", "include").lower() in {
                    "all", "include", "true", "1"
                }
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, **runtime.issues(include_acknowledged=include_ack)},
                )
                return
            if path == "/api/guadao/settings":
                self._send_json(HTTPStatus.OK, {"ok": True, **_guadao_settings_payload(runtime)})
                return
            if path == "/api/guadao/items/search":
                try:
                    payload = runtime.search_items(
                        _query_value(query, "q") or _query_value(query, "query"),
                        limit=_query_int(query, "limit", 30, maximum=100),
                        offset=_query_int(query, "offset", 0, minimum=0),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/guadao/logs":
                try:
                    page = _query_int(query, "page", 1)
                    page_size = _query_int(query, "pageSize", 20, maximum=100)
                    filters = _guadao_log_filters(query)
                    filters.pop("cursor", None)
                    storage = guadao_logger.storage_status()
                    include_scheduler = _query_value(
                        query, "includeSteamScheduler", "false"
                    ).lower() in {"1", "true", "yes", "include"}
                    target_count = page * page_size + 1
                    public_events: list[dict[str, Any]] = []
                    cursor: str | None = None
                    logger_has_more = False
                    seen_cursors: set[str] = set()
                    while len(public_events) < target_count:
                        query_filters = dict(filters)
                        query_filters["pageSize"] = 1000
                        if cursor:
                            query_filters["cursor"] = cursor
                        result = guadao_logger.query(query_filters)
                        chunk = [
                            runtime._public_guadao_log(event)
                            for event in result.get("events") or []
                        ]
                        if not include_scheduler:
                            chunk = [
                                event
                                for event in chunk
                                if event.get("service") != "shared_steam_request_scheduler"
                            ]
                        public_events.extend(
                            event
                            for event in chunk
                            if _public_guadao_log_matches(event, query)
                        )
                        logger_has_more = bool(result.get("hasMore"))
                        next_cursor = str(result.get("nextCursor") or "") or None
                        if not logger_has_more or not next_cursor or next_cursor in seen_cursors:
                            break
                        seen_cursors.add(next_cursor)
                        cursor = next_cursor
                    if include_scheduler:
                        logged_request_ids = {
                            str(event.get("requestId"))
                            for event in public_events
                            if event.get("requestId")
                        }
                        public_events.extend(
                            event
                            for event in runtime.steam_scheduler_log_rows(limit=1000)
                            if str(event.get("caller") or "") != "guadao"
                            and str(event.get("requestId") or "") not in logged_request_ids
                            and _public_guadao_log_matches(event, query)
                        )
                    public_events.sort(
                        key=lambda event: str(event.get("timestamp") or ""), reverse=True
                    )
                    start = (page - 1) * page_size
                    public_logs = public_events[start : start + page_size]
                    has_more = logger_has_more or len(public_events) > start + page_size
                    total = len(public_events) + (1 if logger_has_more else 0)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "logs": public_logs,
                        "events": public_logs,
                        "items": public_logs,
                        "total": total,
                        "page": page,
                        "pageSize": page_size,
                        "hasMore": has_more,
                        "meta": {
                            "retentionDays": storage.get("retentionDays"),
                            "diskUsageMb": round(float(storage.get("totalBytes") or 0) / 1_048_576, 3),
                            "fileCount": storage.get("fileCount"),
                            "streamStatus": "available",
                            "startAt": storage.get("earliestTimestamp"),
                            "endAt": storage.get("latestTimestamp"),
                        },
                        "storage": storage,
                    },
                )
                return
            if path == "/api/guadao/logs/event":
                event = guadao_logger.get_event(_query_value(query, "eventId"))
                if event is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "log_event_not_found"})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "event": runtime._public_guadao_log(event), "rawEvent": event},
                )
                return
            if path == "/api/guadao/logs/stream":
                self._send_guadao_log_stream(query=query)
                return
            if path == "/api/guadao/logs/export":
                export_filters = _guadao_log_filters(query)
                export_filters["includeSteamScheduler"] = _query_value(
                    query, "includeSteamScheduler", "false"
                ).lower() in {"1", "true", "yes", "include"}
                self._send_guadao_log_export(
                    filters=export_filters,
                    export_format=_query_value(query, "format", "jsonl"),
                )
                return
            if path == "/api/profit-trade/roi-watch":
                try:
                    active_text = _query_value(query, "active", "true").lower()
                    if active_text not in {
                        "1", "true", "yes", "0", "false", "no", "all", "include"
                    }:
                        raise ValueError("active must be true, false, or all")
                    active = None if active_text in {"all", "include"} else active_text not in {"0", "false", "no"}
                    roi_sign = _query_value(query, "roiSign", "all").lower()
                    if roi_sign not in {"all", "positive", "negative"}:
                        raise ValueError("roiSign must be all, positive, or negative")
                    payload = build_profit_trade_roi_watch_payload(
                        settings,
                        active=active,
                        keyword=_query_value(query, "keyword") or None,
                        execution_status=_query_value(query, "status") or None,
                        roi_sign=None if roi_sign == "all" else roi_sign,
                        sort=_query_value(query, "sort", "roi_desc"),
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "pageSize", 50, maximum=200),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/profit-trade/manual-execution/status":
                try:
                    payload = runtime.profit_trade_manual_execution_status(
                        _query_value(query, "requestId")
                    )
                except LookupError as exc:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/profit-trade/roi-watch/history":
                try:
                    payload = build_profit_trade_roi_history_payload(
                        settings,
                        _query_value(query, "marketHashName"),
                        from_time=_query_value(query, "from") or None,
                        to_time=_query_value(query, "to") or None,
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "pageSize", 100, maximum=500),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/profit-trade/selection-watch":
                try:
                    active_text = _query_value(query, "active", "true").lower()
                    if active_text not in {
                        "1", "true", "yes", "0", "false", "no", "all", "include"
                    }:
                        raise ValueError("active must be true, false, or all")
                    active = None if active_text in {"all", "include"} else active_text not in {"0", "false", "no"}
                    payload = build_profit_trade_selection_watch_payload(
                        settings,
                        active=active,
                        keyword=_query_value(query, "keyword") or None,
                        status=_query_value(query, "status") or None,
                        sort=_query_value(query, "sort", "roi_desc"),
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "pageSize", 50, maximum=200),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/profit-trade/selection-watch/history":
                try:
                    payload = build_profit_trade_selection_history_payload(
                        settings,
                        _query_value(query, "marketHashName"),
                        from_time=_query_value(query, "from") or None,
                        to_time=_query_value(query, "to") or None,
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "pageSize", 100, maximum=500),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/profit-trade/interruptions":
                try:
                    status_text = _query_value(query, "status")
                    statuses = tuple(
                        value.strip()
                        for value in status_text.split(",")
                        if value.strip()
                    ) or ("cancelled", "failed", "manual_required")
                    payload = build_profit_trade_interruptions_payload(
                        settings,
                        statuses=statuses,
                        step_key=_query_value(query, "stepKey") or None,
                        acknowledged=_query_value(query, "acknowledged", "exclude"),
                        keyword=_query_value(query, "keyword") or None,
                        from_time=_query_value(query, "from") or None,
                        to_time=_query_value(query, "to") or None,
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "pageSize", 50, maximum=200),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/profit-trade/interruptions/timeline":
                try:
                    trade_id_text = _query_value(query, "tradeId")
                    if not trade_id_text:
                        raise ValueError("tradeId is required")
                    trade_id = int(trade_id_text)
                    if trade_id <= 0:
                        raise ValueError("tradeId must be a positive integer")
                    payload = build_profit_trade_interruption_timeline_payload(settings, trade_id)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/profit-trade/logs":
                try:
                    filters = _profit_trade_log_filters(query)
                    result = profit_trade_logger.query(filters)
                    storage = profit_trade_logger.storage_status()
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        **result,
                        "items": result.get("events", []),
                        "storage": storage,
                    },
                )
                return
            if path == "/api/profit-trade/logs/event":
                event_id = _query_value(query, "eventId")
                event = profit_trade_logger.get_event(event_id)
                if event is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "log_event_not_found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "event": event})
                return
            if path == "/api/profit-trade/logs/stream":
                self._send_profit_trade_log_stream(query=query)
                return
            if path == "/api/profit-trade/logs/export":
                filters = _profit_trade_log_filters(query)
                export_format = _query_value(query, "format", "jsonl")
                self._send_profit_trade_log_export(
                    filters=filters,
                    export_format=export_format,
                )
                return
            if path == "/api/c5-sweeper/dashboard":
                round_id = str((query.get("roundId") or [""])[0]).strip() or None
                try:
                    self._send_json(HTTPStatus.OK, c5_sweeper.dashboard(round_id))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            if path == "/api/c5-sweeper/items":
                keyword = str((query.get("query") or [""])[0])
                try:
                    limit = int((query.get("limit") or ["20"])[0])
                    offset = max(0, int((query.get("offset") or ["0"])[0]))
                    payload = c5_sweeper.search_items_page(keyword, limit=limit, offset=offset)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path == "/api/c5-sweeper/accounts":
                refresh = str((query.get("refresh") or [""])[0]).lower() in {"1", "true", "yes"}
                try:
                    accounts = c5_sweeper.receiving_accounts(refresh=refresh)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "accounts": accounts})
                return
            if path == "/api/profit-trade/dashboard":
                self._send_json(HTTPStatus.OK, build_profit_trade_dashboard_payload(settings))
                return
            if path == "/api/profit-trade/completed":
                try:
                    payload = build_profit_trade_completed_payload(
                        settings,
                        bought_from=_query_value(query, "boughtFrom") or None,
                        bought_to=_query_value(query, "boughtTo") or None,
                    )
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/profit-trade/items/search":
                keyword = _query_value(query, "query")
                try:
                    limit = _query_int(query, "limit", 20, minimum=1, maximum=50)
                    offset = _query_int(query, "offset", 0, minimum=0)
                    result = search_profit_trade_catalog_items(
                        settings,
                        keyword,
                        limit=limit,
                        offset=offset,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/config":
                self._send_json(
                    HTTPStatus.OK,
                    build_profit_trade_dashboard_payload(settings, limit=0)["config"],
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/guadao-audit/runs":
                try:
                    body = self._read_json_body_strict()
                    payload = runtime.queue_guadao_audit_run(body)
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                request_id = str(payload.get("requestId") or "").strip()
                if not request_id:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": "audit queue returned no requestId"},
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, **payload})
                return
            if path.startswith("/api/guadao-audit/runs/"):
                parts = [part for part in path.split("/") if part]
                request_id = parts[3] if len(parts) >= 4 else ""
                action = parts[4] if len(parts) == 5 else ""
                if action not in {"cancel", "retry"}:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "guadao_audit_action_not_found"},
                    )
                    return
                try:
                    self._read_json_body_strict()
                    payload = (
                        runtime.cancel_guadao_audit_run(request_id)
                        if action == "cancel"
                        else runtime.retry_guadao_audit_run(request_id)
                    )
                except (KeyError, LookupError) as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, **payload})
                return
            if path == "/api/c5-research/estimate":
                try:
                    body = self._read_json_body_strict()
                    payload = estimate_c5_catalog_filter(settings, body)
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "researchOnly": True,
                        "canExecute": False,
                        **payload,
                    },
                )
                return
            if path == "/api/c5-research/scans":
                try:
                    body = self._read_json_body_strict()
                    payload = runtime.queue_c5_research_scan(body)
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                request_id = str(payload.get("requestId") or "").strip()
                if not request_id:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": "research queue returned no requestId"},
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, **payload})
                return
            if path.startswith("/api/c5-research/scans/"):
                parts = [part for part in path.split("/") if part]
                request_id = parts[3] if len(parts) >= 4 else ""
                action = parts[4] if len(parts) == 5 else ""
                if action not in {"pause", "resume", "cancel"}:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "c5_research_action_not_found"},
                    )
                    return
                try:
                    self._read_json_body_strict()
                    payload = runtime.control_c5_research_scan(request_id, action)
                except (KeyError, LookupError) as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, **payload})
                return
            if path == "/api/case-monitor/start":
                body = self._read_json_body()
                try:
                    payload = case_monitor.start_monitor(body.get("intervalMinutes", 5))
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/case-monitor/pause":
                self._send_json(HTTPStatus.OK, case_monitor.pause_monitor())
                return
            if path == "/api/case-monitor/collect":
                try:
                    job = case_monitor.request_collect()
                except CaseMonitorBusyError as exc:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "ok": False,
                            "error": str(exc),
                            "currentJob": exc.job,
                        },
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job": job})
                return
            if path == "/api/case-monitor/report":
                body = self._read_json_body()
                try:
                    job = case_monitor.request_report(body)
                except CaseMonitorBusyError as exc:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "ok": False,
                            "error": str(exc),
                            "currentJob": exc.job,
                        },
                    )
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job": job})
                return
            if path == "/api/steam-balances/refresh":
                try:
                    payload = refresh_steam_account_balances(settings)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
                return
            if path in {"/api/runtime/toggle", "/api/guadao/runtime/toggle"}:
                body = self._read_json_body()
                executor_key = str(body.get("executor") or "guadao")
                if "enabled" not in body:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "enabled is required"})
                    return
                try:
                    result = runtime.toggle_executor(executor_key, bool(body["enabled"]))
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "runtime": result})
                return
            if path == "/api/guadao/runtime/run-due":
                runtime.wake()
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {
                        "ok": True,
                        "queued": True,
                        "scope": "unified_runtime",
                        "message": "已唤醒统一到期任务调度；各执行器仍服从自己的持久化开关",
                    },
                )
                return
            if path == "/api/guadao/runtime/stale-recheck-now":
                body = self._read_json_body()
                confirmed = body.get("confirm") == "stale_listing_recheck_only"
                try:
                    result = runtime.stale_listing_recheck_now(confirmed=confirmed)
                except RuntimeError as exc:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, result)
                return
            if path == "/api/guadao/runtime/full-scan":
                try:
                    result = runtime.full_scan_now()
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                if not bool(result.get("ok")):
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {**result, "error": "guadao_scan_task_not_scheduled"},
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, result)
                return
            if path in {"/api/runtime/migration/confirm", "/api/guadao/migration/confirm"}:
                try:
                    result = runtime.confirm_migration()
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path in {"/api/runtime/cookies/refresh", "/api/guadao/cookies/refresh"}:
                try:
                    result = runtime.refresh_all_cookies_now()
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.ACCEPTED, result)
                return
            if path == "/api/guadao/auth/retry-failed":
                try:
                    result = runtime.retry_failed_steam_auth_now()
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.ACCEPTED, result)
                return
            if path == "/api/guadao/issues/review":
                body = self._read_json_body()
                issue_id = str(body.get("issueId") or "").strip()
                if not issue_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "issueId is required"})
                    return
                try:
                    result = runtime.queue_issue_safe_review(issue_id)
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.ACCEPTED, result)
                return
            if path == "/api/guadao/issues/ack":
                body = self._read_json_body()
                issue_id = str(body.get("issueId") or "").strip()
                if not issue_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "issueId is required"})
                    return
                try:
                    result = runtime.acknowledge_issue(
                        issue_id,
                        acknowledged=bool(body.get("acknowledged", True)),
                        reason=str(body.get("reason") or "") or None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "acknowledgement": result})
                return
            if path == "/api/guadao/operations/batch-refreeze-rebuy":
                body = self._read_json_body()
                try:
                    result = runtime.batch_refreeze_guadao_rebuys(
                        body.get("operationIds"),
                        rebuy_price=float(body.get("rebuyPrice")),
                        execute_now=bool(body.get("executeNow", True)),
                        confirmed=body.get("confirmed") is True,
                        request_id=str(body.get("requestId") or "") or None,
                        reason=str(body.get("reason") or "") or None,
                    )
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/guadao/operations/batch-manual-complete":
                body = self._read_json_body()
                try:
                    result = runtime.batch_complete_guadao_rebuys_manually(
                        body.get("operationIds"),
                        actual_rebuy_price=float(body.get("actualRebuyPrice")),
                        source=str(body.get("source") or ""),
                        completed_at=str(body.get("completedAt") or ""),
                        memo=str(body.get("memo") or "") or None,
                        external_order_ref=str(body.get("externalOrderRef") or "") or None,
                        confirmed=body.get("confirmed") is True,
                        request_id=str(body.get("requestId") or "") or None,
                        reason=str(body.get("reason") or "") or None,
                    )
                except (TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/guadao/settings":
                body = self._read_json_body()
                try:
                    result = runtime.update_settings(body)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, **_guadao_settings_payload(runtime, result)},
                )
                return
            if path == "/api/guadao-report/query":
                if guadao_report_builder is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"ok": False, "error": "挂刀报表服务未配置"},
                    )
                    return
                body = self._read_json_body()
                date_from = str(body.get("dateFrom") or "").strip()
                if not date_from:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "dateFrom is required"},
                    )
                    return
                try:
                    report = guadao_report_builder(
                        settings,
                        date_from,
                        str(body.get("dateTo") or "").strip() or None,
                        str(body.get("marketHashName") or "").strip() or None,
                        bool(body.get("includeDetails")),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "report": report})
                return
            if path in {"/api/c5-sweeper/round", "/api/c5-sweeper/task"}:
                body = self._read_json_body()
                try:
                    kwargs = dict(
                        market_hash_name=str(body.get("marketHashName") or ""),
                        display_name=str(body.get("displayName") or "") or None,
                        receiving_account_id=str(body.get("receivingAccountId") or ""),
                        max_price=float(body.get("maxPrice") or 0),
                        budget=float(body.get("budget") or 0),
                        target_count=int(body.get("targetCount") or 0),
                        interval_seconds=int(body.get("intervalSeconds") or 60),
                        delivery=int(body.get("delivery") or 2),
                    )
                    round_id = str(body.get("roundId") or "").strip()
                    if round_id:
                        result = c5_sweeper.update_round(round_id, **kwargs)
                    else:
                        result = c5_sweeper.create_round(**kwargs)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "dashboard": result})
                return
            if path in {"/api/c5-sweeper/start", "/api/c5-sweeper/resume"}:
                body = self._read_json_body()
                try:
                    result = c5_sweeper.start(
                        str(body.get("confirmation") or ""),
                        round_id=str(body.get("roundId") or "").strip() or None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "dashboard": result})
                return
            if path == "/api/c5-sweeper/pause":
                body = self._read_json_body()
                try:
                    result = c5_sweeper.pause(round_id=str(body.get("roundId") or "").strip() or None)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "dashboard": result})
                return
            if path == "/api/c5-sweeper/stop":
                body = self._read_json_body()
                try:
                    result = c5_sweeper.stop(round_id=str(body.get("roundId") or "").strip() or None)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "dashboard": result})
                return
            if path == "/api/c5-sweeper/refresh":
                # Manual refresh audits pending C5 orders and refreshes price only.
                # It deliberately cannot buy, so it cannot bypass the one-minute cadence.
                body = self._read_json_body()
                try:
                    result = c5_sweeper.run_cycle(
                        allow_buy=False,
                        round_id=str(body.get("roundId") or "").strip() or None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "dashboard": result})
                return
            if path == "/api/c5-sweeper/confirm-not-bought":
                body = self._read_json_body()
                try:
                    result = c5_sweeper.confirm_unresolved_not_bought(
                        round_id=str(body.get("roundId") or "").strip() or None,
                        reason=str(body.get("reason") or "").strip() or None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "dashboard": result})
                return
            if path == "/api/profit-trade/config":
                body = self._read_json_body()
                allowed_fields = {
                    "enabled",
                    "allowRealExecution",
                    "longBuyEnabled",
                    "longBuyAllowRealExecution",
                    "longBuyMaxActiveOrders",
                    "longBuyCreateFractionPerCycle",
                    "longBuyAggressiveRoiDelta",
                    "longBuyMinPriceAdvantage",
                    "longBuyMaxPriceAdvantage",
                    "stickerSlabStatus",
                    "stickerStatus",
                    "dailySteamBudget",
                    "accountReservedBalances",
                }
                unsupported_fields = sorted(set(body) - allowed_fields)
                if unsupported_fields:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": f"unsupported config field(s): {', '.join(unsupported_fields)}"},
                    )
                    return
                if not any(field in body for field in allowed_fields):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "at least one supported Profit Trade config field is required"},
                    )
                    return
                try:
                    config = set_profit_trade_config(
                        settings,
                        enabled=bool(body["enabled"]) if "enabled" in body else None,
                        allow_real_execution=bool(body["allowRealExecution"])
                        if "allowRealExecution" in body
                        else None,
                        long_buy_enabled=bool(body["longBuyEnabled"])
                        if "longBuyEnabled" in body
                        else None,
                        long_buy_allow_real_execution=bool(
                            body["longBuyAllowRealExecution"]
                        )
                        if "longBuyAllowRealExecution" in body
                        else None,
                        long_buy_max_active_orders=int(body["longBuyMaxActiveOrders"])
                        if "longBuyMaxActiveOrders" in body
                        else None,
                        long_buy_create_fraction_per_cycle=float(
                            body["longBuyCreateFractionPerCycle"]
                        )
                        if "longBuyCreateFractionPerCycle" in body
                        else None,
                        long_buy_aggressive_roi_delta=float(
                            body["longBuyAggressiveRoiDelta"]
                        )
                        if "longBuyAggressiveRoiDelta" in body
                        else None,
                        long_buy_min_price_advantage=float(
                            body["longBuyMinPriceAdvantage"]
                        )
                        if "longBuyMinPriceAdvantage" in body
                        else None,
                        long_buy_max_price_advantage=float(
                            body["longBuyMaxPriceAdvantage"]
                        )
                        if "longBuyMaxPriceAdvantage" in body
                        else None,
                        sticker_slab_status=str(body["stickerSlabStatus"])
                        if "stickerSlabStatus" in body
                        else None,
                        sticker_status=str(body["stickerStatus"])
                        if "stickerStatus" in body
                        else None,
                        daily_steam_budget=float(body["dailySteamBudget"])
                        if "dailySteamBudget" in body
                        else None,
                        account_reserved_balances=body["accountReservedBalances"]
                        if "accountReservedBalances" in body
                        else None,
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "enabled": config.profit_trade_enabled,
                        "allowRealExecution": config.profit_trade_allow_real_execution,
                        "longBuyEnabled": config.profit_trade_long_buy_enabled,
                        "longBuyAllowRealExecution": (
                            config.profit_trade_long_buy_allow_real_execution
                        ),
                        "config": build_profit_trade_dashboard_payload(settings, limit=0)["config"],
                    },
                )
                return
            if path == "/api/profit-trade/serverchan/daily-report":
                try:
                    send_profit_trade_daily_report(settings)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/profit-trade/protection":
                body = self._read_json_body()
                try:
                    config = update_profit_trade_protection(
                        settings,
                        action=str(body.get("action") or ""),
                        kind=str(body.get("kind") or ""),
                        value=str(body.get("value") or ""),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "config": build_profit_trade_dashboard_payload(
                            settings,
                            config=config,
                            limit=0,
                        )["config"],
                    },
                )
                return
            if path == "/api/profit-trade/selection-watch":
                body = self._read_json_body()
                try:
                    result = update_profit_trade_selection_watch(
                        settings,
                        action=str(body.get("action") or ""),
                        market_hash_name=str(body.get("marketHashName") or ""),
                    )
                    # The selection pool is research-only, but an add/re-enter
                    # should make its independent P3 task eligible immediately.
                    runtime.wake()
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/selection-watch/refresh":
                self._read_json_body()
                try:
                    result = runtime.profit_selection_watch_now()
                except RuntimeError as exc:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(HTTPStatus.ACCEPTED, result)
                return
            if path == "/api/profit-trade/roi-watch/execute":
                body = self._read_json_body()
                try:
                    result = runtime.queue_profit_trade_manual_execution(
                        market_hash_name=str(body.get("marketHashName") or ""),
                        quantity=int(body.get("quantity") or 0),
                        confirmed=body.get("confirmed") is True,
                        expected_roi=body.get("expectedRoi"),
                        scan_id=str(body.get("scanId") or "") or None,
                        observed_at=str(body.get("observedAt") or "") or None,
                    )
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.ACCEPTED, result)
                return
            if path == "/api/profit-trade/scan":
                body = self._read_json_body()
                try:
                    report = scan_profit_trade_opportunities(
                        settings,
                        limit=int(body.get("limit") or 20),
                        scan_max_items=int(body["scanMaxItems"]) if body.get("scanMaxItems") is not None else None,
                        record=bool(body.get("record")),
                        lock_asset=bool(body.get("lock")),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "report": report.to_dict()})
                return
            if path == "/api/profit-trade/run-once":
                try:
                    result = runtime.profit_cycle_now()
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.ACCEPTED, result)
                return
            if path == "/api/profit-trade/refresh-sales":
                try:
                    result = refresh_profit_trade_sales(settings)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/dismiss":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = dismiss_profit_trade(
                        settings,
                        int(trade_id),
                        reason=str(body.get("reason") or "user dismissed from dashboard"),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/interruptions/acknowledge":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                action = str(body.get("action") or "acknowledge").strip().lower()
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                if action not in {"acknowledge", "restore"}:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "action must be acknowledge or restore"})
                    return
                try:
                    reason = str(body.get("reason") or "user acknowledged from interruption tracker")
                    result = set_profit_trade_interruption_acknowledged(
                        settings,
                        int(trade_id),
                        acknowledged=action == "acknowledge",
                        reason=reason,
                    )
                    if (
                        action == "acknowledge"
                        and result.get("conflict")
                        and result.get("requiresRemoteResolution")
                    ):
                        resolution = dismiss_profit_trade(
                            settings,
                            int(trade_id),
                            reason=f"safe interruption acknowledgement: {reason}",
                        )
                        if resolution.get("dismissed") is False:
                            self._send_json(
                                HTTPStatus.CONFLICT,
                                {
                                    "ok": False,
                                    "error": resolution.get("message") or "Steam order terminal state changed",
                                    "resolution": resolution,
                                },
                            )
                            return
                        result = set_profit_trade_interruption_acknowledged(
                            settings,
                            int(trade_id),
                            acknowledged=True,
                            reason=reason,
                        )
                except Exception as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                if result.get("conflict"):
                    self._send_json(HTTPStatus.CONFLICT, result)
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/manual-settle":
                body = self._read_json_body()
                try:
                    result = manual_settle_profit_trade(
                        settings,
                        int(body.get("tradeId") or 0),
                        sold_net_price=float(body.get("soldNetPrice") or 0),
                        source=str(body.get("source") or "manual_other_platform"),
                        memo=str(body.get("memo") or "") or None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/manual-record/create":
                body = self._read_json_body()
                try:
                    result = create_manual_profit_trade_record(
                        settings,
                        market_hash_name=body.get("marketHashName"),
                        name=body.get("name"),
                        steam_account_id=body.get("steamAccountId"),
                        steam_buy_price=body.get("steamBuyPrice"),
                        balance_discount=body.get("balanceDiscount"),
                        c5_sold_net_price=body.get("c5SoldNetPrice"),
                        steam_bought_at=body.get("steamBoughtAt"),
                        completed_at=body.get("completedAt"),
                        a_asset_id=body.get("aAssetId"),
                        b_asset_id=body.get("bAssetId"),
                        memo=body.get("memo"),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/manual-record/update":
                body = self._read_json_body()
                trade_id = body.get("tradeId")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = update_manual_profit_trade_record(
                        settings,
                        int(trade_id),
                        market_hash_name=body.get("marketHashName"),
                        name=body.get("name"),
                        steam_account_id=body.get("steamAccountId"),
                        steam_buy_price=body.get("steamBuyPrice"),
                        balance_discount=body.get("balanceDiscount"),
                        c5_sold_net_price=body.get("c5SoldNetPrice"),
                        steam_bought_at=body.get("steamBoughtAt"),
                        completed_at=body.get("completedAt"),
                        a_asset_id=body.get("aAssetId"),
                        b_asset_id=body.get("bAssetId"),
                        memo=body.get("memo"),
                    )
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/lock":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = lock_profit_trade(settings, int(trade_id))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/buy":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = execute_profit_trade_buy(settings, int(trade_id))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/list-c5":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = execute_profit_trade_list_c5(settings, int(trade_id))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"profitTrade API listening on http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        if owns_case_monitor_controller:
            case_monitor.stop()
        if owns_runtime_controller:
            runtime.stop()
        c5_sweeper.close()
        server.server_close()
