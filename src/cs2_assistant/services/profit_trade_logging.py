from __future__ import annotations

import base64
import gzip
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cs2_assistant.config import PROJECT_ROOT


DEFAULT_PROFIT_TRADE_LOG_DIR = PROJECT_ROOT / "logs" / "profit_trade"
PROFIT_TRADE_LOG_DIR_ENV = "CS2_PROFIT_TRADE_LOG_DIR"
DEFAULT_RETENTION_DAYS = 90
DEFAULT_BROKER_SIZE = 2_000
DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 1_000
MAX_SAFE_STRING_LENGTH = 2_000
MAX_STACK_TRACE_LENGTH = 8_000

TelemetryCallback = Callable[[dict[str, Any]], None]

_EVENT_FIELDS = (
    "event_id",
    "timestamp_utc",
    "sequence",
    "level",
    "source",
    "provider",
    "component",
    "operation",
    "message",
    "run_id",
    "trade_id",
    "trade_no",
    "market_hash_name",
    "asset_id",
    "account_id",
    "steam_id64",
    "request_id",
    "client_instance_id",
    "attempt",
    "method",
    "endpoint",
    "status_code",
    "elapsed_ms",
    "retry_after",
    "state_from",
    "state_to",
    "step_from",
    "step_to",
    "exception_type",
    "stack_trace",
    "safe_context",
)

_SENSITIVE_KEY_FRAGMENTS = (
    "apikey",
    "appkey",
    "authorization",
    "cookie",
    "identitysecret",
    "devicesecret",
    "sharedsecret",
    "password",
    "passwd",
    "sessionid",
    "steamguard",
    "steamlogin",
    "styletoken",
    "tradeurl",
    "accesstoken",
    "refreshtoken",
)
_EXACT_SENSITIVE_KEYS = {
    "secret",
    "token",
    "cookies",
    "credentials",
    "auth",
    "body",
    "requestbody",
    "responsebody",
    "formdata",
    "multipart",
    "files",
    "headers",
    "requestheaders",
    "responseheaders",
    "params",
    "queryparams",
}
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(app[-_ ]?key|api[-_ ]?key|authorization|cookie|identity[-_ ]?secret|"
    r"device[-_ ]?secret|shared[-_ ]?secret|password|passwd|sessionid|steam[-_ ]?guard|"
    r"style[-_ ]?token|access[-_ ]?token|refresh[-_ ]?token|token)"
    r"(\s*[=:]\s*)([^&;\s,}\]]+|\"[^\"]*\"|'[^']*')"
)
_TRADE_URL_RE = re.compile(
    r"https?://steamcommunity\.com/tradeoffer/new/\?[^\s\"']+",
    re.IGNORECASE,
)
_LOG_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(?:\.gz)?$")


