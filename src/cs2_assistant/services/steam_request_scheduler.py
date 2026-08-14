from __future__ import annotations

import time
import threading
import uuid
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import IntEnum
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable
from urllib.parse import urlsplit

from cs2_assistant.services.profit_trade_logging import redact_sensitive_data


T = TypeVar("T")

GLOBAL_CIRCUIT_KEY = "steam:global"
QUIET_WINDOW_CIRCUIT_KEY = "steam:quiet:profit_search_listings"
DEFAULT_ACCOUNT_ROUTE_COOLDOWN_SECONDS = 120.0
DEFAULT_GLOBAL_COOLDOWN_SECONDS = 600.0
DEFAULT_ADMISSION_TIMEOUT_SECONDS = 15.0
DEFAULT_CRITICAL_ADMISSION_TIMEOUT_SECONDS = 30.0
MAX_PARALLEL_OBSERVATION_REQUESTS = 8
ORPHAN_PENDING_STALE_SECONDS = 30.0
ORPHAN_RUNNING_GRACE_SECONDS = 5.0
DEGRADED_GLOBAL_PROBE_SECONDS = 1_800.0
GLOBAL_DEGRADED_AFTER_SECONDS = 3_600.0
RATE_LIMIT_AGGREGATION_SECONDS = 60.0


class SteamRequestPriority(IntEnum):
    """Shared priority classes. A smaller value is more urgent."""

    P0_SAFETY = 0
    P1_EXECUTION = 1
    P2_SYNC = 2
    P3_OBSERVATION = 3


@runtime_checkable
class SteamRequestStore(Protocol):
    """Database contract used by the cross-process scheduler.

    The production ``Database`` owns SQLite transactions.  This protocol keeps
    request governance independent from a particular database implementation
    and makes it testable without network or a real database.
    """

    def enqueue_steam_request(self, **kwargs: Any) -> Any: ...

    def claim_steam_request(self, request_id: str, worker_id: str, **kwargs: Any) -> Any: ...

    def renew_steam_request_lease(self, request_id: str, worker_id: str, **kwargs: Any) -> Any: ...

    def complete_steam_request(self, request_id: str, worker_id: str, **kwargs: Any) -> Any: ...

    def cancel_steam_request(self, request_id: str, reason: str | None = None) -> Any: ...

    def get_steam_request(self, request_id: str) -> Any: ...

    def list_steam_requests(self, **kwargs: Any) -> Any: ...

    def get_steam_queue_snapshot(self) -> Any: ...

    def list_recent_steam_429_events(self, since: str, **kwargs: Any) -> Any: ...

    def get_steam_route_circuit(self, circuit_key: str) -> Any: ...

    def upsert_steam_route_circuit(self, circuit_key: str, **kwargs: Any) -> Any: ...

    def claim_steam_circuit_probe(self, circuit_key: str, worker_id: str, **kwargs: Any) -> Any: ...

    def release_steam_circuit_probe(self, circuit_key: str, worker_id: str, **kwargs: Any) -> Any: ...

    def cancel_orphaned_steam_requests(self, **kwargs: Any) -> Any: ...


class SteamSchedulerError(RuntimeError):
    pass


class SteamRequestTimeout(SteamSchedulerError):
    pass


class SteamRequestGuardRejected(SteamSchedulerError):
    """A last-moment execution gate rejected the remote callback."""

    pass


class SteamCircuitOpen(SteamSchedulerError):
    def __init__(
        self,
        message: str,
        *,
        circuit_key: str,
        retry_at: datetime | None,
        state: str = "open",
    ) -> None:
        super().__init__(message)
        self.circuit_key = circuit_key
        self.retry_at = retry_at
        self.state = state


@dataclass(frozen=True)
class QueuePosition:
    request_id: str
    position: int
    pending_count: int
    running_count: int
    estimated_wait_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "position": self.position,
            "pendingCount": self.pending_count,
            "runningCount": self.running_count,
            "estimatedWaitSeconds": round(self.estimated_wait_seconds, 3),
        }


@dataclass
class SteamRequestPermit(Generic[T]):
    scheduler: "SteamRequestScheduler"
    request_id: str
    source: str
    route: str
    priority: SteamRequestPriority
    account_id: str | None
    method: str
    operation_id: str | None
    probe_keys: tuple[str, ...] = ()
    started_at: datetime | None = None
    _finished: bool = field(default=False, init=False)
    _heartbeat_stop: threading.Event | None = field(default=None, init=False, repr=False)
    _heartbeat_thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def renew(self, lease_seconds: int | None = None) -> bool:
        return self.scheduler.renew(self, lease_seconds=lease_seconds)

    def complete(
        self,
        result: T | None = None,
        *,
        status_code: int | None = None,
        retry_after: Any = None,
    ) -> T | None:
        if self._finished:
            return result
        self._stop_heartbeat()
        resolved_status = status_code
        if resolved_status is None:
            resolved_status = _extract_http_status(result)
        resolved_retry_after = retry_after
        if resolved_retry_after is None:
            resolved_retry_after = _extract_retry_after(result)
        self.scheduler._finish_permit(
            self,
            status=(
                "failed"
                if resolved_status is not None and resolved_status >= 400
                else "completed"
            ),
            http_status=resolved_status,
            result=result,
            retry_after=resolved_retry_after,
        )
        self._finished = True
        return result

    def fail(self, exc: BaseException) -> None:
        if self._finished:
            return
        self._stop_heartbeat()
        self.scheduler._finish_permit(
            self,
            status="failed",
            http_status=_extract_http_status(exc),
            error=exc,
            retry_after=_extract_retry_after(exc),
        )
        self._finished = True

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None:
            return
        stop = threading.Event()
        self._heartbeat_stop = stop
        interval = max(1.0, self.scheduler.lease_seconds / 3.0)

        def heartbeat() -> None:
            try:
                while not stop.wait(interval):
                    try:
                        if not self.renew():
                            return
                    except Exception:
                        return
            finally:
                self.scheduler._close_store_thread_connection()

        thread = threading.Thread(
            target=heartbeat,
            name=f"steam-request-lease-{self.request_id[-8:]}",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        stop = self._heartbeat_stop
        if stop is not None:
            stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)
        self._heartbeat_stop = None
        self._heartbeat_thread = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _payload_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    payload = row.get("payload_json")
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _extract_http_status(value: Any) -> int | None:
    for candidate in (
        getattr(value, "status_code", None),
        getattr(getattr(value, "response", None), "status_code", None),
    ):
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    if isinstance(value, Mapping):
        candidate = value.get("status_code", value.get("statusCode"))
        try:
            return int(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, (tuple, list)) and len(value) >= 2 and value[0] is False:
        status_hint = str(value[1] or "").strip().lower()
        if any(token in status_hint for token in ("429", "throttled", "too many requests")):
            return 429
    if isinstance(value, (tuple, list)) and value and value[0] is True:
        # Steam auth helpers return an explicit application success tuple
        # rather than exposing their internal HTTP response. Treat that
        # explicit success as a successful scheduler probe.
        return 200
    return None


