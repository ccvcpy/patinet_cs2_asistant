from __future__ import annotations

import csv
import io
import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from cs2_assistant.config import PROJECT_ROOT
from cs2_assistant.services.profit_trade_logging import (
    DEFAULT_BROKER_SIZE,
    DEFAULT_RETENTION_DAYS,
    ProfitTradeEventBroker,
    ProfitTradeEventLogger,
    TelemetryCallback,
)


DEFAULT_GUADAO_LOG_DIR = PROJECT_ROOT / "logs" / "guadao"
GUADAO_LOG_DIR_ENV = "CS2_GUADAO_LOG_DIR"

_CSV_FIELDS = (
    "timestamp_utc",
    "level",
    "provider",
    "component",
    "operation",
    "message",
    "account_id",
    "steam_id64",
    "market_hash_name",
    "asset_id",
    "trade_no",
    "request_id",
    "method",
    "endpoint",
    "status_code",
    "elapsed_ms",
    "retry_after",
    "state_from",
    "state_to",
    "safe_context",
)

_SHARED_STEAM_SCHEDULER_COMPONENT = "shared_steam_request_scheduler"


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "include"}


class GuadaoEventBroker(ProfitTradeEventBroker):
    """Bounded in-process feed consumed by the guadao SSE endpoint."""


class GuadaoEventLogger(ProfitTradeEventLogger):
    """Fail-open structured logger isolated to ``source=guadao``.

    Storage and SSE failures are deliberately independent from trading state.
    The base implementation supplies UTC-day JSONL files, closed-day gzip,
    retention, filtering, cursor pagination, redaction, and the event broker.
    """

    event_source = "guadao"
    event_id_prefix = "gdlog"
    maintenance_thread_name = "guadao-log-maintenance"
    failure_label = "guadao-log"

    @staticmethod
    def _include_export_event(
        event: Mapping[str, Any],
        *,
        include_steam_scheduler: bool,
        market_hash_name: str | None,
        operation_id: str | None,
        http_status: str | None,
    ) -> bool:
        if (
            not include_steam_scheduler
            and str(event.get("component") or "").strip().lower()
            == _SHARED_STEAM_SCHEDULER_COMPONENT
        ):
            return False
        expected_name = str(market_hash_name or "").strip().lower()
        if expected_name and expected_name not in str(
            event.get("market_hash_name") or ""
        ).lower():
            return False

        expected_operation = str(operation_id or "").strip().lower()
        if expected_operation:
            context = event.get("safe_context")
            context = context if isinstance(context, Mapping) else {}
            candidates = {
                str(value).strip().lower()
                for value in (
                    event.get("trade_id"),
                    event.get("trade_no"),
                    context.get("operationId"),
                    context.get("operation_id"),
                    context.get("tradeNo"),
                    context.get("trade_no"),
                )
                if value not in (None, "")
            }
            normalized_expected = expected_operation.removeprefix("gd-")
            if not any(
                expected_operation in candidate
                or normalized_expected == candidate.removeprefix("gd-")
                for candidate in candidates
            ):
                return False

        expected_status = str(http_status or "").strip().lower()
        if expected_status:
            try:
                actual_status = int(event.get("status_code"))
            except (TypeError, ValueError):
                return False
            if expected_status.endswith("xx") and len(expected_status) == 3:
                if actual_status // 100 != int(expected_status[0]):
                    return False
            elif expected_status == "error":
                if actual_status < 400:
                    return False
            elif actual_status != int(expected_status):
                return False
        return True

    def iter_export(
        self,
        *,
        format: str = "jsonl",
        from_time: str | datetime | None = None,
        to_time: str | datetime | None = None,
        level: str | Iterable[str] | None = None,
        provider: str | Iterable[str] | None = None,
        component: str | Iterable[str] | None = None,
        operation: str | Iterable[str] | None = None,
        steam_id64: str | None = None,
        account_id: str | None = None,
        trade_no: str | None = None,
        request_id: str | None = None,
        keyword: str | None = None,
        market_hash_name: str | None = None,
        operation_id: str | None = None,
        http_status: str | None = None,
        include_steam_scheduler: bool = False,
    ) -> Iterator[bytes]:
        export_format = str(format or "jsonl").strip().lower()
        if export_format not in {"jsonl", "log", "csv"}:
            raise ValueError("format must be jsonl, log or csv")

        filtered_events = (
            event
            for event in self._iter_filtered_events(
                from_time=from_time,
                to_time=to_time,
                level=level,
                provider=provider,
                component=component,
                operation=operation,
                steam_id64=steam_id64,
                account_id=account_id,
                trade_no=trade_no,
                request_id=request_id,
                keyword=keyword,
            )
            if self._include_export_event(
                event,
                include_steam_scheduler=include_steam_scheduler,
                market_hash_name=market_hash_name,
                operation_id=operation_id,
                http_status=http_status,
            )
        )
        if export_format != "csv":
            for event in filtered_events:
                line = (
                    json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    if export_format == "jsonl"
                    else self._format_readable_log(event)
                )
                yield f"{line}\n".encode("utf-8")
            return

        # UTF-8 BOM keeps Chinese item names readable when opened in Excel.
        yield b"\xef\xbb\xbf"
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)
        for event in filtered_events:
            row = dict(event)
            context = row.get("safe_context")
            if context is not None and not isinstance(context, str):
                row["safe_context"] = json.dumps(
                    context,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            writer.writerow(row)
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)

    def export_iter(
        self,
        filters: Mapping[str, Any] | None = None,
        *,
        format: str = "jsonl",
    ) -> Iterator[bytes]:
        supplied = dict(filters or {})
        return self.iter_export(
            format=format,
            from_time=supplied.get("from_time", supplied.get("from")),
            to_time=supplied.get("to_time", supplied.get("to")),
            level=supplied.get("level"),
            provider=supplied.get("provider"),
            component=supplied.get("component"),
            operation=supplied.get("operation"),
            steam_id64=supplied.get("steam_id64", supplied.get("steamId")),
            account_id=supplied.get("account_id", supplied.get("accountId")),
            trade_no=supplied.get("trade_no", supplied.get("tradeNo")),
            request_id=supplied.get("request_id", supplied.get("requestId")),
            keyword=supplied.get("keyword"),
            market_hash_name=supplied.get(
                "market_hash_name",
                supplied.get("marketHashName"),
            ),
            operation_id=supplied.get("operation_id", supplied.get("operationId")),
            http_status=supplied.get("http_status", supplied.get("httpStatus")),
            include_steam_scheduler=_as_bool(
                supplied.get(
                    "include_steam_scheduler",
                    supplied.get("includeSteamScheduler"),
                )
            ),
        )


