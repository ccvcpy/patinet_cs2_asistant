from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from cs2_assistant.config import Settings
from cs2_assistant.services.c5_case_sweeper import C5CaseSweeper
from cs2_assistant.services.profit_trade_logging import get_profit_trade_event_logger
from cs2_assistant.services.public_payload import sanitize_public_payload
from cs2_assistant.services.profit_trade import (
    build_profit_trade_dashboard_payload,
    build_profit_trade_interruption_timeline_payload,
    build_profit_trade_interruptions_payload,
    build_profit_trade_roi_history_payload,
    build_profit_trade_roi_watch_payload,
    dismiss_profit_trade,
    execute_profit_trade_buy,
    execute_profit_trade_list_c5,
    lock_profit_trade,
    manual_settle_profit_trade,
    refresh_profit_trade_sales,
    run_profit_trade_once,
    scan_profit_trade_opportunities,
    send_profit_trade_daily_report,
    set_profit_trade_config,
    set_profit_trade_interruption_acknowledged,
    update_profit_trade_protection,
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


def run_profit_trade_api_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    guadao_report_builder: Callable[
        [Settings, str, str | None, str | None, bool],
        dict[str, Any],
    ] | None = None,
) -> None:
    c5_sweeper = C5CaseSweeper(settings)
    profit_trade_logger = get_profit_trade_event_logger()

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

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send_json(HTTPStatus.NO_CONTENT, {})

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/api/profit-trade/roi-watch":
                try:
                    active_text = _query_value(query, "active", "true").lower()
                    if active_text not in {
                        "1", "true", "yes", "0", "false", "no", "all", "include"
                    }:
                        raise ValueError("active must be true, false, or all")
                    active = None if active_text in {"all", "include"} else active_text not in {"0", "false", "no"}
                    payload = build_profit_trade_roi_watch_payload(
                        settings,
                        active=active,
                        keyword=_query_value(query, "keyword") or None,
                        execution_status=_query_value(query, "status") or None,
                        sort=_query_value(query, "sort", "roi_desc"),
                        page=_query_int(query, "page", 1),
                        page_size=_query_int(query, "pageSize", 50, maximum=200),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, **payload})
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
                    items = c5_sweeper.search_items(keyword, limit=limit)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "items": items})
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
            if path == "/api/profit-trade/config":
                self._send_json(
                    HTTPStatus.OK,
                    build_profit_trade_dashboard_payload(settings, limit=0)["config"],
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
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
            if path == "/api/profit-trade/config":
                body = self._read_json_body()
                if (
                    "enabled" not in body
                    and "allowRealExecution" not in body
                    and "allowRepriceExecution" not in body
                    and "stickerSlabStatus" not in body
                    and "stickerStatus" not in body
                    and "dailySteamBudget" not in body
                ):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "enabled, allowRealExecution, allowRepriceExecution, stickerSlabStatus, stickerStatus or dailySteamBudget is required"},
                    )
                    return
                config = set_profit_trade_config(
                    settings,
                    enabled=bool(body["enabled"]) if "enabled" in body else None,
                    allow_real_execution=bool(body["allowRealExecution"])
                    if "allowRealExecution" in body
                    else None,
                    allow_reprice_execution=bool(body["allowRepriceExecution"])
                    if "allowRepriceExecution" in body
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
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "enabled": config.profit_trade_enabled,
                        "allowRealExecution": config.profit_trade_allow_real_execution,
                        "allowRepriceExecution": config.profit_trade_allow_reprice_execution,
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
                body = self._read_json_body()
                try:
                    report = run_profit_trade_once(
                        settings,
                        scan_max_items=int(body["scanMaxItems"]) if body.get("scanMaxItems") is not None else None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "report": report.to_dict()})
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
        c5_sweeper.close()
        server.server_close()