def _extract_retry_after(value: Any) -> Any:
    response = value
    if isinstance(value, BaseException):
        response = getattr(value, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        return headers.get("Retry-After") or headers.get("retry-after")
    if isinstance(value, Mapping):
        return value.get("retry_after", value.get("retryAfter"))
    return None


def parse_retry_after_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - (now or utc_now())).total_seconds())


def normalize_steam_route(endpoint: str) -> str:
    """Collapse dynamic Steam URLs into stable circuit/rate-limit route keys."""

    raw = str(endpoint or "").strip()
    path = urlsplit(raw).path if "://" in raw else raw.split("?", 1)[0]
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if not segments:
        return "steam/root"
    lowered = [segment.lower() for segment in segments]
    if lowered[:2] == ["market", "listings"]:
        return "market/listings"
    if len(lowered) >= 2 and lowered[0] == "market" and lowered[1] in {
        "buylisting",
        "removelisting",
        "createbuyorder",
        "cancelbuyorder",
    }:
        return f"market/{lowered[1]}"
    return "/".join(lowered)


class SteamRequestScheduler:
    """Cooperative, SQLite-backed, cross-process Steam request governor.

    Callers enqueue metadata only; credentials and request bodies must stay in
    the caller closure.  ``claim_steam_request`` is intentionally request-
    specific: it may claim the ticket only when it is the globally highest
    priority available ticket and no valid running lease exists.  This avoids
    one process accidentally claiming a callback owned by another process.
    """

    configured = True

    def __init__(
        self,
        store: SteamRequestStore,
        *,
        worker_id: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        telemetry: Callable[[dict[str, Any]], None] | None = None,
        lease_seconds: int = 30,
        poll_seconds: float = 0.1,
        estimated_request_seconds: float = 1.5,
        quiet_window_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.worker_id = worker_id or f"steam-worker-{uuid.uuid4().hex}"
        self._now_provider = now_provider or utc_now
        self._sleep = sleep or time.sleep
        self._telemetry = telemetry
        self.lease_seconds = max(5, int(lease_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.estimated_request_seconds = max(0.05, float(estimated_request_seconds))
        self.quiet_window_seconds = max(0.0, float(quiet_window_seconds))

    def execute(
        self,
        request: Callable[[], T],
        *,
        source: str,
        route: str,
        priority: SteamRequestPriority | int,
        account_id: str | None = None,
        method: str = "GET",
        operation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
        quiet_before: bool | None = None,
        bounded_retry: bool = False,
        execution_guard: Callable[[], bool] | None = None,
    ) -> T:
        with self.request_context(
            source=source,
            route=route,
            priority=priority,
            account_id=account_id,
            method=method,
            operation_id=operation_id,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
            quiet_before=quiet_before,
            bounded_retry=bounded_retry,
        ) as permit:
            try:
                if execution_guard is not None:
                    try:
                        allowed = bool(execution_guard())
                    except Exception as exc:
                        raise SteamRequestGuardRejected(
                            "Steam execution guard failed before the remote callback"
                        ) from exc
                    if not allowed:
                        raise SteamRequestGuardRejected(
                            "Steam execution guard rejected the remote callback"
                        )
                result = request()
            except BaseException as exc:
                permit.fail(exc)
                raise
            permit.complete(result)
            return result

    def call(
        self,
        *,
        method: str,
        url: str,
        callback: Callable[[], T],
        account: str | None = None,
        route: str | None = None,
        priority: SteamRequestPriority | int = SteamRequestPriority.P3_OBSERVATION,
        source: str = "unknown",
        operation: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
        quiet_before: bool | None = None,
        bounded_retry: bool = False,
        execution_guard: Callable[[], bool] | None = None,
    ) -> T:
        """Convenient adapter for ``requests.Session.request`` wrappers."""

        return self.execute(
            callback,
            source=source,
            route=route or url,
            priority=priority,
            account_id=account,
            method=method,
            operation_id=operation,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
            quiet_before=quiet_before,
            bounded_retry=bounded_retry,
            execution_guard=execution_guard,
        )

    @contextmanager
    def request_context(
        self,
        *,
        source: str,
        route: str,
        priority: SteamRequestPriority | int,
        account_id: str | None = None,
        method: str = "GET",
        operation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
        quiet_before: bool | None = None,
        bounded_retry: bool = False,
    ) -> Iterator[SteamRequestPermit[Any]]:
        safe_priority = SteamRequestPriority(int(priority))
        safe_route = normalize_steam_route(route)
        admission_timeout = (
            (
                DEFAULT_CRITICAL_ADMISSION_TIMEOUT_SECONDS
                if safe_priority <= SteamRequestPriority.P1_EXECUTION
                else DEFAULT_ADMISSION_TIMEOUT_SECONDS
            )
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        admission_deadline = self._now() + timedelta(seconds=admission_timeout)
        should_quiet = quiet_before
        if should_quiet is None:
            should_quiet = str(source) == "profit_trade" and safe_route == "market/listings"
        if should_quiet and self.quiet_window_seconds > 0:
            self._prepare_quiet_window(
                self.quiet_window_seconds,
                deadline=admission_deadline,
            )

        # The first pass prevents a blocked request from entering the queue,
        # but deliberately does not reserve a half-open probe while the ticket
        # may still wait behind another process. Admission is checked again
        # immediately before the global request lease is claimed.
        self._await_circuit_admission(
            priority=safe_priority,
            account_id=account_id,
            route=safe_route,
            deadline=admission_deadline,
            source=str(source or "unknown"),
            bounded_retry=bool(bounded_retry),
            claim_probes=False,
        )
        probe_keys: list[str] = []
        request_id = f"steamreq_{uuid.uuid4().hex}"
        safe_metadata = redact_sensitive_data(dict(metadata or {}))
        if isinstance(safe_metadata, dict) and bounded_retry:
            safe_metadata["boundedRetry"] = True
        if isinstance(safe_metadata, dict):
            # The creator worker is written onto the ticket so that an
            # abandoned pending row (e.g. after a process restart) can be
            # identified and its orphan cleanup confirmed in diagnostics.
            safe_metadata["schedulerCreatorWorkerId"] = self.worker_id
        self.store.enqueue_steam_request(
            request_id=request_id,
            source=str(source or "unknown"),
            route=safe_route,
            priority=int(safe_priority),
            account_id=account_id,
            method=str(method or "GET").upper(),
            operation_id=operation_id,
            payload=safe_metadata,
            available_at=_iso(self._now()),
        )
        # Queue time starts when the ticket actually exists. Circuit admission
        # and a listings quiet window must not silently consume the queue's
        # entire timeout before the request can even be claimed.
        queue_deadline = self._now() + timedelta(seconds=admission_timeout)
        position = self.queue_position(request_id)
        self._emit(
            source=source,
            phase="queued",
            request_id=request_id,
            route=safe_route,
            priority=int(safe_priority),
            account_id=account_id,
            method=method,
            operation_id=operation_id,
            queue=position.as_dict(),
            bounded_retry=bool(bounded_retry),
        )
        try:
            claimed, probe_keys, request_id = self._wait_and_claim(
                request_id,
                deadline=queue_deadline,
                priority=safe_priority,
                account_id=account_id,
                route=safe_route,
                source=str(source or "unknown"),
                method=str(method or "GET").upper(),
                operation_id=operation_id,
                payload=safe_metadata if isinstance(safe_metadata, Mapping) else {},
                bounded_retry=bool(bounded_retry),
            )
        except BaseException:
            self.store.cancel_steam_request(request_id, reason="queue_wait_aborted")
            self._release_probes(probe_keys, success=False)
            raise
        if not claimed:
            self._release_probes(probe_keys, success=False)
            raise SteamRequestTimeout(f"Steam request {request_id} could not acquire the global lease")

        permit = SteamRequestPermit(
            scheduler=self,
            request_id=request_id,
            source=str(source or "unknown"),
            route=safe_route,
            priority=safe_priority,
            account_id=account_id,
            method=str(method or "GET").upper(),
            operation_id=operation_id,
            probe_keys=tuple(probe_keys),
            started_at=self._now(),
        )
        permit._start_heartbeat()
        self._emit(
            source=source,
            phase="start",
            request_id=request_id,
            route=safe_route,
            priority=int(safe_priority),
            account_id=account_id,
            method=method,
            operation_id=operation_id,
            request_frequency=self._request_frequency(),
            bounded_retry=bool(bounded_retry),
        )
        try:
            yield permit
        except BaseException as exc:
            permit.fail(exc)
            raise
        finally:
            if not permit._finished:
                permit.complete()
            self._close_store_thread_connection()

    def acquire(self, **kwargs: Any) -> Any:
        """Alias used by low-level clients that prefer an explicit permit."""

        return self.request_context(**kwargs)

    def renew(self, permit: SteamRequestPermit[Any], *, lease_seconds: int | None = None) -> bool:
        result = self.store.renew_steam_request_lease(
            permit.request_id,
            self.worker_id,
            lease_seconds=max(5, int(lease_seconds or self.lease_seconds)),
            now=_iso(self._now()),
        )
        return bool(result)

    def queue_position(self, request_id: str) -> QueuePosition:
        rows = [_row_dict(row) for row in self.store.list_steam_requests(limit=2_000)]
        running = [row for row in rows if str(row.get("status") or "") == "running"]
        pending = [row for row in rows if str(row.get("status") or "") in {"pending", "queued"}]
        pending.sort(
            key=lambda row: (
                int(row.get("priority") if row.get("priority") is not None else 99),
                str(row.get("created_at") or row.get("createdAt") or ""),
                str(row.get("request_id") or row.get("requestId") or ""),
            )
        )
        position = 0
        for index, row in enumerate(pending, start=1):
            if str(row.get("request_id") or row.get("requestId") or "") == request_id:
                position = index
                break
        ahead = len(running) + max(0, position - 1)
        return QueuePosition(
            request_id=request_id,
            position=position,
            pending_count=len(pending),
            running_count=len(running),
            estimated_wait_seconds=ahead * self.estimated_request_seconds,
        )

    def queue_snapshot(self) -> dict[str, Any]:
        snapshot = self.store.get_steam_queue_snapshot()
        return redact_sensitive_data(_row_dict(snapshot))

    def record_429(
        self,
        *,
        account_id: str | None,
        route: str,
        retry_after: Any = None,
    ) -> dict[str, Any]:
        now = self._now()
        safe_route = normalize_steam_route(route)
        route_key = self._account_route_circuit_key(account_id, safe_route)
        existing = _row_dict(self.store.get_steam_route_circuit(route_key))
        first_429 = _as_utc(existing.get("first_429_at")) or now
        route_delay = parse_retry_after_seconds(retry_after, now=now)
        if route_delay is None:
            route_delay = DEFAULT_ACCOUNT_ROUTE_COOLDOWN_SECONDS
        route_until = now + timedelta(seconds=route_delay)
        self.store.upsert_steam_route_circuit(
            route_key,
            scope="account_route",
            state="open",
            account_id=account_id,
            route=safe_route,
            consecutive_429=int(existing.get("consecutive_429") or 0) + 1,
            first_429_at=_iso(first_429),
            last_429_at=_iso(now),
            cooldown_until=_iso(route_until),
            next_probe_at=_iso(route_until),
            payload={"retryAfter": retry_after, "cooldownSeconds": route_delay},
        )

        recent_rows = [
            _row_dict(row)
            for row in self.store.list_recent_steam_429_events(
                _iso(now - timedelta(seconds=RATE_LIMIT_AGGREGATION_SECONDS)),
                limit=1_000,
            )
        ]
        # A store may expose only already-committed events. Ensure the current
        # observation still participates in aggregation exactly once.
        if not any(
            str(row.get("request_id") or "") == str(getattr(self, "_last_completed_request_id", ""))
            for row in recent_rows
        ):
            recent_rows.append({"account_id": account_id, "route": safe_route, "http_status": 429})
        accounts = {str(row.get("account_id")) for row in recent_rows if row.get("account_id")}
        routes = {
            normalize_steam_route(str(row.get("route") or ""))
            for row in recent_rows
            if row.get("route")
        }
        global_existing = _row_dict(self.store.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY))
        global_state = str(global_existing.get("state") or "closed").lower()
        global_is_active = global_state in {"open", "half_open"}
        listings_only_failure = bool(routes) and routes == {"market/listings"}
        should_open_global = (
            (
                not listings_only_failure
                and (
                    len(recent_rows) >= 3
                    or len(accounts) >= 2
                    or len(routes) >= 2
                )
            )
            # A failed half-open recovery probe reopens the existing global
            # circuit even when the new 60-second window has only one event.
            or global_is_active
        )
        first_candidates = [
            value
            for value in (
                (
                    _as_utc(global_existing.get("first_429_at"))
                    if global_is_active
                    else None
                ),
                first_429,
                *(
                    _as_utc(row.get("completed_at") or row.get("last_429_at"))
                    for row in recent_rows
                ),
            )
            if value is not None
        ]
        global_first = min(first_candidates) if first_candidates else now
        already_degraded = (
            global_is_active
            and (now - global_first).total_seconds() >= GLOBAL_DEGRADED_AFTER_SECONDS
        )
        global_until: datetime | None = None
        if should_open_global:
            global_delay = (
                DEGRADED_GLOBAL_PROBE_SECONDS
                if already_degraded
                else DEFAULT_GLOBAL_COOLDOWN_SECONDS
            )
            if retry_after not in (None, ""):
                global_delay = max(global_delay, route_delay)
            global_until = now + timedelta(seconds=global_delay)
            self.store.upsert_steam_route_circuit(
                GLOBAL_CIRCUIT_KEY,
                scope="global",
                state="open",
                account_id=None,
                route=None,
                consecutive_429=int(global_existing.get("consecutive_429") or 0) + 1,
                first_429_at=_iso(global_first),
                last_429_at=_iso(now),
                cooldown_until=_iso(global_until),
                next_probe_at=_iso(global_until),
                payload={
                    "windowSeconds": RATE_LIMIT_AGGREGATION_SECONDS,
                    "eventCount": len(recent_rows),
                    "accountCount": len(accounts),
                    "routeCount": len(routes),
                    "listingsOnlyFailure": listings_only_failure,
                    "degradedProbe": already_degraded,
                },
            )
        return {
            "routeCircuitKey": route_key,
            "routeCooldownUntil": _iso(route_until),
            "globalCircuitOpened": should_open_global,
            "globalCooldownUntil": _iso(global_until) if global_until else None,
            "recent429Count": len(recent_rows),
            "recentAccountCount": len(accounts),
            "recentRouteCount": len(routes),
        }

    def _wait_and_claim(
        self,
        request_id: str,
        *,
        deadline: datetime | None,
        priority: SteamRequestPriority,
        account_id: str | None,
        route: str,
        source: str,
        method: str,
        operation_id: str | None,
        payload: Mapping[str, Any],
        bounded_retry: bool,
    ) -> tuple[bool, list[str], str]:
        current_request_id = request_id
        while True:
            self._cleanup_orphaned_requests()
            # A ticket may have been queued before Profit Trade opened its
            # listings quiet window or before a circuit was opened by another
            # process. Re-run admission before every attempt to own the global
            # request lease so an already queued P2/P3 request cannot slip in.
            probe_keys = self._await_circuit_admission(
                priority=priority,
                account_id=account_id,
                route=route,
                deadline=deadline,
                source=source,
                bounded_retry=bounded_retry,
                claim_probes=True,
            )
            try:
                requested_parallel_limit = int(
                    payload.get("schedulerParallelLimit") or 1
                )
            except (TypeError, ValueError, OverflowError):
                requested_parallel_limit = 1
            claimed = self.store.claim_steam_request(
                current_request_id,
                self.worker_id,
                lease_seconds=self.lease_seconds,
                now=_iso(self._now()),
                parallel_group=(
                    str(payload.get("schedulerParallelGroup") or "").strip()
                    or None
                ),
                parallel_limit=min(
                    MAX_PARALLEL_OBSERVATION_REQUESTS,
                    max(1, requested_parallel_limit),
                ),
                account_exclusive=bool(
                    payload.get("schedulerAccountExclusive")
                ),
            )
            if claimed:
                try:
                    # Close the cross-process race between the pre-claim
                    # admission read and the SQLite lease claim. If a quiet
                    # window/circuit appeared in that interval, do not run the
                    # callback and do not keep the global lease while waiting.
                    self._claim_required_probes(
                        priority=priority,
                        account_id=account_id,
                        route=route,
                        source=source,
                        bounded_retry=bounded_retry,
                        claim_probes=False,
                        owned_probe_keys=set(probe_keys),
                    )
                except SteamCircuitOpen as exc:
                    self._release_unspent_probes(probe_keys)
                    self.store.complete_steam_request(
                        current_request_id,
                        self.worker_id,
                        status="cancelled",
                        error=f"admission_changed:{exc.circuit_key}",
                        now=_iso(self._now()),
                    )
                    if deadline is not None and self._now() >= deadline:
                        raise SteamRequestTimeout(
                            f"Steam request {current_request_id} lost admission at its deadline"
                        ) from exc
                    previous_request_id = current_request_id
                    current_request_id = f"steamreq_{uuid.uuid4().hex}"
                    self.store.enqueue_steam_request(
                        request_id=current_request_id,
                        source=source,
                        route=route,
                        priority=int(priority),
                        account_id=account_id,
                        method=method,
                        operation_id=operation_id,
                        payload=dict(payload),
                        available_at=_iso(self._now()),
                    )
                    self._emit(
                        source=source,
                        phase="requeued",
                        request_id=current_request_id,
                        previous_request_id=previous_request_id,
                        route=route,
                        priority=int(priority),
                        account_id=account_id,
                        method=method,
                        operation_id=operation_id,
                        circuit_key=exc.circuit_key,
                        bounded_retry=bounded_retry,
                    )
                    continue
                return True, probe_keys, current_request_id
            # No Steam callback ran, so a half-open probe reserved during the
            # second admission must not be held while another queue ticket or
            # running lease remains ahead of us.
            self._release_unspent_probes(probe_keys)
            row = _row_dict(self.store.get_steam_request(current_request_id))
            if str(row.get("status") or "") in {"cancelled", "canceled", "failed"}:
                return False, [], current_request_id
            if deadline is not None and self._now() >= deadline:
                self.store.cancel_steam_request(current_request_id, reason="queue_timeout")
                raise SteamRequestTimeout(f"Steam request {current_request_id} timed out in queue")
            self._sleep(self.poll_seconds)

    def _finish_permit(
        self,
        permit: SteamRequestPermit[Any],
        *,
        status: str,
        http_status: int | None,
        result: Any = None,
        error: BaseException | None = None,
        retry_after: Any = None,
    ) -> None:
        safe_result: Any = None
        if isinstance(result, Mapping):
            safe_result = redact_sensitive_data(result)
        safe_error = redact_sensitive_data(str(error)) if error is not None else None
        self.store.complete_steam_request(
            permit.request_id,
            self.worker_id,
            status=status,
            http_status=http_status,
            result=safe_result,
            error=safe_error,
        )
        self._last_completed_request_id = permit.request_id
        circuit_change: dict[str, Any] | None = None
        if http_status == 429:
            # Drop probe leases first; record_429 then writes the authoritative
            # Retry-After/global cooldown and must be the final circuit write.
            self._release_probes(permit.probe_keys, success=False)
            circuit_change = self.record_429(
                account_id=permit.account_id,
                route=permit.route,
                retry_after=retry_after,
            )
        else:
            probe_succeeded = bool(
                error is None
                and http_status is not None
                and 200 <= int(http_status) < 300
            )
            self._release_probes(permit.probe_keys, success=probe_succeeded)
        self._emit(
            source=permit.source,
            phase=(
                "failure"
                if error is not None or (http_status is not None and http_status >= 400)
                else "success"
            ),
            request_id=permit.request_id,
            route=permit.route,
            priority=int(permit.priority),
            account_id=permit.account_id,
            method=permit.method,
            operation_id=permit.operation_id,
            status_code=http_status,
            retry_after=retry_after,
            error=safe_error,
            circuit=circuit_change,
            elapsed_ms=(
                round((self._now() - permit.started_at).total_seconds() * 1000, 3)
                if permit.started_at is not None
                else None
            ),
            request_frequency=self._request_frequency(),
        )

    def _await_circuit_admission(
        self,
        *,
        priority: SteamRequestPriority,
        account_id: str | None,
        route: str,
        deadline: datetime | None,
        source: str = "unknown",
        bounded_retry: bool = False,
        claim_probes: bool = True,
        owned_probe_keys: set[str] | None = None,
    ) -> list[str]:
        while True:
            try:
                return self._claim_required_probes(
                    priority=priority,
                    account_id=account_id,
                    route=route,
                    source=source,
                    bounded_retry=bounded_retry,
                    claim_probes=claim_probes,
                    owned_probe_keys=owned_probe_keys,
                )
            except SteamCircuitOpen as exc:
                now = self._now()
                if deadline is not None and (exc.retry_at is None or exc.retry_at > deadline):
                    raise SteamRequestTimeout(
                        f"Steam request blocked by {exc.circuit_key} beyond its deadline"
                    ) from exc
                delay = self.poll_seconds
                if exc.retry_at is not None:
                    delay = max(self.poll_seconds, min(1.0, (exc.retry_at - now).total_seconds()))
                self._sleep(delay)

    def _claim_required_probes(
        self,
        *,
        priority: SteamRequestPriority,
        account_id: str | None,
        route: str,
        source: str = "unknown",
        bounded_retry: bool = False,
        claim_probes: bool = True,
        owned_probe_keys: set[str] | None = None,
    ) -> list[str]:
        now = self._now()
        if int(priority) >= int(SteamRequestPriority.P2_SYNC):
            quiet = _row_dict(self.store.get_steam_route_circuit(QUIET_WINDOW_CIRCUIT_KEY))
            quiet_until = _as_utc(quiet.get("cooldown_until") or quiet.get("next_probe_at"))
            if str(quiet.get("state") or "closed") == "open" and quiet_until and quiet_until > now:
                raise SteamCircuitOpen(
                    "Profit Trade listings quiet window is active",
                    circuit_key=QUIET_WINDOW_CIRCUIT_KEY,
                    retry_at=quiet_until,
                )

        probe_keys: list[str] = []
        # P0 is reserved for safety/terminal-state work such as confirming a
        # cancellation or sale. It may bypass a global circuit raised by an
        # unrelated account or route, but it must still obey its own
        # account+route Retry-After circuit below. P1/P2/P3 remain globally
        # gated.
        circuit_keys = (
            []
            if priority == SteamRequestPriority.P0_SAFETY
            else [GLOBAL_CIRCUIT_KEY]
        )
        # Only the explicitly bounded Profit Trade listings retry chain may
        # bypass its account+route cooldown. Listings-only 429s never open the
        # global circuit; buy/create/sell routes remain independent. All other
        # P1 actions must still respect their own account+route cooldown.
        is_bounded_profit_listings_retry = (
            bool(bounded_retry)
            and str(source) == "profit_trade"
            and normalize_steam_route(route) == "market/listings"
        )
        account_route_key = self._account_route_circuit_key(account_id, route)
        circuit_keys.append(account_route_key)
        owned = owned_probe_keys or set()
        for key in circuit_keys:
            circuit = _row_dict(self.store.get_steam_route_circuit(key))
            if key == account_route_key and is_bounded_profit_listings_retry:
                retry_after = _payload_dict(circuit).get("retryAfter")
                # The local 2s/4s bounded chain may bypass only the scheduler's
                # fallback cooldown. An explicit server Retry-After is
                # authoritative and must never be shortened.
                if retry_after in (None, ""):
                    continue
            state = str(circuit.get("state") or "closed").lower()
            if state in {"", "closed"}:
                continue
            if (
                key in owned
                and state == "half_open"
                and str(circuit.get("probe_lease_owner") or "") == self.worker_id
            ):
                continue
            retry_at = _as_utc(circuit.get("next_probe_at") or circuit.get("cooldown_until"))
            if state == "open" and retry_at and retry_at > now:
                self._release_unspent_probes(probe_keys)
                raise SteamCircuitOpen(
                    f"Steam circuit {key} is cooling down",
                    circuit_key=key,
                    retry_at=retry_at,
                )
            if not claim_probes:
                if state == "half_open":
                    lease_until = _as_utc(circuit.get("probe_lease_expires_at"))
                    lease_owner = str(circuit.get("probe_lease_owner") or "").strip()
                    if lease_owner and (lease_until is None or lease_until > now):
                        self._release_unspent_probes(probe_keys)
                        raise SteamCircuitOpen(
                            f"Steam circuit {key} already has a half-open probe",
                            circuit_key=key,
                            retry_at=lease_until or now + timedelta(seconds=self.poll_seconds),
                            state="half_open",
                        )
                # The request may queue, but the unique half-open probe is not
                # reserved until the ticket is about to claim the global lease.
                continue
            if state == "open":
                self.store.upsert_steam_route_circuit(
                    key,
                    scope=str(circuit.get("scope") or "account_route"),
                    state="half_open",
                    account_id=circuit.get("account_id"),
                    route=circuit.get("route"),
                    consecutive_429=int(circuit.get("consecutive_429") or 0),
                    first_429_at=circuit.get("first_429_at"),
                    last_429_at=circuit.get("last_429_at"),
                    cooldown_until=circuit.get("cooldown_until"),
                    next_probe_at=_iso(now),
                    payload=circuit.get("payload"),
                )
            claimed = self.store.claim_steam_circuit_probe(
                key,
                self.worker_id,
                lease_seconds=self.lease_seconds,
                now=_iso(now),
            )
            if not claimed:
                self._release_unspent_probes(probe_keys)
                raise SteamCircuitOpen(
                    f"Steam circuit {key} already has a half-open probe",
                    circuit_key=key,
                    retry_at=now + timedelta(seconds=self.poll_seconds),
                    state="half_open",
                )
            probe_keys.append(key)
        return probe_keys

    def _release_unspent_probes(self, probe_keys: list[str]) -> None:
        """Release admission probes when no Steam request was attempted."""

        now = _iso(self._now())
        for key in probe_keys:
            try:
                self.store.release_steam_circuit_probe(
                    key,
                    self.worker_id,
                    state="open",
                    cooldown_until=now,
                    next_probe_at=now,
                    reason="probe_not_executed",
                )
            except Exception:
                pass

    def _prepare_quiet_window(self, seconds: float, *, deadline: datetime | None) -> None:
        now = self._now()
        quiet_until = now + timedelta(seconds=max(0.0, seconds))
        if deadline is not None and quiet_until > deadline:
            raise SteamRequestTimeout("Profit Trade quiet window exceeds request deadline")
        self.store.upsert_steam_route_circuit(
            QUIET_WINDOW_CIRCUIT_KEY,
            scope="quiet",
            state="open",
            account_id=None,
            route="market/listings",
            consecutive_429=0,
            first_429_at=None,
            last_429_at=None,
            cooldown_until=_iso(quiet_until),
            next_probe_at=_iso(quiet_until),
            payload={"owner": self.worker_id, "seconds": seconds},
        )
        self._emit(
            source="profit_trade",
            phase="quiet_window",
            route="market/listings",
            duration_seconds=seconds,
            quiet_until=_iso(quiet_until),
        )
        remaining = (quiet_until - self._now()).total_seconds()
        if remaining > 0:
            self._sleep(remaining)
        latest = _row_dict(self.store.get_steam_route_circuit(QUIET_WINDOW_CIRCUIT_KEY))
        latest_payload = latest.get("payload")
        if not isinstance(latest_payload, Mapping):
            latest_payload = latest.get("payload_json")
            if isinstance(latest_payload, str):
                try:
                    import json

                    latest_payload = json.loads(latest_payload)
                except (TypeError, ValueError):
                    latest_payload = {}
        if not isinstance(latest_payload, Mapping) or latest_payload.get("owner") == self.worker_id:
            self.store.upsert_steam_route_circuit(
                QUIET_WINDOW_CIRCUIT_KEY,
                scope="quiet",
                state="closed",
                account_id=None,
                route="market/listings",
                consecutive_429=0,
                first_429_at=None,
                last_429_at=None,
                cooldown_until=None,
                next_probe_at=None,
                payload={"owner": self.worker_id},
            )

    def _release_probes(
        self,
        probe_keys: tuple[str, ...] | list[str],
        *,
        success: bool | None,
    ) -> None:
        for key in probe_keys:
            try:
                if success is None:
                    self.store.release_steam_circuit_probe(key, self.worker_id)
                elif success:
                    released = self.store.release_steam_circuit_probe(
                        key,
                        self.worker_id,
                        state="closed",
                        cooldown_until=None,
                        next_probe_at=None,
                    )
                    if released:
                        circuit = _row_dict(self.store.get_steam_route_circuit(key))
                        self.store.upsert_steam_route_circuit(
                            key,
                            scope=str(circuit.get("scope") or "account_route"),
                            state="closed",
                            account_id=circuit.get("account_id"),
                            route=circuit.get("route"),
                            consecutive_429=0,
                            first_429_at=None,
                            last_429_at=None,
                            cooldown_until=None,
                            next_probe_at=None,
                            reason="probe_recovered",
                            payload={"recoveredAt": _iso(self._now())},
                        )
                else:
                    retry_at = self._now() + timedelta(
                        seconds=DEFAULT_ACCOUNT_ROUTE_COOLDOWN_SECONDS
                    )
                    self.store.release_steam_circuit_probe(
                        key,
                        self.worker_id,
                        state="open",
                        cooldown_until=_iso(retry_at),
                        next_probe_at=_iso(retry_at),
                    )
            except Exception:
                # Probe cleanup must not replace the original request result.
                pass

    def _emit(self, **event: Any) -> None:
        if self._telemetry is None:
            return
        try:
            safe_event = redact_sensitive_data(event)
            self._telemetry(dict(safe_event) if isinstance(safe_event, Mapping) else {})
        except Exception:
            # Observability is fail-open by project policy.
            pass

    def _request_frequency(self) -> dict[str, int]:
        now = self._now()
        rows = [_row_dict(row) for row in self.store.list_steam_requests(limit=2_000)]
        created_times = [
            parsed
            for row in rows
            if (parsed := _as_utc(row.get("created_at") or row.get("createdAt"))) is not None
        ]
        return {
            "last10Seconds": sum(value >= now - timedelta(seconds=10) for value in created_times),
            "last60Seconds": sum(value >= now - timedelta(seconds=60) for value in created_times),
            "last5Minutes": sum(value >= now - timedelta(minutes=5) for value in created_times),
            "currentConcurrent": sum(
                str(row.get("status") or "") == "running" for row in rows
            ),
        }

    def _now(self) -> datetime:
        current = self._now_provider()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _close_store_thread_connection(self) -> None:
        close = getattr(self.store, "close_thread_connection", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _cleanup_orphaned_requests(self) -> None:
        cleanup = getattr(self.store, "cancel_orphaned_steam_requests", None)
        if not callable(cleanup):
            return
        cleanup(
            now=_iso(self._now()),
            pending_stale_seconds=ORPHAN_PENDING_STALE_SECONDS,
            running_grace_seconds=ORPHAN_RUNNING_GRACE_SECONDS,
        )

    @staticmethod
    def _account_route_circuit_key(account_id: str | None, route: str) -> str:
        return f"steam:account:{account_id or '_'}:route:{normalize_steam_route(route)}"


class DirectSteamRequestScheduler:
    """Compatibility facade used until the shared runtime is configured."""

    configured = False

    def execute(
        self,
        request: Callable[[], T],
        **kwargs: Any,
    ) -> T:
        execution_guard = kwargs.get("execution_guard")
        if execution_guard is not None:
            try:
                allowed = bool(execution_guard())
            except Exception as exc:
                raise SteamRequestGuardRejected(
                    "Steam execution guard failed before the remote callback"
                ) from exc
            if not allowed:
                raise SteamRequestGuardRejected(
                    "Steam execution guard rejected the remote callback"
                )
        return request()

    def call(
        self,
        *,
        callback: Callable[[], T],
        **kwargs: Any,
    ) -> T:
        return self.execute(callback, **kwargs)

    @contextmanager
    def request_context(self, **_: Any) -> Iterator["DirectSteamRequestPermit"]:
        yield DirectSteamRequestPermit()

    def acquire(self, **kwargs: Any) -> Any:
        return self.request_context(**kwargs)


class DirectSteamRequestPermit:
    """No-op permit matching the configured scheduler's context API."""

    def renew(self, lease_seconds: int | None = None) -> bool:
        return True

    def complete(self, result: T | None = None, **_: Any) -> T | None:
        return result

    def fail(self, exc: BaseException) -> None:
        return None


class _ThreadLocalDatabaseStore:
    """Give every backend thread one SQLite connection to the shared DB.

    ``Database`` uses SQLite's thread affinity. A thread-local connection keeps
    that guarantee without reopening SQLite for every queue phase. The
    scheduler explicitly closes the owning thread's connection after each
    governed Steam request, so Windows shutdown/tests retain no file handles.
    """

    def __init__(self, path: Any) -> None:
        from pathlib import Path

        from cs2_assistant.db import Database

        self.path = Path(path)
        bootstrap = Database(self.path)
        try:
            bootstrap.initialize()
        finally:
            bootstrap.close()
        self._local = threading.local()

    def _database(self) -> Any:
        database = getattr(self._local, "database", None)
        if database is None:
            from cs2_assistant.db import Database

            database = Database(self.path)
            self._local.database = database
        return database

    def __getattr__(self, name: str) -> Any:
        return getattr(self._database(), name)

    def close(self) -> None:
        self.close_thread_connection()

    def close_thread_connection(self) -> None:
        database = getattr(self._local, "database", None)
        if database is not None:
            database.close()
            self._local.database = None


_SHARED_LOCK = threading.RLock()
_DIRECT_SCHEDULER = DirectSteamRequestScheduler()
_SHARED_SCHEDULER: SteamRequestScheduler | None = None
_SHARED_OWNED_STORE: Any = None


def _logger_telemetry(logger: Any) -> Callable[[dict[str, Any]], None] | None:
    if logger is None:
        return None
    if callable(logger):
        return logger
    callback = getattr(logger, "telemetry_callback", None)
    if not callable(callback):
        return None

    def emit(event: dict[str, Any]) -> None:
        source = str(event.get("source") or "unknown")
        phase = str(event.get("phase") or "request")
        callback(
            {
                "source": source,
                "provider": "steam",
                "component": "steam_request_scheduler",
                "operation": f"request_{phase}",
                "message": f"Steam shared request {phase}",
                "request_id": event.get("request_id"),
                "account_id": event.get("account_id"),
                "method": event.get("method"),
                "endpoint": event.get("route"),
                "status_code": event.get("status_code"),
                "elapsed_ms": event.get("elapsed_ms"),
                "retry_after": event.get("retry_after"),
                "safe_context": event,
            }
        )

    return emit


def configure_shared_steam_scheduler(
    db_path: Any,
    logger: Any = None,
    **scheduler_options: Any,
) -> SteamRequestScheduler:
    """Configure the process facade against the shared SQLite database.

    ``db_path`` may be a filesystem path or an already initialized Database-
    compatible store.  Importing this module never opens SQLite; configuration
    remains an explicit backend/CLI startup action.
    """

    global _SHARED_SCHEDULER, _SHARED_OWNED_STORE
    with _SHARED_LOCK:
        previous = _SHARED_OWNED_STORE
        if hasattr(db_path, "enqueue_steam_request"):
            store = db_path
            owned = None
        else:
            store = _ThreadLocalDatabaseStore(db_path)
            owned = store
        cleanup = getattr(store, "cancel_orphaned_steam_requests", None)
        if callable(cleanup):
            cleanup(
                pending_stale_seconds=ORPHAN_PENDING_STALE_SECONDS,
                running_grace_seconds=ORPHAN_RUNNING_GRACE_SECONDS,
            )
        scheduler = SteamRequestScheduler(
            store,
            telemetry=_logger_telemetry(logger),
            **scheduler_options,
        )
        _SHARED_SCHEDULER = scheduler
        _SHARED_OWNED_STORE = owned
        if previous is not None and previous is not owned:
            try:
                previous.close()
            except Exception:
                pass
        return scheduler


def get_shared_steam_scheduler() -> SteamRequestScheduler | DirectSteamRequestScheduler:
    """Return a safe facade; before configuration requests pass straight through."""

    with _SHARED_LOCK:
        return _SHARED_SCHEDULER or _DIRECT_SCHEDULER


def reset_shared_steam_scheduler(
    expected: SteamRequestScheduler | DirectSteamRequestScheduler | None = None,
) -> bool:
    """Close an owned test/runtime store and restore safe direct mode."""

    global _SHARED_SCHEDULER, _SHARED_OWNED_STORE
    with _SHARED_LOCK:
        if expected is not None and _SHARED_SCHEDULER is not expected:
            return False
        owned = _SHARED_OWNED_STORE
        _SHARED_SCHEDULER = None
        _SHARED_OWNED_STORE = None
        if owned is not None:
            try:
                owned.close()
            except Exception:
                pass
        return True


__all__ = [
    "DEFAULT_ACCOUNT_ROUTE_COOLDOWN_SECONDS",
    "DEFAULT_ADMISSION_TIMEOUT_SECONDS",
    "DEFAULT_GLOBAL_COOLDOWN_SECONDS",
    "DEGRADED_GLOBAL_PROBE_SECONDS",
    "DirectSteamRequestScheduler",
    "DirectSteamRequestPermit",
    "GLOBAL_CIRCUIT_KEY",
    "QUIET_WINDOW_CIRCUIT_KEY",
    "QueuePosition",
    "SteamCircuitOpen",
    "SteamRequestPermit",
    "SteamRequestGuardRejected",
    "SteamRequestPriority",
    "SteamRequestScheduler",
    "SteamRequestStore",
    "SteamRequestTimeout",
    "configure_shared_steam_scheduler",
    "get_shared_steam_scheduler",
    "normalize_steam_route",
    "parse_retry_after_seconds",
    "reset_shared_steam_scheduler",
]