def _normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    if normalized in _EXACT_SENSITIVE_KEYS:
        return True
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _truncate_text(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated>"


def redact_sensitive_data(
    value: Any,
    *,
    key: str | None = None,
    max_string_length: int = MAX_SAFE_STRING_LENGTH,
    _depth: int = 0,
) -> Any:
    """Return a JSON-safe, recursively redacted copy of telemetry data.

    The logger deliberately accepts only a small root schema.  This helper is
    the second safety boundary for ``safe_context`` and error summaries.  It
    never mutates caller-owned objects.
    """

    if key is not None and _is_sensitive_key(key):
        return "<redacted>"
    if _depth >= 8:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        redacted = _TRADE_URL_RE.sub("<redacted:trade_url>", value)
        redacted = _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", redacted)
        return _truncate_text(redacted, limit=max(64, int(max_string_length)))
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            safe_key = _truncate_text(str(raw_key), limit=120)
            result[safe_key] = redact_sensitive_data(
                raw_value,
                key=safe_key,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            redact_sensitive_data(
                item,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            for item in list(value)[:500]
        ]
    return redact_sensitive_data(
        str(value),
        max_string_length=max_string_length,
        _depth=_depth + 1,
    )


def _utc_timestamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_key(value: Any) -> str:
    try:
        parsed = _parse_utc(str(value or ""))
    except (TypeError, ValueError):
        return ""
    return _utc_timestamp(parsed) if parsed else ""


class ProfitTradeEventBroker:
    """A bounded, in-process event feed suitable for an SSE endpoint."""

    def __init__(self, max_events: int = DEFAULT_BROKER_SIZE) -> None:
        self.max_events = max(1, int(max_events))
        self._events: deque[dict[str, Any]] = deque(maxlen=self.max_events)
        self._condition = threading.Condition()

    def publish(self, event: Mapping[str, Any]) -> None:
        safe_event = dict(event)
        with self._condition:
            self._events.append(safe_event)
            self._condition.notify_all()

    def snapshot(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self._condition:
            events = list(self._events)
        if limit is not None:
            events = events[-max(0, int(limit)) :]
        return [dict(event) for event in events]

    def wait_for_events(
        self,
        *,
        after_event_id: str | None = None,
        after_sequence: int | None = None,
        timeout: float = 15.0,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        """Wait for events newer than a known event id or process sequence."""

        safe_limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while True:
                available = self._events_after_locked(
                    after_event_id=after_event_id,
                    after_sequence=after_sequence,
                )
                if available:
                    return [dict(event) for event in available[:safe_limit]]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)

    def wait_after(
        self,
        last_event_id: str | None = None,
        *,
        timeout: float = 15.0,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        return self.wait_for_events(
            after_event_id=last_event_id,
            timeout=timeout,
            limit=limit,
        )

    def _events_after_locked(
        self,
        *,
        after_event_id: str | None,
        after_sequence: int | None,
    ) -> list[dict[str, Any]]:
        events = list(self._events)
        if after_sequence is not None:
            return [
                event
                for event in events
                if int(event.get("sequence") or 0) > int(after_sequence)
            ]
        if not after_event_id:
            return events
        for index, event in enumerate(events):
            if str(event.get("event_id") or "") == str(after_event_id):
                return events[index + 1 :]
        # The requested id may have fallen out of the bounded buffer.  Returning
        # the retained window lets the SSE client recover instead of hanging.
        return events


class ProfitTradeEventLogger:
    """Fail-open structured event logger dedicated to Profit Trade.

    ``emit`` and ``telemetry_callback`` never raise.  Trading behavior must not
    depend on the availability of the log directory, compression, or the SSE
    broker.
    """

    def __init__(
        self,
        log_dir: str | Path | None = None,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        broker: ProfitTradeEventBroker | None = None,
        broker_max_events: int = DEFAULT_BROKER_SIZE,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        configured_log_dir = log_dir or os.environ.get(PROFIT_TRADE_LOG_DIR_ENV)
        self.log_dir = Path(configured_log_dir or DEFAULT_PROFIT_TRADE_LOG_DIR).resolve(strict=False)
        self.retention_days = max(1, int(retention_days))
        self.broker = broker or ProfitTradeEventBroker(broker_max_events)
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._io_lock = threading.RLock()
        self._maintenance_lock = threading.Lock()
        self._request_activity_lock = threading.Lock()
        self._steam_request_times: deque[float] = deque()
        self._active_steam_requests: set[str] = set()
        self._sequence = 0
        self._last_maintenance_date: date | None = None

    def emit(
        self,
        *,
        level: str = "INFO",
        provider: str = "local",
        component: str,
        operation: str,
        message: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        try:
            now = self._normalized_now()
            event = self._build_event(
                now=now,
                level=level,
                provider=provider,
                component=component,
                operation=operation,
                message=message,
                fields=fields,
            )
        except Exception as exc:  # pragma: no cover - final fail-open boundary
            self._report_internal_failure("build", exc)
            return None

        wrote_event = False
        try:
            self._append_event(event)
            wrote_event = True
        except Exception as exc:
            self._report_internal_failure("write", exc)
        try:
            self.broker.publish(event)
        except Exception as exc:  # pragma: no cover - custom broker failure
            self._report_internal_failure("publish", exc)
        if wrote_event:
            self._schedule_daily_maintenance(now)
        return dict(event)

    def telemetry_callback(self, event: Mapping[str, Any]) -> None:
        """Ingest the safe event dictionary emitted by an instrumented client."""

        try:
            source = str(event.get("source") or "").strip()
            if source != "profit_trade":
                return
            event = self._with_request_activity(event)
            fields = {key: event.get(key) for key in _EVENT_FIELDS if key in event}
            safe_context = fields.pop("safe_context", None)
            fields.pop("source", None)
            for key in ("event_id", "timestamp_utc", "sequence", "level", "provider", "component", "operation", "message"):
                fields.pop(key, None)
            if safe_context is not None:
                fields["safe_context"] = safe_context
            self.emit(
                level=str(event.get("level") or "INFO"),
                provider=str(event.get("provider") or "local"),
                component=str(event.get("component") or "client"),
                operation=str(event.get("operation") or "request"),
                message=str(event.get("message") or "Profit Trade client event"),
                **fields,
            )
        except Exception as exc:  # pragma: no cover - final fail-open boundary
            self._report_internal_failure("ingest", exc)

    def _with_request_activity(self, event: Mapping[str, Any]) -> dict[str, Any]:
        enriched = dict(event)
        if str(enriched.get("provider") or "").lower() != "steam":
            return enriched
        request_id = str(enriched.get("request_id") or "").strip()
        safe_context = enriched.get("safe_context")
        context = dict(safe_context) if isinstance(safe_context, Mapping) else {}
        phase = str(context.get("phase") or "").lower()
        if not request_id or phase not in {"start", "success", "failure"}:
            return enriched
        now_seconds = self._normalized_now().timestamp()
        with self._request_activity_lock:
            cutoff = now_seconds - 300.0
            while self._steam_request_times and self._steam_request_times[0] < cutoff:
                self._steam_request_times.popleft()
            if phase == "start":
                self._steam_request_times.append(now_seconds)
                self._active_steam_requests.add(request_id)
            else:
                self._active_steam_requests.discard(request_id)
            context["request_frequency"] = {
                "last_10_seconds": sum(timestamp >= now_seconds - 10.0 for timestamp in self._steam_request_times),
                "last_60_seconds": sum(timestamp >= now_seconds - 60.0 for timestamp in self._steam_request_times),
                "last_5_minutes": len(self._steam_request_times),
                "current_concurrent": len(self._active_steam_requests),
            }
        enriched["safe_context"] = context
        return enriched

    def bind_telemetry(self, **context: Any) -> TelemetryCallback:
        """Return a callback that adds an explicit Profit Trade call context."""

        safe_context = {
            key: value
            for key, value in context.items()
            if key in _EVENT_FIELDS and key not in {"event_id", "timestamp_utc", "sequence"}
        }

        def callback(event: dict[str, Any]) -> None:
            merged = dict(safe_context)
            merged.update(event)
            merged["source"] = "profit_trade"
            self.telemetry_callback(merged)

        return callback

    def query(
        self,
        filters: Mapping[str, Any] | None = None,
        *,
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
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        supplied = dict(filters or {})
        from_time = from_time if from_time is not None else supplied.get("from_time", supplied.get("from"))
        to_time = to_time if to_time is not None else supplied.get("to_time", supplied.get("to"))
        level = level if level is not None else supplied.get("level")
        provider = provider if provider is not None else supplied.get("provider")
        component = component if component is not None else supplied.get("component")
        operation = operation if operation is not None else supplied.get("operation")
        steam_id64 = steam_id64 if steam_id64 is not None else supplied.get("steam_id64", supplied.get("steamId"))
        account_id = account_id if account_id is not None else supplied.get("account_id", supplied.get("accountId"))
        trade_no = trade_no if trade_no is not None else supplied.get("trade_no", supplied.get("tradeNo"))
        request_id = request_id if request_id is not None else supplied.get("request_id", supplied.get("requestId"))
        keyword = keyword if keyword is not None else supplied.get("keyword")
        cursor = cursor if cursor is not None else supplied.get("cursor")
        if limit is None:
            limit = supplied.get("limit", supplied.get("pageSize", DEFAULT_QUERY_LIMIT))
        safe_limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
        cursor_data = self._decode_cursor(cursor)
        cursor_id = str(cursor_data.get("event_id") or "") if cursor_data else ""
        cursor_timestamp = str(cursor_data.get("timestamp_utc") or "") if cursor_data else ""
        passed_cursor = not bool(cursor_data)
        selected: list[dict[str, Any]] = []
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
        ):
            if not passed_cursor:
                if str(event.get("event_id") or "") == cursor_id:
                    passed_cursor = True
                    continue
                if cursor_timestamp and _timestamp_key(event.get("timestamp_utc")) < cursor_timestamp:
                    passed_cursor = True
                else:
                    continue
            selected.append(event)
            if len(selected) > safe_limit:
                break
        has_more = len(selected) > safe_limit
        events = selected[:safe_limit]
        next_cursor = self._encode_cursor(events[-1]) if has_more and events else None
        return {
            "events": events,
            "nextCursor": next_cursor,
            "hasMore": has_more,
        }

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        target = str(event_id or "").strip()
        if not target:
            return None
        for event in reversed(self.broker.snapshot()):
            if str(event.get("event_id") or "") == target:
                return event
        for event in self._iter_events_newest_first():
            if str(event.get("event_id") or "") == target:
                return event
        return None

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
    ) -> Iterator[bytes]:
        export_format = str(format or "jsonl").strip().lower()
        if export_format not in {"jsonl", "log"}:
            raise ValueError("format must be jsonl or log")
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
        ):
            if export_format == "jsonl":
                line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            else:
                line = self._format_readable_log(event)
            yield f"{line}\n".encode("utf-8")

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
        )

    def wait_after(
        self,
        last_event_id: str | None = None,
        *,
        timeout: float = 15.0,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        return self.broker.wait_after(
            last_event_id,
            timeout=timeout,
            limit=limit,
        )

    def storage_status(self) -> dict[str, Any]:
        files = self._log_files()
        total_bytes = 0
        compressed = 0
        dates: list[str] = []
        for path in files:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
            if path.suffix == ".gz":
                compressed += 1
            match = _LOG_FILE_RE.match(path.name)
            if match:
                dates.append(match.group(1))
        return {
            "logDirectory": str(self.log_dir),
            "retentionDays": self.retention_days,
            "totalBytes": total_bytes,
            "fileCount": len(files),
            "compressedFileCount": compressed,
            "earliestTimestamp": f"{min(dates)}T00:00:00.000Z" if dates else None,
            "latestTimestamp": f"{max(dates)}T23:59:59.999Z" if dates else None,
        }

    def run_maintenance(self, *, now: datetime | None = None) -> None:
        """Compress closed UTC days and remove files outside retention."""

        current = now or self._normalized_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        today = current.date()
        cutoff = today - timedelta(days=self.retention_days - 1)
        with self._maintenance_lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            for path in list(self.log_dir.iterdir()):
                match = _LOG_FILE_RE.match(path.name)
                if not match or not path.is_file():
                    continue
                try:
                    file_date = date.fromisoformat(match.group(1))
                except ValueError:
                    continue
                if file_date < cutoff:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        self._report_internal_failure("retention", exc)
                    continue
                if path.suffix == ".jsonl" and file_date < today:
                    try:
                        self._compress_file(path)
                    except OSError as exc:
                        self._report_internal_failure("compression", exc)

    def _normalized_now(self) -> datetime:
        current = self._now_provider()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _schedule_daily_maintenance(self, now: datetime) -> None:
        with self._maintenance_lock:
            if self._last_maintenance_date == now.date():
                return
            self._last_maintenance_date = now.date()

        def worker() -> None:
            try:
                self.run_maintenance(now=now)
            except Exception as exc:
                self._report_internal_failure("maintenance", exc)

        threading.Thread(
            target=worker,
            name="profit-trade-log-maintenance",
            daemon=True,
        ).start()

    def _build_event(
        self,
        *,
        now: datetime,
        level: str,
        provider: str,
        component: str,
        operation: str,
        message: str,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._io_lock:
            self._sequence += 1
            sequence = self._sequence
        normalized_level = str(level or "INFO").upper()
        if normalized_level == "WARN":
            normalized_level = "WARNING"
        event: dict[str, Any] = {
            "event_id": f"ptlog_{uuid.uuid4().hex}",
            "timestamp_utc": _utc_timestamp(now),
            "sequence": sequence,
            "level": normalized_level,
            "source": "profit_trade",
            "provider": str(provider or "local").lower(),
            "component": _truncate_text(str(component or "unknown"), limit=120),
            "operation": _truncate_text(str(operation or "unknown"), limit=160),
            "message": redact_sensitive_data(str(message or "")),
        }
        for key in _EVENT_FIELDS:
            if key in event or key not in fields:
                continue
            value = fields.get(key)
            if value is None:
                continue
            limit = MAX_STACK_TRACE_LENGTH if key == "stack_trace" else MAX_SAFE_STRING_LENGTH
            event[key] = redact_sensitive_data(value, key=key, max_string_length=limit)
        return event

    def _append_event(self, event: Mapping[str, Any]) -> None:
        day = str(event.get("timestamp_utc") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            day = self._normalized_now().date().isoformat()
        line = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))
        with self._io_lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"{day}.jsonl"
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.write("\n")
                handle.flush()

    def _compress_file(self, path: Path) -> None:
        target = path.with_suffix(f"{path.suffix}.gz")
        if target.exists():
            try:
                with gzip.open(target, "rb") as existing:
                    existing.read(1)
            except (OSError, EOFError):
                return
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        try:
            with path.open("rb") as source, gzip.open(temporary, "wb") as destination:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    destination.write(block)
            os.replace(temporary, target)
            path.unlink()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _log_files(self) -> list[Path]:
        try:
            files = [path for path in self.log_dir.iterdir() if path.is_file() and _LOG_FILE_RE.match(path.name)]
        except OSError:
            return []
        # If a failed/manual recovery leaves both representations, prefer the
        # plain file so events are not returned twice.
        by_date: dict[str, Path] = {}
        for path in sorted(files, key=lambda item: item.name):
            day = path.name[:10]
            current = by_date.get(day)
            if current is None or (current.suffix == ".gz" and path.suffix == ".jsonl"):
                by_date[day] = path
        return sorted(by_date.values(), key=lambda item: item.name)

    def _iter_events_newest_first(self) -> Iterator[dict[str, Any]]:
        for path in reversed(self._log_files()):
            try:
                if path.suffix == ".gz":
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        lines = handle.readlines()
                else:
                    # Capture a complete-line snapshot without holding the
                    # append lock while a potentially large day file is parsed.
                    with self._io_lock:
                        snapshot_size = path.stat().st_size
                    with path.open("rb") as handle:
                        payload = handle.read(snapshot_size)
                    lines = payload.decode("utf-8").splitlines(keepends=True)
            except (OSError, UnicodeError, EOFError):
                continue
            for line in reversed(lines):
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict) and event.get("source") == "profit_trade":
                    yield event

    def _iter_filtered_events(self, **filters: Any) -> Iterator[dict[str, Any]]:
        try:
            start = _parse_utc(filters.get("from_time"))
            end = _parse_utc(filters.get("to_time"))
        except (TypeError, ValueError):
            return
        start_key = _utc_timestamp(start) if start else ""
        end_key = _utc_timestamp(end) if end else ""
        exact_filters = {
            "level": self._as_filter_set(filters.get("level"), upper=True),
            "provider": self._as_filter_set(filters.get("provider")),
            "component": self._as_filter_set(filters.get("component")),
            "operation": self._as_filter_set(filters.get("operation")),
        }
        scalar_filters = {
            "steam_id64": filters.get("steam_id64"),
            "account_id": filters.get("account_id"),
            "trade_no": filters.get("trade_no"),
            "request_id": filters.get("request_id"),
        }
        keyword = str(filters.get("keyword") or "").strip().lower()
        for event in self._iter_events_newest_first():
            timestamp = _timestamp_key(event.get("timestamp_utc"))
            if start_key and timestamp < start_key:
                continue
            if end_key and timestamp > end_key:
                continue
            matched = True
            for key, accepted in exact_filters.items():
                if accepted and str(event.get(key) or "").lower() not in accepted:
                    matched = False
                    break
            if not matched:
                continue
            for key, expected in scalar_filters.items():
                if expected not in (None, "") and str(event.get(key) or "") != str(expected):
                    matched = False
                    break
            if not matched:
                continue
            if keyword:
                haystack = json.dumps(event, ensure_ascii=False, separators=(",", ":")).lower()
                if keyword not in haystack:
                    continue
            yield event

    @staticmethod
    def _as_filter_set(value: str | Iterable[str] | None, *, upper: bool = False) -> set[str]:
        if value is None or value == "":
            return set()
        values = [value] if isinstance(value, str) else list(value)
        normalized = {str(item).strip() for item in values if str(item).strip()}
        if upper:
            return {item.upper().lower() for item in normalized}
        return {item.lower() for item in normalized}

    @staticmethod
    def _encode_cursor(event: Mapping[str, Any]) -> str:
        payload = json.dumps(
            {
                "event_id": event.get("event_id"),
                "timestamp_utc": event.get("timestamp_utc"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
        if not cursor:
            return None
        try:
            raw = str(cursor).encode("ascii")
            raw += b"=" * (-len(raw) % 4)
            payload = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _format_readable_log(event: Mapping[str, Any]) -> str:
        prefix = (
            f"[{event.get('timestamp_utc', '')}] "
            f"{event.get('level', 'INFO')} "
            f"{event.get('provider', 'local')}/{event.get('component', '')} "
            f"{event.get('operation', '')}"
        )
        references = []
        for key in ("trade_no", "request_id", "steam_id64", "status_code", "elapsed_ms"):
            value = event.get(key)
            if value not in (None, ""):
                references.append(f"{key}={value}")
        suffix = f" ({', '.join(references)})" if references else ""
        return f"{prefix}{suffix} - {event.get('message', '')}"

    @staticmethod
    def _report_internal_failure(stage: str, exc: Exception) -> None:
        try:
            summary = redact_sensitive_data(str(exc), max_string_length=400)
            sys.stderr.write(
                f"[profit-trade-log] {stage} failed: {type(exc).__name__}: {summary}\n"
            )
        except Exception:
            pass


_LOGGER_CACHE: dict[str, ProfitTradeEventLogger] = {}
_DEFAULT_LOGGER_LOCK = threading.Lock()


def reset_profit_trade_event_loggers() -> None:
    """Forget cached logger/broker instances after an explicit context change.

    Production code normally never needs this.  Pytest uses it before and
    after installing an isolated log directory so a logger cached by another
    test cannot keep writing to the previous destination.
    """

    with _DEFAULT_LOGGER_LOCK:
        _LOGGER_CACHE.clear()


def get_profit_trade_event_logger(
    settings: Mapping[str, Any] | None = None,
    *,
    log_dir: str | Path | None = None,
    retention_days: int | None = None,
) -> ProfitTradeEventLogger:
    supplied = dict(settings or {})
    custom_log_dir = log_dir or supplied.get("log_dir") or supplied.get("logDirectory")
    custom_retention = retention_days or supplied.get("retention_days") or supplied.get("retentionDays")
    configured_log_dir = custom_log_dir or os.environ.get(PROFIT_TRADE_LOG_DIR_ENV)
    resolved_dir = Path(configured_log_dir or DEFAULT_PROFIT_TRADE_LOG_DIR).resolve(strict=False)
    cache_key = os.path.normcase(str(resolved_dir))
    with _DEFAULT_LOGGER_LOCK:
        logger = _LOGGER_CACHE.get(cache_key)
        if logger is None:
            logger = ProfitTradeEventLogger(
                resolved_dir,
                retention_days=int(custom_retention or DEFAULT_RETENTION_DAYS),
            )
            _LOGGER_CACHE[cache_key] = logger
        return logger


__all__ = [
    "DEFAULT_PROFIT_TRADE_LOG_DIR",
    "PROFIT_TRADE_LOG_DIR_ENV",
    "DEFAULT_RETENTION_DAYS",
    "ProfitTradeEventBroker",
    "ProfitTradeEventLogger",
    "TelemetryCallback",
    "get_profit_trade_event_logger",
    "redact_sensitive_data",
    "reset_profit_trade_event_loggers",
]