_LOGGER_CACHE: dict[str, GuadaoEventLogger] = {}
_LOGGER_LOCK = threading.Lock()


def reset_guadao_event_loggers() -> None:
    with _LOGGER_LOCK:
        _LOGGER_CACHE.clear()


def get_guadao_event_logger(
    settings: Mapping[str, Any] | None = None,
    *,
    log_dir: str | Path | None = None,
    retention_days: int | None = None,
) -> GuadaoEventLogger:
    supplied = dict(settings or {})
    configured_dir = (
        log_dir
        or supplied.get("log_dir")
        or supplied.get("logDirectory")
        or os.environ.get(GUADAO_LOG_DIR_ENV)
        or DEFAULT_GUADAO_LOG_DIR
    )
    configured_retention = (
        retention_days
        or supplied.get("retention_days")
        or supplied.get("retentionDays")
        or DEFAULT_RETENTION_DAYS
    )
    resolved = Path(configured_dir).resolve(strict=False)
    cache_key = os.path.normcase(str(resolved))
    with _LOGGER_LOCK:
        logger = _LOGGER_CACHE.get(cache_key)
        if logger is None:
            logger = GuadaoEventLogger(
                resolved,
                retention_days=int(configured_retention),
                broker=GuadaoEventBroker(DEFAULT_BROKER_SIZE),
            )
            _LOGGER_CACHE[cache_key] = logger
        return logger


__all__ = [
    "DEFAULT_GUADAO_LOG_DIR",
    "GUADAO_LOG_DIR_ENV",
    "GuadaoEventBroker",
    "GuadaoEventLogger",
    "TelemetryCallback",
    "get_guadao_event_logger",
    "reset_guadao_event_loggers",
]
