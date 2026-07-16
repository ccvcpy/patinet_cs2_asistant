from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cs2_assistant.accounts.steam_auth import try_steam_auto_relogin
from cs2_assistant.accounts.store import Account, AccountStore
from cs2_assistant.catalog import is_csgo_api_weapon_case
from cs2_assistant.config import PROJECT_ROOT, Settings
from cs2_assistant.clients.serverchan import ServerChanClient
from cs2_assistant.db import Database
from cs2_assistant.models import (
    OP_REBUY_C5,
    OP_SELL_STEAM,
    StrategyConfig,
    looks_like_weapon_case_name,
)
from cs2_assistant.services.executor_engine import (
    C5_DELIVERY_FAILED,
    C5_DELIVERY_STATUS_KEY,
    ExecutionEngine,
    _normalize_timestamp_iso,
    _parse_iso,
    _read_note,
)
from cs2_assistant.services.profit_trade import (
    execute_profit_trade_list_c5,
    recover_unverified_profit_trade_steam_buys,
    refresh_profit_trade_listings,
    refresh_profit_trade_sales,
    run_profit_trade_once,
)
from cs2_assistant.services.strategy import load_strategy_config, save_strategy_config
from cs2_assistant.utils import safe_float, safe_int, utc_now_iso


RUNTIME_GUADAO = "guadao"
RUNTIME_PROFIT_TRADE = "profit_trade"
RUNTIME_KEYS = (RUNTIME_GUADAO, RUNTIME_PROFIT_TRADE)

TASK_GUADAO_SCAN = "guadao_scan"
TASK_STEAM_ACCOUNT_SYNC = "steam_account_sync"
TASK_STEAM_LISTING_CONFIRM = "steam_listing_confirmation"
TASK_STEAM_SALE_EVIDENCE = "steam_sale_evidence"
TASK_REBUY_ATTEMPT = "rebuy_attempt"
TASK_C5_DELIVERY_CONFIRM = "c5_delivery_confirm"
TASK_PROFIT_CYCLE = "profit_cycle"

TASK_PUBLIC_LABELS = {
    TASK_GUADAO_SCAN: "挂刀候选完整扫描",
    TASK_STEAM_ACCOUNT_SYNC: "Steam 账号状态同步",
    TASK_STEAM_LISTING_CONFIRM: "Steam 挂单确认",
    TASK_STEAM_SALE_EVIDENCE: "Steam 卖出证据复核",
    TASK_REBUY_ATTEMPT: "C5 补仓价格复查",
    TASK_C5_DELIVERY_CONFIRM: "C5 发货确认",
    TASK_PROFIT_CYCLE: "Profit Trade 执行轮次",
}

COOKIE_BATCH_REUSE_SECONDS = 10 * 60
COOKIE_RETRY_DELAYS_SECONDS = (30, 60, 120, 300, 900)
PROFIT_CYCLE_INTERVAL_SECONDS = 10 * 60
WORKER_HEARTBEAT_STALE_SECONDS = 45


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _iso_after(seconds: float, *, now: datetime | None = None) -> str:
    return ((now or _now_utc()) + timedelta(seconds=max(0.0, float(seconds)))).isoformat()


def _task_payload(row: Any) -> dict[str, Any]:
    return _json_dict(row["payload_json"] if row is not None else None)


def _runtime_payload(row: Any) -> dict[str, Any]:
    return _json_dict(row["payload_json"] if row is not None else None)


def _catalog_item_matches_guadao_case_semantics(item: Any) -> bool:
    """Use the same broad Case classification that the guadao executor uses."""

    if item is None:
        return False
    raw_json = _json_dict(item["raw_json"])
    if isinstance(raw_json.get("csgoApi"), dict):
        return is_csgo_api_weapon_case(raw_json)
    names = (
        item["market_hash_name"],
        item["name_cn"],
        raw_json.get("marketHashName"),
        raw_json.get("name"),
    )
    if any(looks_like_weapon_case_name(str(value or "")) for value in names):
        return True
    type_name = str(raw_json.get("typeName") or raw_json.get("type") or "")
    return "武器箱" in type_name or "weaponcase" in type_name.lower()


def _tier_interval_seconds(
    tiers: Any,
    *,
    age_seconds: float,
    minimum_seconds: float,
) -> float:
    previous = float(minimum_seconds)
    for raw in tiers if isinstance(tiers, list) else []:
        if not isinstance(raw, dict):
            continue
        interval = max(previous, float(raw.get("intervalSeconds") or previous))
        previous = interval
        until = raw.get("untilSeconds")
        if until is None or age_seconds <= float(until):
            return interval
    return previous


class UnifiedRuntimeController:
    """Own both executors and the persistent due-task loop behind the 8765 API."""

    def __init__(self, settings: Settings, *, poll_seconds: float = 1.0) -> None:
        self.settings = settings
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.worker_id = f"runtime-{uuid.uuid4().hex[:12]}"
        self.account_store = AccountStore(PROJECT_ROOT / "config")
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._tick_lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._notification_lock = threading.Lock()
        self._steam_metadata_lock = threading.Lock()
        self._steam_request_metadata_cache: dict[str, dict[str, Any]] = {}
        self._last_error: str | None = None
        # Direct controller use in isolated tests/CLI is preconfigured by its
        # caller. ``start()`` replaces this with the production init result.
        self._steam_scheduler_ready = True
        self._steam_scheduler_error: str | None = None
        self._owns_steam_scheduler = False
        self._steam_scheduler_instance: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle and persistent runtime switches
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._initialize()
        try:
            from cs2_assistant.services.steam_request_scheduler import (
                configure_shared_steam_scheduler,
            )

            scheduler = configure_shared_steam_scheduler(
                self.settings.db_path,
                logger=self._steam_scheduler_telemetry,
            )
            self._steam_scheduler_ready = True
            self._steam_scheduler_error = None
            self._owns_steam_scheduler = True
            self._steam_scheduler_instance = scheduler
        except Exception as exc:
            self._steam_scheduler_ready = False
            self._steam_scheduler_error = str(exc)
            self._owns_steam_scheduler = False
            self._steam_scheduler_instance = None
            self._last_error = f"共享 Steam 请求调度器初始化失败: {exc}"
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                for row in db.list_executor_runtime_states():
                    payload = _runtime_payload(row)
                    payload["steamScheduler"] = {
                        "ready": False,
                        "error": str(exc),
                        "failedAt": utc_now_iso(),
                    }
                    db.upsert_executor_runtime_state(
                        str(row["executor_key"]),
                        enabled=bool(row["enabled"]),
                        runtime_status="preparing" if bool(row["enabled"]) else str(row["runtime_status"]),
                        migration_hold=bool(row["migration_hold"]),
                        gate_reason="共享 Steam 请求调度器不可用；Steam 动作已阻止",
                        heartbeat_at=utc_now_iso(),
                        payload=payload,
                    )
            finally:
                db.close()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="cs2-unified-runtime",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        if self._owns_steam_scheduler and (thread is None or not thread.is_alive()):
            try:
                from cs2_assistant.services.steam_request_scheduler import (
                    reset_shared_steam_scheduler,
                )

                reset_shared_steam_scheduler(expected=self._steam_scheduler_instance)
            finally:
                self._owns_steam_scheduler = False
                self._steam_scheduler_instance = None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def wake(self) -> None:
        self._wake_event.set()

    def _new_actions_enabled(self, executor_key: str) -> bool:
        """Re-read the persistent switch immediately before a new remote action."""

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            state = db.get_executor_runtime_state(executor_key)
            return bool(
                self._steam_scheduler_ready
                and state is not None
                and bool(state["enabled"])
                and not bool(state["migration_hold"])
            )
        except Exception:
            return False
        finally:
            db.close()

    def _initialize(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self._ensure_cookie_rows(db)
            self._seed_tasks(db)
            for key in RUNTIME_KEYS:
                row = db.get_executor_runtime_state(key)
                if row is None:
                    continue
                payload = _runtime_payload(row)
                if not payload.get("migrationInitializedAt"):
                    payload.update(
                        {
                            "migrationInitializedAt": utc_now_iso(),
                            "migrationLookbackDays": 7,
                            "migrationMessage": (
                                "首次升级处于 migration_hold；已备份数据库并回填只读任务，"
                                "确认后才恢复存量真实闭环。"
                            ),
                        }
                    )
                    db.upsert_executor_runtime_state(
                        key,
                        enabled=False,
                        runtime_status="migration_hold",
                        migration_hold=True,
                        gate_reason="等待用户确认迁移",
                        payload=payload,
                    )
            self._initialize_issue_notification_baseline(db)
        finally:
            db.close()

    def _initialize_issue_notification_baseline(self, db: Database) -> None:
        state = db.get_executor_runtime_state(RUNTIME_GUADAO)
        if state is None:
            return
        payload = _runtime_payload(state)
        if "knownGuadaoIssueIds" in payload:
            return
        payload["knownGuadaoIssueIds"] = [
            str(item.get("id") or item.get("issueId"))
            for item in self._issue_rows(db, include_acknowledged=True)
            if item.get("id") or item.get("issueId")
        ]
        payload["issueNotificationBaselineAt"] = utc_now_iso()
        db.upsert_executor_runtime_state(
            RUNTIME_GUADAO,
            enabled=bool(state["enabled"]),
            runtime_status=str(state["runtime_status"]),
            migration_hold=bool(state["migration_hold"]),
            gate_reason=state["gate_reason"],
            heartbeat_at=state["heartbeat_at"],
            payload=payload,
        )

    def toggle_executor(self, executor_key: str, enabled: bool) -> dict[str, Any]:
        key = str(executor_key or "").strip()
        if key not in RUNTIME_KEYS:
            raise ValueError("executor must be guadao or profit_trade")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_executor_runtime_state(key)
            if row is None:
                raise RuntimeError(f"runtime state missing: {key}")
            if bool(row["migration_hold"]):
                raise RuntimeError("migration_hold 尚未确认，不能开启执行器")
            payload = _runtime_payload(row)
            if enabled:
                now = utc_now_iso()
                batch_id = f"cookie-{uuid.uuid4().hex[:12]}"
                payload["requestedAt"] = now
                payload["cookieBatchId"] = batch_id
                payload["cookieBatchStartedAt"] = now
                payload["cookieGate"] = {
                    "status": "preparing",
                    "batchId": batch_id,
                    "validCount": 0,
                    "totalCount": len(self._accounts()),
                    "updatedAt": now,
                }
                for account in self._accounts():
                    current = db.get_steam_cookie_health(account.id)
                    db.upsert_steam_cookie_health(
                        account.id,
                        status="unknown",
                        account_name=account.name,
                        steam_id=account.steam_id64,
                        batch_id=batch_id,
                        failure_count=(
                            int(current["failure_count"] or 0) if current is not None else 0
                        ),
                        next_retry_at=now,
                        payload={"message": "执行器开启，等待逐账号刷新 Cookie"},
                    )
                status = "preparing"
                reason = "等待 5/5 Steam Cookie 门禁"
            else:
                payload["disabledAt"] = utc_now_iso()
                status = "closing_only" if self._has_closure_work(db, key) else "stopped"
                reason = "已停止新动作；存量流水继续安全闭环" if status == "closing_only" else None
            updated = db.upsert_executor_runtime_state(
                key,
                enabled=bool(enabled),
                runtime_status=status,
                migration_hold=False,
                gate_reason=reason,
                heartbeat_at=utc_now_iso(),
                payload=payload,
            )
        finally:
            db.close()
        self.wake()
        return self._public_runtime_row(updated)

    def confirm_migration(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            states: list[dict[str, Any]] = []
            for key in RUNTIME_KEYS:
                row = db.get_executor_runtime_state(key)
                payload = _runtime_payload(row)
                payload["migrationConfirmedAt"] = utc_now_iso()
                updated = db.upsert_executor_runtime_state(
                    key,
                    enabled=False,
                    runtime_status="closing_only" if self._has_closure_work(db, key) else "stopped",
                    migration_hold=False,
                    gate_reason="存量流水恢复闭环；新扫描保持关闭",
                    heartbeat_at=utc_now_iso(),
                    payload=payload,
                )
                states.append(self._public_runtime_row(updated))
        finally:
            db.close()
        self.wake()
        return {"ok": True, "states": states}

    def full_scan_now(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            state = db.get_executor_runtime_state(RUNTIME_GUADAO)
            if state is None or not bool(state["enabled"]):
                raise RuntimeError("挂刀执行器未开启，不能立即完整扫描")
            if bool(state["migration_hold"]):
                raise RuntimeError("migration_hold 尚未确认")
            changed = db.reschedule_scheduled_task(
                TASK_GUADAO_SCAN,
                next_attempt_at=utc_now_iso(),
            )
        finally:
            db.close()
        self.wake()
        return {"ok": bool(changed), "taskKey": TASK_GUADAO_SCAN}

    def profit_cycle_now(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            state = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
            if state is None or not bool(state["enabled"]):
                raise RuntimeError("Profit Trade 执行器未开启")
            if bool(state["migration_hold"]):
                raise RuntimeError("migration_hold 尚未确认")
            changed = db.reschedule_scheduled_task(
                TASK_PROFIT_CYCLE,
                next_attempt_at=utc_now_iso(),
            )
        finally:
            db.close()
        self.wake()
        return {"ok": bool(changed), "taskKey": TASK_PROFIT_CYCLE, "queued": True}

    def refresh_all_cookies_now(self) -> dict[str, Any]:
        batch_id = f"cookie-{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            for account in self._accounts():
                current = db.get_steam_cookie_health(account.id)
                db.upsert_steam_cookie_health(
                    account.id,
                    status="unknown",
                    account_name=account.name,
                    steam_id=account.steam_id64,
                    batch_id=batch_id,
                    failure_count=int(current["failure_count"] or 0) if current is not None else 0,
                    next_retry_at=now,
                    payload={"message": "用户请求重新刷新全部 Cookie"},
                )
            for key in RUNTIME_KEYS:
                state = db.get_executor_runtime_state(key)
                if state is None:
                    continue
                payload = _runtime_payload(state)
                payload["cookieBatchId"] = batch_id
                payload["cookieBatchStartedAt"] = now
                payload["cookieGate"] = {
                    "status": "preparing",
                    "batchId": batch_id,
                    "validCount": 0,
                    "totalCount": len(self._accounts()),
                    "updatedAt": now,
                }
                db.upsert_executor_runtime_state(
                    key,
                    enabled=bool(state["enabled"]),
                    runtime_status="preparing" if bool(state["enabled"]) else str(state["runtime_status"]),
                    migration_hold=bool(state["migration_hold"]),
                    gate_reason="重新刷新 5/5 Steam Cookie",
                    heartbeat_at=now,
                    payload=payload,
                )
        finally:
            db.close()
        self.wake()
        return {"ok": True, "batchId": batch_id, "startedAt": now}

    def retry_failed_steam_auth_now(self) -> dict[str, Any]:
        """Requeue only unhealthy account authentication checks.

        This method never performs a Steam request in the HTTP handler.  It
        merely advances eligible health rows so the unified worker can retry
        them through the shared Steam scheduler.  An active Retry-After for a
        limited account remains authoritative and is not shortened.
        """

        now = _now_utc()
        now_iso = now.isoformat()
        batch_id = f"auth-retry-{uuid.uuid4().hex[:12]}"
        queued: list[str] = []
        delayed: list[str] = []
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self._ensure_cookie_rows(db)
            accounts = {account.id: account for account in self._accounts()}
            for row in db.list_steam_cookie_health():
                account_id = str(row["account_id"] or "")
                status = str(row["status"] or "unknown")
                if not account_id or status in {"valid", "refreshing"}:
                    continue
                next_retry_at = now_iso
                current_retry_at = _parse_iso(str(row["next_retry_at"] or ""))
                if current_retry_at is not None and current_retry_at.tzinfo is None:
                    current_retry_at = current_retry_at.replace(tzinfo=timezone.utc)
                if (
                    status == "limited"
                    and current_retry_at is not None
                    and current_retry_at.astimezone(timezone.utc) > now
                ):
                    next_retry_at = current_retry_at.astimezone(timezone.utc).isoformat()
                    delayed.append(account_id)
                account = accounts.get(account_id)
                db.upsert_steam_cookie_health(
                    account_id,
                    status=status,
                    account_name=(account.name if account is not None else row["account_name"]),
                    steam_id=(account.steam_id64 if account is not None else row["steam_id"]),
                    batch_id=batch_id,
                    failure_count=int(row["failure_count"] or 0),
                    last_error=row["last_error"],
                    last_validated_at=row["last_validated_at"],
                    next_retry_at=next_retry_at,
                    retry_after_seconds=row["retry_after_seconds"],
                    payload={"message": "用户请求只重试失败账号"},
                )
                queued.append(account_id)
        finally:
            db.close()
        if queued:
            self.wake()
        return {
            "ok": True,
            "queued": len(queued),
            "accountIds": queued,
            "retryAfterProtectedAccountIds": delayed,
            "batchId": batch_id,
            "requestedAt": now_iso,
        }

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
            self._wake_event.wait(self.poll_seconds)
            self._wake_event.clear()

    def _steam_scheduler_telemetry(self, event: dict[str, Any]) -> None:
        """Route business logs separately and mirror only shared scheduler metadata."""

        try:
            source = str(event.get("source") or "")
            request_id = str(event.get("request_id") or "").strip()
            request_metadata: dict[str, Any] = {}
            if request_id:
                with self._steam_metadata_lock:
                    request_metadata = dict(
                        self._steam_request_metadata_cache.get(request_id) or {}
                    )
                if not request_metadata:
                    db = Database(self.settings.db_path)
                    try:
                        db.initialize()
                        request_row = db.get_steam_request(request_id)
                        if request_row is not None:
                            payload = _json_dict(request_row["payload_json"])
                            request_metadata = {
                                "marketHashName": payload.get("marketHashName"),
                                "businessOperationId": payload.get("businessOperationId"),
                            }
                    finally:
                        db.close()
                    with self._steam_metadata_lock:
                        self._steam_request_metadata_cache[request_id] = dict(
                            request_metadata
                        )
                        if len(self._steam_request_metadata_cache) > 5_000:
                            self._steam_request_metadata_cache.pop(
                                next(iter(self._steam_request_metadata_cache)),
                                None,
                            )
            market_hash_name = request_metadata.get("marketHashName")
            business_operation_id = request_metadata.get("businessOperationId")
            if source == "profit_trade":
                from cs2_assistant.services.profit_trade_logging import (
                    get_profit_trade_event_logger,
                )

                logger = get_profit_trade_event_logger()
            elif source == "guadao":
                from cs2_assistant.services.guadao_logging import get_guadao_event_logger

                logger = get_guadao_event_logger()
            else:
                logger = None
            phase = str(event.get("phase") or "request")
            if logger is not None:
                logger.telemetry_callback(
                    {
                        "source": source,
                        "provider": "steam",
                        "component": "steam_request_scheduler",
                        "operation": f"request_{phase}",
                        "message": f"Steam shared request {phase}",
                        "request_id": event.get("request_id"),
                        "market_hash_name": market_hash_name,
                        "trade_no": business_operation_id,
                        "account_id": event.get("account_id"),
                        "method": event.get("method"),
                        "endpoint": event.get("route"),
                        "status_code": event.get("status_code"),
                        "elapsed_ms": event.get("elapsed_ms"),
                        "retry_after": event.get("retry_after"),
                        "safe_context": {
                            **event,
                            **request_metadata,
                        },
                    }
                )
            if source != "guadao":
                from cs2_assistant.services.guadao_logging import get_guadao_event_logger

                get_guadao_event_logger().emit(
                    level="ERROR" if safe_int(event.get("status_code")) == 429 else "INFO",
                    provider="steam",
                    component="shared_steam_request_scheduler",
                    operation=f"request_{phase}",
                    message=f"[{source or 'unknown'}] Steam shared request {phase}",
                    request_id=event.get("request_id"),
                    market_hash_name=market_hash_name,
                    trade_no=business_operation_id,
                    account_id=event.get("account_id"),
                    method=event.get("method"),
                    endpoint=event.get("route"),
                    status_code=event.get("status_code"),
                    elapsed_ms=event.get("elapsed_ms"),
                    retry_after=event.get("retry_after"),
                    safe_context={
                        **event,
                        **request_metadata,
                        "source": source,
                        "sharedScheduler": True,
                    },
                )
            self._record_cookie_health_from_steam_event(event)
            self._notify_steam_circuit_state()
        except Exception:
            return

    def _emit_guadao_runtime_event(
        self,
        *,
        operation: str,
        message: str,
        level: str = "INFO",
        **context: Any,
    ) -> None:
        try:
            from cs2_assistant.services.guadao_logging import get_guadao_event_logger

            get_guadao_event_logger().emit(
                level=level,
                provider="local",
                component="guadao_runtime",
                operation=operation,
                message=message,
                safe_context=context,
            )
        except Exception:
            return

    def _send_runtime_notification_once(
        self,
        db: Database,
        *,
        event_key: str,
        title: str,
        body: str,
    ) -> bool:
        """Send one ServerChan event and persist the dedupe key in runtime state."""

        with self._notification_lock:
            state = db.get_executor_runtime_state(RUNTIME_GUADAO)
            if state is None:
                return False
            payload = _runtime_payload(state)
            sent = payload.get("notificationEvents")
            sent = dict(sent) if isinstance(sent, dict) else {}
            if event_key in sent or not self.settings.serverchan_sendkey:
                return False
            try:
                ServerChanClient(
                    self.settings.serverchan_sendkey,
                    self.settings.serverchan_base_url,
                    timeout=10,
                ).send(title, body)
            except Exception as exc:
                self._emit_guadao_runtime_event(
                    operation="serverchan_notify",
                    message="ServerChan 通知发送失败，后续仍可重试",
                    level="WARN",
                    eventKey=event_key,
                    error=str(exc),
                )
                return False
            sent[event_key] = utc_now_iso()
            if len(sent) > 500:
                sent = dict(sorted(sent.items(), key=lambda item: str(item[1]))[-500:])
            payload["notificationEvents"] = sent
            db.upsert_executor_runtime_state(
                RUNTIME_GUADAO,
                enabled=bool(state["enabled"]),
                runtime_status=str(state["runtime_status"]),
                migration_hold=bool(state["migration_hold"]),
                gate_reason=state["gate_reason"],
                heartbeat_at=state["heartbeat_at"],
                payload=payload,
            )
        self._emit_guadao_runtime_event(
            operation="serverchan_notify",
            message="ServerChan 通知已发送",
            eventKey=event_key,
            title=title,
        )
        return True

    def _record_cookie_health_from_steam_event(self, event: dict[str, Any]) -> None:
        account_id = str(event.get("account_id") or "").strip()
        status_code = safe_int(event.get("status_code"))
        route = str(event.get("route") or "").lower()
        if not account_id or status_code == 429:
            return
        authenticated_route = any(
            token in route
            for token in (
                "mylistings",
                "sellitem",
                "removelisting",
                "buylisting",
                "createbuyorder",
                "cancelbuyorder",
                "market/eligibilitycheck",
            )
        ) or route.rstrip("/") == "market"
        if status_code not in {400, 401} and not (status_code == 200 and authenticated_route):
            return
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            current = db.get_steam_cookie_health(account_id)
            if current is None:
                return
            current_status = str(current["status"] or "unknown")
            payload = _json_dict(current["payload_json"])
            if status_code in {400, 401}:
                if current_status == "invalid":
                    return
                outage_at = utc_now_iso()
                payload.update(
                    {
                        "outageStartedAt": outage_at,
                        "authenticationRoute": route,
                        "authenticationStatus": status_code,
                    }
                )
                db.upsert_steam_cookie_health(
                    account_id,
                    status="invalid",
                    failure_count=int(current["failure_count"] or 0) + 1,
                    last_error=f"Steam 认证接口返回 {status_code}",
                    next_retry_at=outage_at,
                    payload=payload,
                )
                self._send_runtime_notification_once(
                    db,
                    event_key=f"cookie-invalid:{account_id}:{outage_at}",
                    title="[Steam Cookie] 运行中账号认证失效",
                    body=(
                        f"账号: {current['account_name'] or account_id}\n"
                        f"接口: {route}\nHTTP: {status_code}\n"
                        "处理: 仅暂停该账号并进入自动 Cookie 刷新；429 不会触发此通知。"
                    ),
                )
                return
            if current_status in {"invalid", "limited", "network_unknown", "missing_credentials"}:
                outage_at = str(payload.get("outageStartedAt") or current["updated_at"] or "unknown")
                db.upsert_steam_cookie_health(
                    account_id,
                    status="valid",
                    failure_count=0,
                    last_validated_at=utc_now_iso(),
                    next_retry_at=None,
                    payload={**payload, "recoveredAt": utc_now_iso()},
                )
                self._send_runtime_notification_once(
                    db,
                    event_key=f"cookie-recovered:{account_id}:{outage_at}",
                    title="[Steam Cookie] 账号认证已恢复",
                    body=f"账号: {current['account_name'] or account_id}\n验证接口: {route}",
                )
        finally:
            db.close()

    def _notify_steam_circuit_state(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            circuit = db.get_steam_route_circuit("steam:global")
            if circuit is None:
                return
            state = str(circuit["state"] or "closed")
            first_at = str(circuit["first_429_at"] or "")
            payload = _json_dict(circuit["payload_json"])
            if state in {"open", "half_open"} and first_at:
                self._send_runtime_notification_once(
                    db,
                    event_key=f"steam-global-429-open:{first_at}",
                    title="[Steam 429] 共享请求已进入全局冷却",
                    body=(
                        f"首次429: {first_at}\n最后429: {circuit['last_429_at']}\n"
                        f"下次探测: {circuit['next_probe_at']}\n"
                        "处理: 新的非必要 Steam 动作已暂停，已有安全终态任务按优先级继续。"
                    ),
                )
                first = _parse_iso(first_at)
                if first is not None:
                    if first.tzinfo is None:
                        first = first.replace(tzinfo=timezone.utc)
                    if (_now_utc() - first.astimezone(timezone.utc)).total_seconds() >= 3600:
                        self._send_runtime_notification_once(
                            db,
                            event_key=f"steam-global-429-degraded:{first_at}",
                            title="[Steam 429] 全局限流持续超过60分钟",
                            body=(
                                f"首次429: {first_at}\n下次探测: {circuit['next_probe_at']}\n"
                                "探测间隔已扩大为30分钟，请结合 Profit 与挂刀实时日志检查请求来源。"
                            ),
                        )
            elif state == "closed" and str(circuit["reason"] or "") == "probe_recovered":
                recovered_at = str(payload.get("recoveredAt") or circuit["updated_at"] or "")
                self._send_runtime_notification_once(
                    db,
                    event_key=f"steam-global-429-recovered:{recovered_at}",
                    title="[Steam 429] 共享请求已恢复",
                    body=f"恢复时间: {recovered_at}\n全局熔断已关闭，后续请求继续受单通道调度保护。",
                )
        finally:
            db.close()

    def _notify_new_guadao_issues(self, db: Database) -> None:
        state = db.get_executor_runtime_state(RUNTIME_GUADAO)
        if state is None:
            return
        payload = _runtime_payload(state)
        known = {str(value) for value in payload.get("knownGuadaoIssueIds") or []}
        current = self._issue_rows(db, include_acknowledged=True)
        current_ids = {
            str(item.get("id") or item.get("issueId"))
            for item in current
            if item.get("id") or item.get("issueId")
        }
        new_items = [
            item
            for item in current
            if str(item.get("id") or item.get("issueId")) not in known
        ]
        payload["knownGuadaoIssueIds"] = sorted(known | current_ids)[-2000:]
        db.upsert_executor_runtime_state(
            RUNTIME_GUADAO,
            enabled=bool(state["enabled"]),
            runtime_status=str(state["runtime_status"]),
            migration_hold=bool(state["migration_hold"]),
            gate_reason=state["gate_reason"],
            heartbeat_at=state["heartbeat_at"],
            payload=payload,
        )
        for item in new_items:
            issue_id = str(item.get("id") or item.get("issueId"))
            self._send_runtime_notification_once(
                db,
                event_key=f"guadao-issue:{issue_id}",
                title=f"[挂刀待处理] {item.get('title') or item.get('issueType')}",
                body=(
                    f"物品: {item.get('marketHashName') or '-'}\n"
                    f"账号: {item.get('accountName') or '-'}\n"
                    f"原因: {item.get('summary') or item.get('reason') or '-'}\n"
                    f"建议: {item.get('recommendation') or '请在异常与待处理页面核对。'}"
                ),
            )

    def _notify_c5_delivery_timeouts(self, db: Database) -> None:
        rows = db.list_pool_operations_by_type_and_statuses(
            OP_REBUY_C5,
            statuses=[C5_DELIVERY_FAILED],
            limit=5000,
        )
        for row in rows:
            note = _read_note(row["note"])
            if str(note.get("c5OrderFailedCode") or "") != "delivery_timeout_24h":
                continue
            self._send_runtime_notification_once(
                db,
                event_key=f"c5-delivery-timeout:{int(row['id'])}",
                title="[挂刀补仓] C5 24小时未发货，已自动失败",
                body=(
                    f"物品: {row['market_hash_name']}\n原补仓流水: GD-{int(row['id'])}\n"
                    f"C5订单: {note.get('c5OrderId') or note.get('c5TradeOrderId') or '-'}\n"
                    f"替换补仓流水: {note.get('replacementRebuyOperationId') or '-'}\n"
                    "处理: 原单已判定补仓失败，替换补仓继续遵守原价格上限和冻结比例。"
                ),
            )

    def tick(self, *, max_tasks: int = 20) -> dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                self._heartbeat(db)
                self._ensure_cookie_rows(db)
                gate = self._cookie_gate_tick(db)
                self._seed_tasks(db)
            finally:
                db.close()

            processed: list[str] = []
            # Finite drain: a Steam sale may create a rebuy task at now; the
            # same worker tick can then claim exactly that task once.
            for _ in range(max(1, min(int(max_tasks), 100))):
                db = Database(self.settings.db_path)
                try:
                    db.initialize()
                    claimed = db.claim_due_scheduled_tasks(
                        self.worker_id,
                        limit=1,
                        lease_seconds=180,
                    )
                finally:
                    db.close()
                if not claimed:
                    break
                task = claimed[0]
                self._execute_claimed_task(task, gate=gate)
                processed.append(str(task["task_key"]))
                db = Database(self.settings.db_path)
                try:
                    db.initialize()
                    self._seed_tasks(db)
                finally:
                    db.close()
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                self._notify_new_guadao_issues(db)
                self._notify_c5_delivery_timeouts(db)
            finally:
                db.close()
            return {"ok": True, "processed": processed, "cookieGate": gate}
        finally:
            self._tick_lock.release()

    def _heartbeat(self, db: Database) -> None:
        now = utc_now_iso()
        for key in RUNTIME_KEYS:
            row = db.get_executor_runtime_state(key)
            if row is None:
                continue
            db.upsert_executor_runtime_state(
                key,
                enabled=bool(row["enabled"]),
                runtime_status=str(row["runtime_status"]),
                migration_hold=bool(row["migration_hold"]),
                gate_reason=row["gate_reason"],
                heartbeat_at=now,
                payload=_runtime_payload(row),
            )

    # ------------------------------------------------------------------
    # Cookie gate
    # ------------------------------------------------------------------

    def _accounts(self) -> list[Account]:
        return self.account_store.list_accounts()

    def _ensure_cookie_rows(self, db: Database) -> None:
        existing = {str(row["account_id"]): row for row in db.list_steam_cookie_health()}
        for account in self._accounts():
            if account.id in existing:
                continue
            db.upsert_steam_cookie_health(
                account.id,
                status="unknown",
                account_name=account.name,
                steam_id=account.steam_id64,
                payload={"message": "等待执行器启动门禁校验"},
            )

    def _runtime_needs_cookie_gate(self, db: Database) -> bool:
        if any(bool(row["enabled"]) for row in db.list_executor_runtime_states()):
            return True
        return self._has_closure_work(db, RUNTIME_GUADAO) or self._has_closure_work(
            db, RUNTIME_PROFIT_TRADE
        )

    def _active_cookie_batch(self, db: Database) -> str:
        newest: tuple[datetime, str] | None = None
        for row in db.list_executor_runtime_states():
            payload = _runtime_payload(row)
            batch_id = str(payload.get("cookieBatchId") or "").strip()
            started = _parse_iso(str(payload.get("cookieBatchStartedAt") or ""))
            if not batch_id or started is None:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if newest is None or started > newest[0]:
                newest = (started, batch_id)
        return newest[1] if newest else f"cookie-{uuid.uuid4().hex[:12]}"

    def _cookie_gate_snapshot(self, db: Database) -> dict[str, Any]:
        accounts = self._accounts()
        account_by_id = {account.id: account for account in accounts}
        rows = {str(row["account_id"]): row for row in db.list_steam_cookie_health()}
        public_accounts: list[dict[str, Any]] = []
        valid_count = 0
        for account in accounts:
            row = rows.get(account.id)
            status = str(row["status"] if row is not None else "unknown")
            if status == "valid":
                valid_count += 1
            public_accounts.append(
                {
                    "accountId": account.id,
                    "accountName": account.name,
                    "steamId": account.steam_id64,
                    "status": status,
                    "valid": status == "valid",
                    "failureCount": int(row["failure_count"] or 0) if row is not None else 0,
                    "lastError": row["last_error"] if row is not None else None,
                    "lastValidatedAt": row["last_validated_at"] if row is not None else None,
                    "lastCheckedAt": row["last_validated_at"] if row is not None else None,
                    "lastRefreshAt": row["updated_at"] if row is not None else None,
                    "lastResult": "Steam 市场门禁通过" if status == "valid" else None,
                    "error": row["last_error"] if row is not None else None,
                    "nextRetryAt": row["next_retry_at"] if row is not None else None,
                    "batchId": row["batch_id"] if row is not None else None,
                }
            )
        total = len(account_by_id)
        return {
            "status": "ready" if total > 0 and valid_count == total else "preparing",
            "validCount": valid_count,
            "totalCount": total,
            "accounts": public_accounts,
            "lastCompletedAt": max(
                (str(item.get("lastValidatedAt") or "") for item in public_accounts),
                default="",
            ) or None,
            "nextRetryAt": min(
                (str(item.get("nextRetryAt")) for item in public_accounts if item.get("nextRetryAt")),
                default=None,
            ),
            "updatedAt": utc_now_iso(),
        }

    def _cookie_gate_tick(self, db: Database) -> dict[str, Any]:
        snapshot = self._cookie_gate_snapshot(db)
        states = db.list_executor_runtime_states()
        if any(bool(row["migration_hold"]) for row in states):
            snapshot["status"] = "migration_hold"
            return snapshot
        if not self._runtime_needs_cookie_gate(db):
            snapshot["status"] = "idle"
            return snapshot
        if not self._steam_scheduler_ready:
            snapshot["status"] = "scheduler_unavailable"
            self._apply_gate_state(db, ready=False, snapshot=snapshot)
            return snapshot

        now = _now_utc()
        reusable = snapshot["totalCount"] > 0 and snapshot["validCount"] == snapshot["totalCount"]
        if reusable:
            for account in snapshot["accounts"]:
                validated = _parse_iso(str(account.get("lastValidatedAt") or ""))
                if validated is None:
                    reusable = False
                    break
                if validated.tzinfo is None:
                    validated = validated.replace(tzinfo=timezone.utc)
                if (now - validated.astimezone(timezone.utc)).total_seconds() > COOKIE_BATCH_REUSE_SECONDS:
                    reusable = False
                    break
        if reusable:
            self._apply_gate_state(db, ready=True, snapshot=snapshot)
            return snapshot

        batch_id = self._active_cookie_batch(db)
        due_rows = db.list_due_steam_cookie_retries(now=now.isoformat())
        by_id = {account.id: account for account in self._accounts()}
        target_row = next((row for row in due_rows if str(row["account_id"]) in by_id), None)
        if target_row is not None:
            account = by_id[str(target_row["account_id"])]
            self._refresh_cookie_account(db, account, target_row, batch_id=batch_id)
            snapshot = self._cookie_gate_snapshot(db)
        self._apply_gate_state(
            db,
            ready=snapshot["totalCount"] > 0 and snapshot["validCount"] == snapshot["totalCount"],
            snapshot=snapshot,
        )
        if snapshot["status"] != "ready":
            previously_ready = any(
                str(row["runtime_status"] or "") == "running"
                for row in states
            )
            if previously_ready and int(snapshot.get("validCount") or 0) > 0:
                snapshot["status"] = "degraded"
        return snapshot

    def _refresh_cookie_account(
        self,
        db: Database,
        account: Account,
        row: Any,
        *,
        batch_id: str,
    ) -> None:
        failure_count = int(row["failure_count"] or 0)
        if not account.username or not account.password:
            delay = COOKIE_RETRY_DELAYS_SECONDS[min(failure_count, len(COOKIE_RETRY_DELAYS_SECONDS) - 1)]
            db.upsert_steam_cookie_health(
                account.id,
                status="missing_credentials",
                account_name=account.name,
                steam_id=account.steam_id64,
                batch_id=batch_id,
                failure_count=failure_count + 1,
                last_error="账号缺少用户名或密码，Cookie 门禁持续阻塞",
                next_retry_at=_iso_after(delay),
                payload={"retryDelaySeconds": delay},
            )
            return
        db.upsert_steam_cookie_health(
            account.id,
            status="refreshing",
            account_name=account.name,
            steam_id=account.steam_id64,
            batch_id=batch_id,
            failure_count=failure_count,
            payload={"startedAt": utc_now_iso()},
        )
        try:
            ok, status, updated = try_steam_auto_relogin(
                self.account_store,
                account_id=account.id,
                force_login=True,
            )
        except Exception as exc:
            ok, status, updated = False, str(exc), None
        if ok and updated is not None and updated.cookies:
            db.upsert_steam_cookie_health(
                account.id,
                status="valid",
                account_name=updated.name,
                steam_id=updated.steam_id64,
                batch_id=batch_id,
                failure_count=0,
                last_validated_at=utc_now_iso(),
                payload={"message": "Steam Cookie 已刷新并通过市场门禁"},
            )
            if failure_count > 0:
                self._send_runtime_notification_once(
                    db,
                    event_key=f"cookie-refresh-recovered:{batch_id}:{account.id}",
                    title="[Steam Cookie] 自动刷新已恢复",
                    body=f"账号: {updated.name or account.name}\n失败次数已清零，账号任务将恢复推进。",
                )
            return
        normalized = str(status or "refresh_failed")[:240]
        delay = COOKIE_RETRY_DELAYS_SECONDS[min(failure_count, len(COOKIE_RETRY_DELAYS_SECONDS) - 1)]
        if "thrott" in normalized.lower() or "429" in normalized:
            health_status = "limited"
        elif any(token in normalized.lower() for token in ("timeout", "ssl", "connection")):
            health_status = "network_unknown"
        else:
            health_status = "invalid"
        db.upsert_steam_cookie_health(
            account.id,
            status=health_status,
            account_name=account.name,
            steam_id=account.steam_id64,
            batch_id=batch_id,
            failure_count=failure_count + 1,
            last_error=normalized,
            next_retry_at=_iso_after(delay),
            payload={"retryDelaySeconds": delay},
        )
        self._send_runtime_notification_once(
            db,
            event_key=f"cookie-refresh-failed:{batch_id}:{account.id}",
            title="[Steam Cookie] 刷新未通过",
            body=(
                f"账号: {account.name}\n状态: {health_status}\n原因: {normalized}\n"
                f"下次自动重试: {delay:g} 秒后\n429 只按限流处理，不会被判定为 Cookie 失效。"
            ),
        )

    def _apply_gate_state(self, db: Database, *, ready: bool, snapshot: dict[str, Any]) -> None:
        notify_ready = False
        for row in db.list_executor_runtime_states():
            key = str(row["executor_key"])
            enabled = bool(row["enabled"])
            payload = _runtime_payload(row)
            previously_ready = (
                str(row["runtime_status"] or "") == "running"
                or str(payload.get("cookieGate", {}).get("status") or "")
                in {"ready", "degraded"}
            )
            if (
                enabled
                and ready
                and self._steam_scheduler_ready
                and str(payload.get("cookieGate", {}).get("status") or "") != "ready"
            ):
                notify_ready = True
            degraded = (
                not ready
                and previously_ready
                and int(snapshot.get("validCount") or 0) > 0
            )
            gate_status = "ready" if ready else "degraded" if degraded else "preparing"
            payload["cookieGate"] = {
                "status": gate_status,
                "validCount": snapshot["validCount"],
                "totalCount": snapshot["totalCount"],
                "updatedAt": snapshot["updatedAt"],
            }
            if enabled:
                if not self._steam_scheduler_ready:
                    status = "preparing"
                    reason = "共享 Steam 请求调度器不可用；Steam 动作已阻止"
                else:
                    status = "running" if ready or degraded else "preparing"
                    reason = (
                        None
                        if ready
                        else f"部分 Steam Cookie 失效，仅暂停对应账号 {snapshot['validCount']}/{snapshot['totalCount']}"
                        if degraded
                        else f"Steam Cookie 启动门禁 {snapshot['validCount']}/{snapshot['totalCount']}"
                    )
            else:
                status = "closing_only" if self._has_closure_work(db, key) else "stopped"
                reason = "仅继续存量闭环" if status == "closing_only" else None
            db.upsert_executor_runtime_state(
                key,
                enabled=enabled,
                runtime_status=status,
                migration_hold=bool(row["migration_hold"]),
                gate_reason=reason,
                heartbeat_at=utc_now_iso(),
                payload=payload,
            )
        if notify_ready:
            batch_id = next(
                (
                    str(account.get("batchId"))
                    for account in snapshot.get("accounts") or []
                    if account.get("batchId")
                ),
                "unknown",
            )
            self._send_runtime_notification_once(
                db,
                event_key=f"cookie-gate-ready:{batch_id}",
                title="[执行器] Steam Cookie 5/5 已就绪",
                body="全部本地 Steam 账号已通过市场 Cookie 门禁，新扫描与新动作可以继续。",
            )

    # ------------------------------------------------------------------
    # Task seeding and execution
    # ------------------------------------------------------------------

    def _ensure_task(
        self,
        db: Database,
        task_key: str,
        *,
        source: str,
        task_type: str,
        next_attempt_at: str,
        account_id: str | None = None,
        operation_id: int | str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int,
        status: str = "pending",
    ) -> None:
        ensure = getattr(db, "ensure_scheduled_task", None)
        if callable(ensure):
            ensure(
                task_key,
                source=source,
                task_type=task_type,
                next_attempt_at=next_attempt_at,
                account_id=account_id,
                operation_id=operation_id,
                payload=payload,
                priority=priority,
                status=status,
            )
            return
        if db.get_scheduled_task(task_key) is None:
            db.upsert_scheduled_task(
                task_key,
                source=source,
                task_type=task_type,
                next_attempt_at=next_attempt_at,
                account_id=account_id,
                operation_id=operation_id,
                payload=payload,
                priority=priority,
                status=status,
            )

    def _guadao_operation_account_id(self, op: Any) -> str | None:
        note = _read_note(op["note"])
        account_id = str(note.get("steamAccountId") or "").strip()
        if account_id:
            return account_id
        steam_id = str(note.get("steamId64") or "").strip()
        if not steam_id:
            return None
        account = next(
            (
                item
                for item in self._accounts()
                if str(item.steam_id64 or "").strip() == steam_id
            ),
            None,
        )
        return account.id if account is not None else None

    @staticmethod
    def _operation_task_delays(config: StrategyConfig, task_type: str) -> list[float]:
        schedule = config.effective_guadao_task_schedule()
        key = (
            "actionConfirmationDelaysSeconds"
            if task_type == TASK_STEAM_LISTING_CONFIRM
            else "saleEvidenceDelaysSeconds"
        )
        values = [max(0.0, float(value)) for value in schedule.get(key) or []]
        return values or ([10.0] if task_type == TASK_STEAM_LISTING_CONFIRM else [0.0, 60.0])

    def _seed_steam_operation_tasks(self, db: Database, config: StrategyConfig) -> None:
        now = _now_utc()
        statuses = ["listing_pending", "listed", "manual_required"]
        operations = db.list_pool_operations_by_type_and_statuses(
            OP_SELL_STEAM,
            statuses=statuses,
            limit=5000,
        )
        for op in operations:
            note = _read_note(op["note"])
            raw_status = str(op["status"] or "")
            if raw_status == "manual_required" and str(
                note.get("staleListedCleanupStatus") or ""
            ) != "manual_required":
                continue
            account_id = self._guadao_operation_account_id(op)
            if not account_id:
                continue
            waiting_for_sale_evidence = (
                raw_status == "listing_pending"
                and str(note.get("confirmationStatus") or "") == "listing_missing_unverified"
            )
            task_type = (
                TASK_STEAM_LISTING_CONFIRM
                if raw_status == "listing_pending" and not waiting_for_sale_evidence
                else TASK_STEAM_SALE_EVIDENCE
            )
            prefix = "listing-confirm" if task_type == TASK_STEAM_LISTING_CONFIRM else "sale-evidence"
            task_key = f"{prefix}:{int(op['id'])}"
            delays = self._operation_task_delays(config, task_type)
            base = _parse_iso(op["created_at"]) or now
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            explicit_next = (
                _parse_iso(str(note.get("staleListedNextCheckAt") or ""))
                if raw_status == "manual_required"
                else None
            )
            first_due = explicit_next or (
                base.astimezone(timezone.utc) + timedelta(seconds=delays[0])
            )
            self._ensure_task(
                db,
                task_key,
                source=RUNTIME_GUADAO,
                task_type=task_type,
                next_attempt_at=first_due.isoformat(),
                account_id=account_id,
                operation_id=int(op["id"]),
                payload={"tierIndex": 0, "delaysSeconds": delays},
                priority=2,
                status="waiting",
            )

        for account in self._accounts():
            markers = [
                row
                for task_type in (TASK_STEAM_LISTING_CONFIRM, TASK_STEAM_SALE_EVIDENCE)
                for row in db.list_scheduled_tasks(
                    source=RUNTIME_GUADAO,
                    task_type=task_type,
                    status="waiting",
                    account_id=account.id,
                    limit=5000,
                )
            ]
            if not markers:
                continue
            earliest = min(str(row["next_attempt_at"]) for row in markers)
            sync_key = f"steam-sync:{account.id}"
            sync_task = db.get_scheduled_task(sync_key)
            if (
                sync_task is not None
                and str(sync_task["status"] or "") in {"pending", "retry"}
                and earliest < str(sync_task["next_attempt_at"] or "")
            ):
                db.reschedule_scheduled_task(sync_key, next_attempt_at=earliest)

    def _seed_tasks(self, db: Database) -> None:
        now = utc_now_iso()
        config = load_strategy_config(self.settings)
        self._ensure_task(
            db,
            TASK_GUADAO_SCAN,
            source=RUNTIME_GUADAO,
            task_type=TASK_GUADAO_SCAN,
            next_attempt_at=now,
            priority=3,
        )
        for account in self._accounts():
            self._ensure_task(
                db,
                f"steam-sync:{account.id}",
                source=RUNTIME_GUADAO,
                task_type=TASK_STEAM_ACCOUNT_SYNC,
                next_attempt_at=now,
                account_id=account.id,
                priority=2,
            )
        self._ensure_task(
            db,
            TASK_PROFIT_CYCLE,
            source=RUNTIME_PROFIT_TRADE,
            task_type=TASK_PROFIT_CYCLE,
            next_attempt_at=now,
            priority=1,
        )
        self._seed_steam_operation_tasks(db, config)
        for op in db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=5000):
            self._ensure_task(
                db,
                f"rebuy:{op['id']}",
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_ATTEMPT,
                next_attempt_at=now,
                operation_id=int(op["id"]),
                priority=1,
                payload={"createdAt": op["created_at"]},
            )
        delivery_rows = db.list_pool_operations_by_type_and_statuses(
            OP_REBUY_C5,
            statuses=["delivery_pending", "completed"],
            limit=5000,
        )
        cutoff = _now_utc() - timedelta(days=7)
        for op in delivery_rows:
            note = _read_note(op["note"])
            if note.get(C5_DELIVERY_STATUS_KEY) in {"c5_success", C5_DELIVERY_FAILED}:
                continue
            op_time = _parse_iso(op["completed_at"]) or _parse_iso(op["created_at"])
            if op_time is not None:
                if op_time.tzinfo is None:
                    op_time = op_time.replace(tzinfo=timezone.utc)
                if str(op["status"] or "") != "delivery_pending" and op_time < cutoff:
                    continue
            if not any(note.get(key) for key in ("c5OutTradeNo", "c5OrderId", "c5TradeOrderId")):
                continue
            self._ensure_task(
                db,
                f"delivery:{op['id']}",
                source=RUNTIME_GUADAO,
                task_type=TASK_C5_DELIVERY_CONFIRM,
                next_attempt_at=now,
                operation_id=int(op["id"]),
                priority=1,
                payload={"createdAt": op["created_at"]},
            )

    def _due_steam_operation_tasks(self, db: Database, account_id: str) -> list[dict[str, Any]]:
        now = utc_now_iso()
        return [
            dict(row)
            for task_type in (TASK_STEAM_LISTING_CONFIRM, TASK_STEAM_SALE_EVIDENCE)
            for row in db.list_scheduled_tasks(
                source=RUNTIME_GUADAO,
                task_type=task_type,
                status="waiting",
                account_id=account_id,
                limit=5000,
            )
            if str(row["next_attempt_at"] or "") <= now
        ]

    def _execute_claimed_task(self, task: Any, *, gate: dict[str, Any]) -> None:
        task_key = str(task["task_key"])
        task_type = str(task["task_type"])
        source = str(task["source"])
        dispatch_task: Any = task
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            state = db.get_executor_runtime_state(source)
            if state is None:
                db.complete_scheduled_task(task_key, self.worker_id, status="failed", error="runtime state missing")
                return
            if bool(state["migration_hold"]):
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(60),
                    worker_id=self.worker_id,
                    error="migration_hold",
                )
                return
            enabled = bool(state["enabled"])
            if (
                not self._steam_scheduler_ready
                and task_type in {TASK_GUADAO_SCAN, TASK_STEAM_ACCOUNT_SYNC, TASK_PROFIT_CYCLE}
            ):
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(30),
                    worker_id=self.worker_id,
                    error="steam_scheduler_unavailable",
                )
                return
            gate_ready = gate.get("status") in {"ready", "degraded"}
            if task_type in {TASK_GUADAO_SCAN, TASK_PROFIT_CYCLE} and enabled and not gate_ready:
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(5),
                    worker_id=self.worker_id,
                    error="cookie_gate_preparing",
                )
                return
            if task_type == TASK_GUADAO_SCAN and not enabled:
                schedule = load_strategy_config(self.settings).effective_guadao_task_schedule()
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(float(schedule["scanIntervalSeconds"])),
                    worker_id=self.worker_id,
                    error="executor_disabled",
                )
                return
            if task_type == TASK_PROFIT_CYCLE and not enabled and not self._has_closure_work(db, source):
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(PROFIT_CYCLE_INTERVAL_SECONDS),
                    worker_id=self.worker_id,
                    error="executor_disabled_no_closure_work",
                )
                return
            if task_type == TASK_STEAM_ACCOUNT_SYNC:
                account_id = str(task["account_id"] or "")
                health = db.get_steam_cookie_health(account_id)
                if health is None or str(health["status"]) != "valid":
                    db.reschedule_scheduled_task(
                        task_key,
                        next_attempt_at=_iso_after(30),
                        worker_id=self.worker_id,
                        error="account_cookie_not_valid",
                    )
                    return
                if not enabled and not self._has_guadao_account_closure_work(db, account_id):
                    schedule = load_strategy_config(self.settings).effective_guadao_task_schedule()
                    db.reschedule_scheduled_task(
                        task_key,
                        next_attempt_at=_iso_after(float(schedule["steamSyncIntervalSeconds"])),
                        worker_id=self.worker_id,
                        error="executor_disabled_no_account_work",
                    )
                    return
                dispatch_task = {
                    **dict(task),
                    "_dueOperationTasks": self._due_steam_operation_tasks(db, account_id),
                }
        finally:
            db.close()

        try:
            result = self._dispatch_task(dispatch_task, enabled=enabled)
        except Exception as exc:
            if source == RUNTIME_GUADAO:
                self._emit_guadao_runtime_event(
                    operation=task_type,
                    message="挂刀到期任务执行失败",
                    level="ERROR",
                    taskKey=task_key,
                    error=str(exc),
                )
            self._reschedule_after_task(task, error=str(exc))
            return
        self._reschedule_after_task(dispatch_task, result=result)

    def _dispatch_task(self, task: Any, *, enabled: bool) -> dict[str, Any]:
        task_type = str(task["task_type"])
        if task_type == TASK_PROFIT_CYCLE:
            if enabled:
                report = run_profit_trade_once(
                    self.settings,
                    new_action_guard=lambda: self._new_actions_enabled(
                        RUNTIME_PROFIT_TRADE
                    ),
                )
                return report.to_dict()
            return self._run_profit_closure_once()
        self._emit_guadao_runtime_event(
            operation=task_type,
            message="挂刀到期任务开始执行",
            taskKey=str(task["task_key"]),
            accountId=task["account_id"],
            operationId=task["operation_id"],
        )
        engine = ExecutionEngine(
            self.settings,
            new_action_guard=(
                (lambda: self._new_actions_enabled(RUNTIME_GUADAO))
                if task_type == TASK_GUADAO_SCAN
                else None
            ),
        )
        try:
            if task_type == TASK_GUADAO_SCAN:
                result = engine.run_guadao_scan_task()
            elif task_type == TASK_STEAM_ACCOUNT_SYNC:
                due_tasks = list(task.get("_dueOperationTasks") or [])
                confirmation_ids = {
                    int(row["operation_id"])
                    for row in due_tasks
                    if str(row.get("task_type") or "") == TASK_STEAM_LISTING_CONFIRM
                    and safe_int(row.get("operation_id")) is not None
                }
                sale_ids = {
                    int(row["operation_id"])
                    for row in due_tasks
                    if str(row.get("task_type") or "") == TASK_STEAM_SALE_EVIDENCE
                    and safe_int(row.get("operation_id")) is not None
                }
                result = engine.run_guadao_account_sync_task(
                    str(task["account_id"] or "") or None,
                    confirmation_operation_ids=confirmation_ids,
                    sale_operation_ids=sale_ids,
                )
            elif task_type == TASK_REBUY_ATTEMPT:
                result = engine.run_guadao_rebuy_task(int(task["operation_id"]))
            elif task_type == TASK_C5_DELIVERY_CONFIRM:
                result = engine.run_guadao_delivery_confirmation_task(int(task["operation_id"]))
            else:
                result = {"ok": False, "error": f"unknown task type: {task_type}"}
            self._emit_guadao_runtime_event(
                operation=task_type,
                message="挂刀到期任务执行完成",
                level="INFO" if result.get("ok", True) else "ERROR",
                taskKey=str(task["task_key"]),
                result=result,
            )
            return result
        finally:
            engine.close()

    def _run_profit_closure_once(self) -> dict[str, Any]:
        config = load_strategy_config(self.settings)
        result: dict[str, Any] = {"ok": True, "settled": [], "listed": [], "errors": []}
        try:
            refreshed = refresh_profit_trade_sales(self.settings, config)
            result["settled"] = refreshed.get("settledTradeIds", [])
            result["errors"].extend(refreshed.get("errors", []))
        except Exception as exc:
            result["errors"].append(f"refresh-sales: {exc}")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            pending_ids = [int(row["id"]) for row in db.list_profit_trades(status="steam_bought", limit=200)]
        finally:
            db.close()
        for trade_id in pending_ids:
            try:
                listed = execute_profit_trade_list_c5(self.settings, trade_id, config=config)
                if listed.get("ok"):
                    result["listed"].append(trade_id)
            except Exception as exc:
                result["errors"].append(f"list-c5 {trade_id}: {exc}")
        try:
            refresh_profit_trade_listings(self.settings, config)
        except Exception as exc:
            result["errors"].append(f"refresh-listings: {exc}")
        try:
            recover_unverified_profit_trade_steam_buys(
                self.settings,
                config=config,
                remote_audit=True,
            )
        except Exception as exc:
            result["errors"].append(f"recover-buys: {exc}")
        result["ok"] = not result["errors"]
        return result

    def _advance_due_steam_operation_tasks(
        self,
        db: Database,
        due_tasks: list[dict[str, Any]],
        *,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        config = load_strategy_config(self.settings)
        request_failed = bool(error) or (result is not None and not bool(result.get("ok", True)))
        for task in due_tasks:
            task_key = str(task.get("task_key") or "")
            operation_id = safe_int(task.get("operation_id"))
            task_type = str(task.get("task_type") or "")
            if not task_key or operation_id is None:
                continue
            op = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            if op is None:
                db.delete_scheduled_task(task_key)
                continue
            note = _read_note(op["note"])
            raw_status = str(op["status"] or "")
            remains_due_kind = (
                task_type == TASK_STEAM_LISTING_CONFIRM
                and raw_status == "listing_pending"
            ) or (
                task_type == TASK_STEAM_SALE_EVIDENCE
                and (
                    raw_status == "listed"
                    or (
                        raw_status == "manual_required"
                        and str(note.get("staleListedCleanupStatus") or "") == "manual_required"
                    )
                )
            )
            if not remains_due_kind:
                db.delete_scheduled_task(task_key)
                continue
            payload = _task_payload(task)
            delays = self._operation_task_delays(config, task_type)
            tier_index = max(0, int(payload.get("tierIndex") or 0))
            if request_failed:
                next_index = tier_index
                delay = min(30.0, float(delays[min(tier_index, len(delays) - 1)] or 30.0))
            else:
                next_index = min(tier_index + 1, len(delays) - 1)
                delay = float(delays[next_index])
            db.upsert_scheduled_task(
                task_key,
                source=RUNTIME_GUADAO,
                task_type=task_type,
                next_attempt_at=_iso_after(max(2.0, delay)),
                account_id=str(task.get("account_id") or "") or None,
                operation_id=operation_id,
                payload={
                    **payload,
                    "tierIndex": next_index,
                    "delaysSeconds": delays,
                    "lastAttemptAt": utc_now_iso(),
                },
                status="waiting",
                priority=int(task.get("priority") or 2),
                last_error=error or (str(result.get("error") or "") if result else None),
            )

    def _reschedule_after_task(
        self,
        task: Any,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        task_key = str(task["task_key"])
        task_type = str(task["task_type"])
        config = load_strategy_config(self.settings)
        schedule = config.effective_guadao_task_schedule()
        next_seconds: float | None
        terminal = False
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            if task_type == TASK_STEAM_ACCOUNT_SYNC:
                due_tasks = (
                    list(task.get("_dueOperationTasks") or [])
                    if isinstance(task, dict)
                    else []
                )
                self._advance_due_steam_operation_tasks(
                    db,
                    due_tasks,
                    result=result,
                    error=error,
                )
            if task_type == TASK_GUADAO_SCAN:
                next_seconds = float(schedule["scanIntervalSeconds"])
            elif task_type == TASK_STEAM_ACCOUNT_SYNC:
                next_seconds = float(schedule["steamSyncIntervalSeconds"])
            elif task_type == TASK_PROFIT_CYCLE:
                next_seconds = PROFIT_CYCLE_INTERVAL_SECONDS
            elif task_type == TASK_REBUY_ATTEMPT:
                op = db.conn.execute(
                    "SELECT * FROM pool_operations WHERE id = ?",
                    (int(task["operation_id"]),),
                ).fetchone()
                if op is None or str(op["status"]) != "pending":
                    terminal = True
                    next_seconds = None
                else:
                    created = _parse_iso(op["created_at"]) or _now_utc()
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    age = max(0.0, (_now_utc() - created).total_seconds())
                    next_seconds = _tier_interval_seconds(
                        schedule.get("rebuyRetryTiers"),
                        age_seconds=age,
                        minimum_seconds=30.0,
                    )
            elif task_type == TASK_C5_DELIVERY_CONFIRM:
                op = db.conn.execute(
                    "SELECT * FROM pool_operations WHERE id = ?",
                    (int(task["operation_id"]),),
                ).fetchone()
                if op is None or str(op["status"]) != "delivery_pending":
                    terminal = True
                    next_seconds = None
                else:
                    note = _read_note(op["note"])
                    submitted = _parse_iso(str(note.get("c5OrderSubmittedAt") or ""))
                    if submitted is not None and submitted.tzinfo is None:
                        submitted = submitted.replace(tzinfo=timezone.utc)
                    # Missing submission time means the 24-hour clock has not
                    # started.  Keep a short confirmation cadence for any
                    # existing remote identifier instead of aging from the
                    # unrelated local operation creation time.
                    age = (
                        max(0.0, (_now_utc() - submitted).total_seconds())
                        if submitted is not None
                        else 0.0
                    )
                    next_seconds = _tier_interval_seconds(
                        schedule.get("deliveryConfirmationTiers"),
                        age_seconds=age,
                        minimum_seconds=30.0,
                    )
            else:
                terminal = True
                next_seconds = None

            if terminal:
                db.complete_scheduled_task(
                    task_key,
                    self.worker_id,
                    status="failed" if error else "completed",
                    error=error,
                )
            else:
                next_attempt_at = _iso_after(float(next_seconds or 60.0))
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=next_attempt_at,
                    worker_id=self.worker_id,
                    error=error or (None if result is None else str(result.get("error") or "") or None),
                    status="retry" if error else "pending",
                )
                state = db.get_executor_runtime_state(str(task["source"]))
                if state is not None:
                    runtime_payload = _runtime_payload(state)
                    runtime_payload["lastRunAt"] = utc_now_iso()
                    runtime_payload["lastRunSummary"] = (
                        f"{task_type} 失败：{error}"
                        if error
                        else f"{task_type} 已完成"
                    )
                    if task_type == TASK_GUADAO_SCAN:
                        runtime_payload["nextScanAt"] = next_attempt_at
                    db.upsert_executor_runtime_state(
                        str(task["source"]),
                        enabled=bool(state["enabled"]),
                        runtime_status=str(state["runtime_status"]),
                        migration_hold=bool(state["migration_hold"]),
                        gate_reason=state["gate_reason"],
                        heartbeat_at=utc_now_iso(),
                        payload=runtime_payload,
                    )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Public read models and settings
    # ------------------------------------------------------------------

    def _has_closure_work(self, db: Database, executor_key: str) -> bool:
        if executor_key == RUNTIME_GUADAO:
            row = db.conn.execute(
                """
                SELECT COUNT(*) AS count FROM pool_operations
                WHERE (operation_type = ? AND status IN ('pending','listed','listing_pending','manual_required'))
                   OR (operation_type = ? AND status IN ('pending','delivery_pending','failed','c5_failed'))
                """,
                (OP_SELL_STEAM, OP_REBUY_C5),
            ).fetchone()
            return bool(row and int(row["count"] or 0) > 0)
        row = db.conn.execute(
            """
            SELECT COUNT(*) AS count FROM profit_trades
            WHERE status NOT IN ('completed','cancelled')
            """
        ).fetchone()
        return bool(row and int(row["count"] or 0) > 0)

    def _has_guadao_account_closure_work(self, db: Database, account_id: str) -> bool:
        account = self.account_store.get_account(account_id)
        steam_id = str(account.steam_id64 or "").strip() if account else ""
        if not steam_id:
            return False
        rows = db.list_pool_operations_by_type_and_statuses(
            OP_SELL_STEAM,
            statuses=["pending", "listed", "listing_pending", "manual_required"],
            limit=5000,
        )
        for row in rows:
            note = _read_note(row["note"])
            if str(note.get("steamAccountId") or "") == account_id:
                return True
            if str(note.get("steamId64") or "") == steam_id:
                return True
        return False

    def _public_runtime_row(self, row: Any) -> dict[str, Any]:
        payload = _runtime_payload(row)
        status = str(row["runtime_status"] or "stopped")
        return {
            "executor": row["executor_key"],
            "enabled": bool(row["enabled"]),
            "status": status,
            "runtimeStatus": status,
            "preparing": status == "preparing",
            "migrationHold": bool(row["migration_hold"]),
            "migrationConfirmed": not bool(row["migration_hold"]),
            "gateReason": row["gate_reason"],
            "heartbeatAt": row["heartbeat_at"],
            "updatedAt": row["updated_at"],
            "lastRunAt": payload.get("lastRunAt"),
            "lastRunSummary": payload.get("lastRunSummary"),
            "nextScanAt": payload.get("nextScanAt"),
            "payload": payload,
        }

    def cookie_snapshot(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            snapshot = self._cookie_gate_snapshot(db)
            snapshot["gate"] = snapshot["status"]
            snapshot["runtimeAlive"] = self.alive
            return snapshot
        finally:
            db.close()

    def runtime_states(self, executor_key: str | None = None) -> dict[str, Any]:
        key = str(executor_key or "").strip()
        if key and key not in RUNTIME_KEYS:
            raise ValueError("executor must be guadao or profit_trade")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            states = {
                str(row["executor_key"]): self._public_runtime_row(row)
                for row in db.list_executor_runtime_states()
            }
            return {
                "state": states.get(key) if key else None,
                "states": states,
                "cookieGate": self._cookie_gate_snapshot(db),
                "workerAlive": self.alive,
            }
        finally:
            db.close()

    def dashboard(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            runtimes = {
                str(row["executor_key"]): self._public_runtime_row(row)
                for row in db.list_executor_runtime_states()
            }
            raw_task_rows = db.list_scheduled_tasks(source=RUNTIME_GUADAO, limit=500)
            tasks = [self._public_task(row) for row in raw_task_rows]
            operation_ids = sorted(
                {
                    int(value)
                    for value in (safe_int(task.get("operationId")) for task in tasks)
                    if value is not None
                }
            )
            operation_markets: dict[int, str] = {}
            if operation_ids:
                placeholders = ",".join("?" for _ in operation_ids)
                operation_markets = {
                    int(row["id"]): str(row["market_hash_name"])
                    for row in db.conn.execute(
                        f"SELECT id, market_hash_name FROM pool_operations WHERE id IN ({placeholders})",
                        tuple(operation_ids),
                    ).fetchall()
                }
            for task in tasks:
                operation_id = safe_int(task.get("operationId"))
                if not task.get("marketHashName") and operation_id is not None:
                    task["marketHashName"] = operation_markets.get(operation_id)
            now = _now_utc()
            due_tasks = []
            for task in tasks:
                next_at = _parse_iso(str(task.get("nextAttemptAt") or ""))
                if next_at is None:
                    continue
                if next_at.tzinfo is None:
                    next_at = next_at.replace(tzinfo=timezone.utc)
                if next_at.astimezone(timezone.utc) <= now and task.get("status") in {
                    "pending",
                    "retry",
                    "running",
                }:
                    task["isDue"] = True
                    due_tasks.append(task)
            task_queue = sorted(
                (
                    {**task, "isDue": task in due_tasks}
                    for task in tasks
                    if task.get("status") in {"pending", "retry", "running", "waiting"}
                    and task.get("nextAttemptAt")
                ),
                key=lambda task: str(task.get("nextAttemptAt") or ""),
            )[:20]
            cookie = self._cookie_gate_snapshot(db)
            guadao_runtime = runtimes.get(RUNTIME_GUADAO, {})
            runtime_cookie_gate = (
                guadao_runtime.get("payload", {}).get("cookieGate", {})
                if isinstance(guadao_runtime.get("payload"), dict)
                else {}
            )
            runtime_gate_status = str(runtime_cookie_gate.get("status") or "")
            if guadao_runtime.get("migrationHold"):
                cookie["status"] = "migration_hold"
            elif runtime_gate_status in {
                "ready",
                "preparing",
                "degraded",
                "idle",
                "scheduler_unavailable",
            }:
                cookie["status"] = runtime_gate_status
            counts = {
                "activeListings": self._operation_count(db, OP_SELL_STEAM, ["listed"]),
                "pendingListingConfirmations": self._operation_count(
                    db,
                    OP_SELL_STEAM,
                    ["listing_pending"],
                ),
                "pendingRebuys": self._operation_count(db, OP_REBUY_C5, ["pending"]),
                "deliveryPending": self._operation_count(db, OP_REBUY_C5, ["delivery_pending"]),
                "issues": len(self._issue_rows(db, include_acknowledged=False)),
            }
            queue_snapshot = {}
            snapshot_loader = getattr(db, "get_steam_queue_snapshot", None)
            if callable(snapshot_loader):
                queue_snapshot = snapshot_loader()
            circuits_loader = getattr(db, "list_steam_route_circuits", None)
            circuits = [self._public_circuit(row) for row in circuits_loader()] if callable(circuits_loader) else []
            config = load_strategy_config(self.settings)
            queue_counts = dict(queue_snapshot.get("counts") or {})
            queue_requests = list(queue_snapshot.get("requests") or [])
            running_request = next(
                (row for row in queue_requests if str(row.get("status")) == "running"),
                None,
            )
            recent_request_count = int(
                db.conn.execute(
                    "SELECT COUNT(*) AS count FROM steam_request_queue WHERE created_at >= ?",
                    ((_now_utc() - timedelta(seconds=60)).isoformat(),),
                ).fetchone()["count"]
            )
            priority_rows = []
            priority_labels = {
                0: "安全终态 / Cookie",
                1: "真实交易 / CLI",
                2: "账号状态同步",
                3: "行情观察",
            }
            for priority in range(4):
                priority_rows.append(
                    {
                        "priority": f"P{priority}",
                        "label": priority_labels[priority],
                        "queued": sum(
                            1
                            for row in queue_requests
                            if int(row.get("priority") or 0) == priority
                            and str(row.get("status")) == "pending"
                        ),
                    }
                )
            open_circuits = [row for row in circuits if row.get("state") in {"open", "half_open"}]
            cooldown_until = min(
                (
                    str(row.get("nextProbeAt") or row.get("cooldownUntil"))
                    for row in open_circuits
                    if row.get("nextProbeAt") or row.get("cooldownUntil")
                ),
                default=None,
            )
            try:
                from cs2_assistant.services.guadao_logging import get_guadao_event_logger

                recent_events = get_guadao_event_logger().query({"pageSize": 4}).get("events", [])
            except Exception:
                recent_events = []
            special_rules = []
            for rule in config.guadao_special_ratio_rules or []:
                if not isinstance(rule, dict):
                    continue
                market_hash_name = rule.get("marketHashName")
                special_rules.append(
                    {
                    "id": rule.get("ruleId"),
                    "marketHashName": market_hash_name,
                    "displayName": rule.get("nameCn"),
                    "maxRatioPct": float(rule.get("maxListingRatio") or 0) * 100.0,
                    "enabled": bool(rule.get("enabled", True)),
                    **self._latest_case_ratio_snapshot(db, market_hash_name),
                    }
                )
            public_issues = self._issue_rows(db, include_acknowledged=False)
            return {
                "generatedAt": utc_now_iso(),
                "backend": {"online": True, "workerAlive": self.alive, "lastError": self._last_error},
                "runtime": guadao_runtime,
                "runtimes": runtimes,
                "cookieGate": cookie,
                "counts": counts,
                "summary": {
                    "activeListings": counts["activeListings"],
                    "pendingListingConfirmations": counts["pendingListingConfirmations"],
                    "pendingRebuys": counts["pendingRebuys"],
                    "deliveryPending": counts["deliveryPending"],
                    "issueCount": counts["issues"],
                    "steamHeatPct": min(100.0, recent_request_count / 30.0 * 100.0),
                },
                "tasks": tasks,
                "dueTasks": due_tasks,
                "taskQueue": task_queue,
                "steamScheduler": {
                    "status": (
                        "unavailable"
                        if not self._steam_scheduler_ready
                        else "cooling"
                        if open_circuits
                        else "healthy"
                    ),
                    "ready": self._steam_scheduler_ready,
                    "error": self._steam_scheduler_error,
                    "queueLength": int(queue_counts.get("pending", 0)),
                    "activeRequest": running_request.get("route") if running_request else None,
                    "requestsPerMinute": recent_request_count,
                    "cooldownUntil": cooldown_until,
                    "priorities": priority_rows,
                    "queue": queue_snapshot,
                    "circuits": circuits,
                },
                "specialRatioRules": list(config.guadao_special_ratio_rules or []),
                "specialRules": special_rules,
                "settingsSummary": {
                    "guadaoMaxListingRatio": config.guadao_max_listing_ratio,
                    "autoListEnabled": config.auto_list_enabled,
                    "autoRebuyEnabled": config.auto_rebuy_enabled,
                },
                "issuesPreview": public_issues[:5],
                "issues": public_issues[:5],
                "recentLogs": [self._public_guadao_log(event) for event in recent_events],
            }
        finally:
            db.close()

    def _operation_count(self, db: Database, operation_type: str, statuses: list[str]) -> int:
        return len(
            db.list_pool_operations_by_type_and_statuses(
                operation_type,
                statuses=statuses,
                limit=10000,
            )
        )

    def _public_account_name(
        self,
        account_id: Any = None,
        steam_id64: Any = None,
    ) -> str | None:
        normalized_id = str(account_id or "").strip()
        normalized_steam = str(steam_id64 or "").strip()
        for account in self._accounts():
            if normalized_id and account.id == normalized_id:
                return account.name
            if normalized_steam and account.steam_id64 == normalized_steam:
                return account.name
        return normalized_id or normalized_steam or None

    def _latest_case_ratio_snapshot(
        self,
        db: Database,
        market_hash_name: Any,
    ) -> dict[str, Any]:
        target = str(market_hash_name or "").strip()
        if not target:
            return {"currentRatioPct": None, "currentRatioObservedAt": None}
        row = db.conn.execute(
            """
            SELECT listing_ratio, observed_at, c5_price_source, steam_price_source
            FROM guadao_case_ratio_snapshots
            WHERE market_hash_name = ?
              AND status = 'ok'
              AND listing_ratio IS NOT NULL
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (target,),
        ).fetchone()
        if row is None:
            return {"currentRatioPct": None, "currentRatioObservedAt": None}
        ratio = safe_float(row["listing_ratio"])
        return {
            "currentRatioPct": ratio * 100.0 if ratio is not None else None,
            "currentRatioObservedAt": row["observed_at"],
            "currentRatioSource": {
                "c5": row["c5_price_source"],
                "steam": row["steam_price_source"],
            },
        }

    def _public_task(self, row: Any) -> dict[str, Any]:
        payload = _task_payload(row)
        task_type = str(row["task_type"])
        return {
            "id": row["task_key"],
            "taskKey": row["task_key"],
            "source": row["source"],
            "taskType": task_type,
            "label": TASK_PUBLIC_LABELS.get(task_type, task_type.replace("_", " ")),
            "accountId": row["account_id"],
            "accountName": self._public_account_name(row["account_id"]),
            "operationId": row["operation_id"],
            "marketHashName": payload.get("marketHashName"),
            "reason": row["last_error"],
            "status": row["status"],
            "priority": int(row["priority"]),
            "nextAttemptAt": row["next_attempt_at"],
            "attemptCount": int(row["attempt_count"]),
            "lastError": row["last_error"],
            "payload": payload,
        }

    def _public_circuit(self, row: Any) -> dict[str, Any]:
        return {
            "circuitKey": row["circuit_key"],
            "scope": row["scope"],
            "accountId": row["account_id"],
            "route": row["route"],
            "state": row["state"],
            "consecutive429": int(row["consecutive_429"] or 0),
            "last429At": row["last_429_at"],
            "cooldownUntil": row["cooldown_until"],
            "nextProbeAt": row["next_probe_at"],
            "reason": row["reason"],
        }

    def _public_guadao_log(self, event: dict[str, Any]) -> dict[str, Any]:
        context = event.get("safe_context") if isinstance(event.get("safe_context"), dict) else {}
        operation_id = (
            context.get("operationId")
            or context.get("businessOperationId")
            or event.get("trade_id")
        )
        return {
            "id": event.get("event_id"),
            "timestamp": event.get("timestamp_utc"),
            "level": event.get("level"),
            "service": event.get("component") or event.get("provider"),
            "operation": event.get("operation"),
            "marketHashName": event.get("market_hash_name"),
            "accountId": event.get("account_id"),
            "steamId": event.get("steam_id64"),
            "accountName": (
                context.get("accountName")
                or self._public_account_name(event.get("account_id"), event.get("steam_id64"))
            ),
            "httpStatus": event.get("status_code"),
            "durationMs": event.get("elapsed_ms"),
            "message": event.get("message"),
            "requestId": event.get("request_id"),
            "operationId": operation_id,
            "tradeNo": event.get("trade_no"),
            "caller": context.get("source") or event.get("source"),
            "endpoint": event.get("endpoint"),
            "retryAfter": event.get("retry_after"),
            "detail": context,
        }

    def steam_scheduler_log_rows(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Expose redacted cross-executor Steam queue metadata for S4 correlation."""

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            rows = db.list_steam_requests(limit=max(1, min(int(limit), 5000)))
            result: list[dict[str, Any]] = []
            for row in rows:
                payload = _json_dict(row["payload_json"])
                request_result = _json_dict(row["result_json"])
                http_status = safe_int(row["http_status"])
                status = str(row["status"] or "")
                source = str(row["source"] or "")
                route = str(row["route"] or "")
                result.append(
                    {
                        "id": f"steamq:{row['request_id']}",
                        "timestamp": row["completed_at"] or row["updated_at"] or row["created_at"],
                        "level": (
                            "ERROR"
                            if status == "failed" or (http_status is not None and http_status >= 400)
                            else "INFO"
                        ),
                        "service": "steam_request_scheduler",
                        "operation": row["operation_id"] or payload.get("operation") or "shared_request",
                        "marketHashName": payload.get("marketHashName"),
                        "accountId": row["account_id"],
                        "accountName": self._public_account_name(row["account_id"]),
                        "httpStatus": http_status,
                        "durationMs": request_result.get("elapsedMs") or payload.get("elapsedMs"),
                        "message": f"{source} {str(row['method'] or '')} {route} {status}".strip(),
                        "requestId": row["request_id"],
                        "operationId": payload.get("businessOperationId"),
                        "tradeNo": payload.get("businessOperationId"),
                        "caller": source,
                        "endpoint": route,
                        "retryAfter": request_result.get("retryAfter") or payload.get("retryAfter"),
                        "detail": {
                            "source": source,
                            "status": status,
                            "priority": f"P{int(row['priority'] or 0)}",
                            "operationId": row["operation_id"],
                            "attemptCount": int(row["attempt_count"] or 0),
                            "createdAt": row["created_at"],
                            "completedAt": row["completed_at"],
                            "lastError": row["last_error"],
                            "payload": payload,
                            "result": request_result,
                        },
                    }
                )
            return result
        finally:
            db.close()

    def operations(
        self,
        *,
        limit: int = 50_000,
        page: int = 1,
        page_size: int = 10,
        keyword: str | None = None,
        account_name: str | None = None,
        status: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            rows = db.conn.execute(
                """
                SELECT o.*, i.name_cn
                FROM pool_operations o
                LEFT JOIN items i ON i.market_hash_name = o.market_hash_name
                WHERE o.strategy = 'guadao'
                ORDER BY o.created_at DESC, o.id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 50_000)),),
            ).fetchall()
            tasks = db.list_scheduled_tasks(source=RUNTIME_GUADAO, limit=5000)
            task_by_operation: dict[str, Any] = {}
            for task in tasks:
                operation_id = str(task["operation_id"] or "")
                if operation_id and operation_id not in task_by_operation:
                    task_by_operation[operation_id] = task
            sell_rows = [row for row in rows if str(row["operation_type"]) == OP_SELL_STEAM]
            rebuy_rows = [row for row in rows if str(row["operation_type"]) == OP_REBUY_C5]
            rebuy_by_sell: dict[int, list[Any]] = {}
            unlinked_rebuys: list[Any] = []
            for rebuy in rebuy_rows:
                source_sell_id = safe_int(_read_note(rebuy["note"]).get("sourceSellOperationId"))
                if source_sell_id is None:
                    unlinked_rebuys.append(rebuy)
                else:
                    rebuy_by_sell.setdefault(source_sell_id, []).append(rebuy)
            projected: list[dict[str, Any]] = []
            for sell in sell_rows:
                children = rebuy_by_sell.get(int(sell["id"]), [])
                child = max(children, key=lambda row: int(row["id"])) if children else None
                projected.append(
                    self._public_operation(
                        sell,
                        rebuy=child,
                        task=(
                            task_by_operation.get(str(child["id"]))
                            if child is not None
                            else task_by_operation.get(str(sell["id"]))
                        ),
                    )
                )
            for rebuy in unlinked_rebuys:
                projected.append(
                    self._public_operation(
                        rebuy,
                        task=task_by_operation.get(str(rebuy["id"])),
                    )
                )
            query = str(keyword or "").strip().lower()
            account_filter = str(account_name or "").strip()
            status_filter = str(status or "").strip()
            start_filter = _parse_iso(str(start_at or ""))
            end_filter = _parse_iso(str(end_at or ""))
            if start_at and start_filter is None:
                raise ValueError("startAt must be an ISO 8601 timestamp")
            if end_at and end_filter is None:
                raise ValueError("endAt must be an ISO 8601 timestamp")
            if start_filter is not None and start_filter.tzinfo is None:
                start_filter = start_filter.replace(tzinfo=timezone.utc)
            if end_filter is not None and end_filter.tzinfo is None:
                end_filter = end_filter.replace(tzinfo=timezone.utc)
            if start_filter is not None and end_filter is not None and end_filter < start_filter:
                raise ValueError("endAt must not be earlier than startAt")
            if query:
                projected = [
                    row
                    for row in projected
                    if query
                    in " ".join(
                        str(row.get(key) or "")
                        for key in (
                            "id",
                            "operationId",
                            "displayName",
                            "marketHashName",
                            "accountName",
                            "listingId",
                            "assetId",
                        )
                    ).lower()
                ]
            if account_filter:
                projected = [row for row in projected if row.get("accountName") == account_filter]
            if status_filter:
                projected = [row for row in projected if row.get("status") == status_filter]
            if start_filter is not None or end_filter is not None:
                filtered_by_created_at: list[dict[str, Any]] = []
                for item in projected:
                    created_at = _parse_iso(str(item.get("createdAt") or ""))
                    if created_at is None:
                        continue
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    created_at = created_at.astimezone(timezone.utc)
                    if start_filter is not None and created_at < start_filter.astimezone(timezone.utc):
                        continue
                    if end_filter is not None and created_at > end_filter.astimezone(timezone.utc):
                        continue
                    filtered_by_created_at.append(item)
                projected = filtered_by_created_at
            projected.sort(key=lambda row: str(row.get("updatedAt") or row.get("createdAt") or ""), reverse=True)
            total = len(projected)
            safe_page_size = max(1, min(int(page_size), 100))
            safe_page = max(1, int(page))
            start = (safe_page - 1) * safe_page_size
            page_rows = projected[start : start + safe_page_size]
            summary = {
                "total": total,
                "pendingConfirmation": sum(row["status"] == "listing_pending" for row in projected),
                "steamListed": sum(row["status"] == "listed" for row in projected),
                "pendingRebuy": sum(row["status"] == "sold" for row in projected),
                "deliveryPending": sum(row["status"] == "delivery_pending" for row in projected),
                "completed": sum(row["status"] == "completed" for row in projected),
            }
            return {
                "generatedAt": utc_now_iso(),
                "items": page_rows,
                "operations": page_rows,
                "total": total,
                "page": safe_page,
                "pageSize": safe_page_size,
                "summary": summary,
                "runtime": (
                    self._public_runtime_row(runtime_row)
                    if (runtime_row := db.get_executor_runtime_state(RUNTIME_GUADAO)) is not None
                    else None
                ),
                "dateField": "createdAt",
                "accounts": [
                    {
                        "id": account.id,
                        "name": account.name,
                        "steamId": account.steam_id64,
                    }
                    for account in self._accounts()
                ],
                "truncated": len(rows) >= max(1, min(int(limit), 50_000)),
            }
        finally:
            db.close()

    def _public_operation(
        self,
        row: Any,
        *,
        rebuy: Any | None = None,
        task: Any | None = None,
    ) -> dict[str, Any]:
        note = _read_note(row["note"])
        rebuy_note = _read_note(rebuy["note"]) if rebuy is not None else {}
        operation_type = str(row["operation_type"])
        raw_status = str(rebuy["status"] if rebuy is not None else row["status"])
        if rebuy is not None:
            if raw_status == "pending":
                status, step, stage = "sold", 4, "已卖出待补仓"
            elif raw_status == "delivery_pending":
                status, step, stage = "delivery_pending", 5, "C5 发货确认"
            elif raw_status == "completed":
                status, step, stage = "completed", 6, "已闭环"
            else:
                status, step, stage = raw_status, 5, "补仓异常"
        elif operation_type == OP_SELL_STEAM:
            status = raw_status
            if status == "listing_pending":
                step, stage = 2, "挂单确认"
            elif status == "listed":
                step, stage = 3, "Steam 在售"
            elif status == "sold":
                step, stage = 4, "已卖出待补仓"
            else:
                step, stage = 1, "资产与上架"
        else:
            status = raw_status
            step, stage = (6, "已闭环") if status == "completed" else (4, "补仓处理中")
        account_name = note.get("steamAccountName") or rebuy_note.get("steamAccountName")
        timeline: list[dict[str, Any]] = [
            {"at": row["created_at"], "label": "创建挂刀流水", "detail": operation_type, "status": "done"}
        ]
        for key, label in (
            ("activeVerifiedAt", "Steam 挂单已确认"),
            ("steamSoldAt", "Steam 官方确认售出"),
            ("c5OrderSubmittedAt", "C5 补仓已下单"),
            ("c5OrderCheckedAt", "C5 发货状态复查"),
            ("c5DeliveryTimedOutAt", "C5 24小时未发货，自动失败"),
        ):
            value = rebuy_note.get(key) or note.get(key)
            if value:
                timeline.append(
                    {
                        "at": _normalize_timestamp_iso(value),
                        "label": label,
                        "detail": "",
                        "status": "done",
                    }
                )
        completed_at = rebuy["completed_at"] if rebuy is not None else row["completed_at"]
        if status == "completed" and completed_at:
            timeline.append({"at": completed_at, "label": "流水闭环", "detail": "", "status": "done"})
        task_payload = _task_payload(task) if task is not None else {}
        c5_order_submitted_at = _normalize_timestamp_iso(
            rebuy_note.get("c5OrderSubmittedAt") or note.get("c5OrderSubmittedAt")
        )
        c5_delivery_deadline_at = None
        submitted_at = _parse_iso(c5_order_submitted_at)
        if submitted_at is not None:
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
            c5_delivery_deadline_at = (
                submitted_at.astimezone(timezone.utc) + timedelta(hours=24)
            ).isoformat()
        return {
            "id": int(row["id"]),
            "operationId": f"GD-{int(row['id'])}",
            "marketHashName": row["market_hash_name"],
            "nameCn": row["name_cn"] if "name_cn" in row.keys() else None,
            "displayName": row["name_cn"] if "name_cn" in row.keys() else row["market_hash_name"],
            "operationType": operation_type,
            "status": status,
            "stage": stage,
            "step": step,
            "stepIndex": step,
            "quantity": int(row["quantity"] or 1),
            "expectedPrice": row["expected_price"],
            "actualPrice": row["actual_price"],
            "assetId": row["asset_id"],
            "listingId": note.get("listingId"),
            "c5OrderId": (
                rebuy_note.get("c5OrderId")
                or rebuy_note.get("c5TradeOrderId")
                or note.get("c5OrderId")
                or note.get("c5TradeOrderId")
            ),
            "steamAccountId": note.get("steamAccountId"),
            "steamAccountName": account_name,
            "accountName": account_name,
            "steamId": note.get("steamId64"),
            "listingRatioAtOpen": note.get("listingRatioAtOpen"),
            "maxRebuyRatioAtOpen": note.get("maxRebuyRatioAtOpen"),
            "guadaoMaxListingRatioAtOpen": note.get("guadaoMaxListingRatioAtOpen"),
            "ratioRuleSource": note.get("guadaoRatioRuleSource"),
            "ratioRuleId": note.get("guadaoRatioRuleId"),
            "ratioRuleVersion": note.get("guadaoRatioRuleVersion"),
            "steamListPrice": note.get("steamListPrice"),
            "steamNetAmount": note.get("steamSellerNetPrice"),
            "steamSoldAt": _normalize_timestamp_iso(
                rebuy_note.get("steamSoldAt") or note.get("steamSoldAt")
            ),
            "c5OrderSubmittedAt": c5_order_submitted_at,
            "c5DeliveryDeadlineAt": c5_delivery_deadline_at,
            "c5RebuyPrice": (
                rebuy["actual_price"] or rebuy["expected_price"]
                if rebuy is not None
                else row["actual_price"] or row["expected_price"]
            ),
            "createdAt": row["created_at"],
            "updatedAt": completed_at or (rebuy["created_at"] if rebuy is not None else row["created_at"]),
            "completedAt": completed_at,
            "nextAttemptAt": task["next_attempt_at"] if task is not None else None,
            "nextTaskLabel": (
                TASK_PUBLIC_LABELS.get(
                    str(task["task_type"]),
                    str(task["task_type"]).replace("_", " "),
                )
                if task is not None
                else None
            ),
            "nextTaskReason": task["last_error"] if task is not None else None,
            "timeline": timeline,
            "note": {**note, **rebuy_note, "nextTaskPayload": task_payload},
        }

    def _issue_rows(self, db: Database, *, include_acknowledged: bool) -> list[dict[str, Any]]:
        ack_loader = getattr(db, "list_guadao_issue_acknowledgements", None)
        acknowledgements = {
            str(row["issue_key"]): row for row in ack_loader()
        } if callable(ack_loader) else {}
        rows = db.conn.execute(
            """
            SELECT o.*, i.name_cn
            FROM pool_operations o
            LEFT JOIN items i ON i.market_hash_name = o.market_hash_name
            WHERE o.strategy = 'guadao'
              AND o.status IN ('manual_required','listing_failed','failed')
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT 1000
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            issue_key = f"pool-operation:{row['id']}"
            ack = acknowledgements.get(issue_key)
            acknowledged = bool(ack and ack["acknowledged"])
            if acknowledged and not include_acknowledged:
                continue
            note = _read_note(row["note"])
            operation_type = str(row["operation_type"] or "")
            raw_status = str(row["status"] or "")
            reason = str(
                note.get("manualReviewReason")
                or note.get("failedReason")
                or note.get("staleListedManualRequiredReason")
                or note.get("lastError")
                or raw_status
            )
            reason_lower = reason.lower()
            if "steam" in reason_lower or operation_type == OP_SELL_STEAM:
                issue_type = "steam_terminal_state"
                category = "steam"
                title = "Steam 挂单或成交终态需要处理"
                recommendation = "请先核对 Steam 活跃挂单、市场历史和资产状态；终态不明确时不要释放资产或重复上架。"
            elif "c5" in reason_lower or operation_type == OP_REBUY_C5:
                issue_type = "c5_rebuy_state"
                category = "c5"
                title = "C5 补仓终态需要处理"
                recommendation = "请按 outTradeNo、orderId 和资产交付证据核对 C5 终态；未确认前不要重复补仓。"
            else:
                issue_type = "local_state"
                category = "local"
                title = "本地流水状态需要安全复核"
                recommendation = "请结合关联日志与远端终态证据复核，确认后再恢复或结束本地流水。"
            severity = "high" if raw_status in {"manual_required", "failed"} else "medium"
            account_name = (
                note.get("steamAccountName")
                or note.get("accountName")
                or note.get("steamAccountId")
            )
            review_account_id = str(note.get("steamAccountId") or "").strip()
            can_queue_safe_review = bool(
                operation_type == OP_SELL_STEAM and review_account_id
            )
            if can_queue_safe_review:
                safe_review_block_reason = None
            elif operation_type != OP_SELL_STEAM:
                safe_review_block_reason = "该问题不是 Steam 卖出流水，不能自动排队安全复核"
            else:
                safe_review_block_reason = "关联流水缺少 Steam 账号，不能自动排队安全复核"
            first_seen_at = str(
                note.get("manualRequiredAt")
                or note.get("staleListedManualRequiredAt")
                or row["created_at"]
                or ""
            ) or None
            last_seen_at = str(
                note.get("lastCheckedAt")
                or note.get("staleListedCheckedAt")
                or row["completed_at"]
                or row["created_at"]
                or ""
            ) or None
            evidence = [
                {"label": "本地流水", "value": f"GD-{int(row['id'])}"},
                {"label": "当前状态", "value": raw_status},
            ]
            for label, value in (
                ("assetId", row["asset_id"]),
                ("listingId", note.get("listingId")),
                ("C5 orderId", note.get("c5OrderId") or note.get("c5TradeOrderId")),
                ("原因", reason),
            ):
                if value not in (None, ""):
                    evidence.append({"label": label, "value": str(value)})
            timeline = [
                {
                    "at": row["created_at"],
                    "label": "流水创建",
                    "detail": operation_type,
                }
            ]
            if first_seen_at and first_seen_at != row["created_at"]:
                timeline.append(
                    {
                        "at": first_seen_at,
                        "label": "首次进入待处理",
                        "detail": reason,
                    }
                )
            if last_seen_at and last_seen_at not in {row["created_at"], first_seen_at}:
                timeline.append(
                    {
                        "at": last_seen_at,
                        "label": "最近复核",
                        "detail": reason,
                    }
                )
            result.append(
                {
                    "id": issue_key,
                    "issueId": issue_key,
                    "operationId": int(row["id"]),
                    "issueType": issue_type,
                    "category": category,
                    "title": title,
                    "severity": severity,
                    "marketHashName": row["market_hash_name"],
                    "nameCn": row["name_cn"],
                    "status": "open",
                    "rawStatus": raw_status,
                    "summary": reason,
                    "detail": reason,
                    "reason": reason,
                    "accountId": note.get("steamAccountId"),
                    "accountName": account_name,
                    "steamId": note.get("steamId64"),
                    "assetId": row["asset_id"],
                    "listingId": note.get("listingId"),
                    "acknowledged": acknowledged,
                    "firstSeenAt": first_seen_at,
                    "lastSeenAt": last_seen_at,
                    "repeatCount": max(1, int(note.get("manualReviewCount") or 1)),
                    "evidence": evidence,
                    "timeline": timeline,
                    "recommendation": recommendation,
                    "canQueueSafeReview": can_queue_safe_review,
                    "safeReviewBlockReason": safe_review_block_reason,
                    "createdAt": row["created_at"],
                    "note": note,
                }
            )
        return result

    def issues(self, *, include_acknowledged: bool = False) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            items = self._issue_rows(db, include_acknowledged=include_acknowledged)
            summary = {
                "total": sum(not bool(item.get("acknowledged")) for item in items),
                "pendingReview": sum(
                    item.get("severity") in {"critical", "high"}
                    and not bool(item.get("acknowledged"))
                    for item in items
                ),
                "steam": sum(
                    item.get("category") == "steam" and not bool(item.get("acknowledged"))
                    for item in items
                ),
                "c5": sum(
                    item.get("category") == "c5" and not bool(item.get("acknowledged"))
                    for item in items
                ),
                "local": sum(
                    item.get("category") == "local" and not bool(item.get("acknowledged"))
                    for item in items
                ),
                "acknowledged": sum(bool(item.get("acknowledged")) for item in items),
            }
            return {
                "generatedAt": utc_now_iso(),
                "items": items,
                "issues": items,
                "total": len(items),
                "summary": summary,
                "runtime": (
                    self._public_runtime_row(runtime_row)
                    if (runtime_row := db.get_executor_runtime_state(RUNTIME_GUADAO)) is not None
                    else None
                ),
            }
        finally:
            db.close()

    def acknowledge_issue(
        self,
        issue_id: str,
        *,
        acknowledged: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            setter = getattr(db, "set_guadao_issue_acknowledgement", None)
            if not callable(setter):
                raise RuntimeError("guadao issue acknowledgement storage unavailable")
            row = setter(
                str(issue_id),
                acknowledged=bool(acknowledged),
                reason=reason,
                actor="web",
            )
            return _row_dict(row)
        finally:
            db.close()

    def queue_issue_safe_review(self, issue_id: str) -> dict[str, Any]:
        """Queue an existing account sync for a Steam/local issue.

        The API handler never calls Steam directly.  The unified worker claims
        the normal account-sync task, so request priority, Cookie health,
        quiet windows, Retry-After and migration guards remain authoritative.
        """

        key = str(issue_id or "").strip()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            issue = next(
                (row for row in self._issue_rows(db, include_acknowledged=True) if str(row.get("id")) == key),
                None,
            )
            if issue is None:
                raise RuntimeError("待处理问题不存在或已经自动闭环")
            operation_id = safe_int(issue.get("operationId"))
            if operation_id is None:
                raise RuntimeError("问题缺少关联流水，不能自动发起安全复核")
            operation = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise RuntimeError("关联挂刀流水不存在")
            if str(operation["operation_type"] or "") != OP_SELL_STEAM:
                raise RuntimeError("该问题没有可自动排队的 Steam 安全复核，请按推荐操作人工核对 C5 终态")
            account_id = str(issue.get("accountId") or self._guadao_operation_account_id(operation) or "").strip()
            if not account_id:
                raise RuntimeError("关联流水缺少 Steam 账号，不能自动发起安全复核")
            task_key = f"steam-sync:{account_id}"
            current = db.get_scheduled_task(task_key)
            if current is not None and str(current["status"] or "") == "running":
                raise RuntimeError("该账号的 Steam 安全复核正在运行，请等待当前任务完成")
            if current is None:
                self._ensure_task(
                    db,
                    task_key,
                    source=RUNTIME_GUADAO,
                    task_type=TASK_STEAM_ACCOUNT_SYNC,
                    next_attempt_at=utc_now_iso(),
                    account_id=account_id,
                    priority=0,
                    payload={"manualSafeReviewIssueId": key, "operationId": operation_id},
                )
            else:
                current_payload = _task_payload(current)
                db.upsert_scheduled_task(
                    task_key,
                    source=str(current["source"] or RUNTIME_GUADAO),
                    task_type=str(current["task_type"] or TASK_STEAM_ACCOUNT_SYNC),
                    next_attempt_at=utc_now_iso(),
                    account_id=account_id,
                    operation_id=current["operation_id"],
                    payload={
                        **current_payload,
                        "manualSafeReviewIssueId": key,
                        "operationId": operation_id,
                    },
                    status="pending",
                    priority=0,
                    last_error=f"manual_safe_review:{key}",
                )
        finally:
            db.close()
        self._emit_guadao_runtime_event(
            operation="manual_safe_review_queued",
            message="人工请求的安全复核已进入统一到期任务队列",
            issueId=key,
            operationId=operation_id,
            accountId=account_id,
            taskKey=task_key,
        )
        self.wake()
        return {
            "ok": True,
            "queued": True,
            "issueId": key,
            "operationId": operation_id,
            "accountId": account_id,
            "taskKey": task_key,
            "message": "已排队；复核将继续服从迁移保护、Cookie 门禁、共享 Steam 调度和终态证据规则",
        }

    def settings_payload(self) -> dict[str, Any]:
        config = load_strategy_config(self.settings)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            audit_loader = getattr(db, "list_strategy_config_audit", None)
            audit = [self._public_config_audit(row) for row in audit_loader(limit=100)] if callable(audit_loader) else []
            runtime = db.get_executor_runtime_state(RUNTIME_GUADAO)
            runtime_payload = self._public_runtime_row(runtime) if runtime is not None else None
            schedule = config.effective_guadao_task_schedule()
            rebuy_tiers = list(schedule.get("rebuyRetryTiers") or [])
            delivery_tiers = list(schedule.get("deliveryConfirmationTiers") or [])
            latest_config_at = audit[0].get("at") if audit else None

            def rule_updated_at(market_hash_name: str) -> str | None:
                target = str(market_hash_name or "").strip()
                if not target:
                    return None
                for audit_row in audit:
                    audit_text = json.dumps(
                        {
                            "oldValue": audit_row.get("oldValue"),
                            "newValue": audit_row.get("newValue"),
                            "diff": audit_row.get("diff"),
                        },
                        ensure_ascii=False,
                    )
                    if target in audit_text:
                        return str(audit_row.get("at") or "") or None
                return None

            special_rules = []
            for rule in config.guadao_special_ratio_rules or []:
                if not isinstance(rule, dict):
                    continue
                market_hash_name = rule.get("marketHashName")
                special_rules.append(
                    {
                    "id": rule.get("ruleId"),
                    "marketHashName": market_hash_name,
                    "displayName": rule.get("nameCn"),
                    "maxRatioPct": float(rule.get("maxListingRatio") or 0) * 100.0,
                    "enabled": bool(rule.get("enabled", True)),
                    "version": int(rule.get("version") or 1),
                    "updatedAt": rule_updated_at(str(market_hash_name or "")),
                    **self._latest_case_ratio_snapshot(db, market_hash_name),
                    }
                )
            settings = {
                "runtime": runtime_payload,
                "global": {
                    "maxListingRatioPct": float(config.guadao_max_listing_ratio) * 100.0,
                    "steamNetFactorPct": float(config.steam_net_factor) * 100.0,
                    "maxNewListingsPerCycle": int(config.max_list_per_cycle),
                    "caseMaxOpenCount": int(config.case_max_open_guadao_count),
                    "autoListing": bool(config.auto_list_enabled),
                    "autoRebuy": bool(config.auto_rebuy_enabled),
                    "lastModifiedAt": latest_config_at,
                },
                "specialRules": special_rules,
                "timePolicy": {
                    "scanMinutes": float(schedule.get("scanIntervalSeconds") or 300.0) / 60.0,
                    "steamSyncSeconds": float(schedule.get("steamSyncIntervalSeconds") or 120.0),
                    "actionConfirmSeconds": [
                        float(value)
                        for value in schedule.get("actionConfirmationDelaysSeconds") or []
                    ],
                    "soldEvidenceMinutes": [
                        float(value) / 60.0
                        for value in schedule.get("saleEvidenceDelaysSeconds") or []
                    ],
                    "rebuyMinutes": [
                        float(tier.get("intervalSeconds") or 0) / 60.0
                        for tier in rebuy_tiers
                        if isinstance(tier, dict)
                    ],
                    "deliveryMinutes": [
                        float(tier.get("intervalSeconds") or 0) / 60.0
                        for tier in delivery_tiers
                        if isinstance(tier, dict)
                    ],
                    "staleListedRecheckHours": float(config.stale_listed_recheck_hours),
                    "staleListedMaxRatioTolerancePct": float(
                        config.stale_listed_max_ratio_tolerance_pct
                    ),
                },
                "steamScheduler": {
                    "respectRetryAfter": True,
                    "adaptiveCooldown": True,
                },
                "audit": audit,
                # Compatibility fields keep older S5 builds usable while the
                # nested contract remains the canonical public shape.
                "guadaoMaxListingRatio": config.guadao_max_listing_ratio,
                "steamNetFactor": config.steam_net_factor,
                "maxNewListingsPerCycle": config.max_list_per_cycle,
                "caseMaxOpenCount": config.case_max_open_guadao_count,
                "autoListEnabled": config.auto_list_enabled,
                "autoRebuyEnabled": config.auto_rebuy_enabled,
                "specialCaseRatioRules": list(config.guadao_special_ratio_rules or []),
                "taskSchedule": schedule,
                "staleListedRecheckHours": config.stale_listed_recheck_hours,
                "staleListedMaxRatioTolerancePct": (
                    config.stale_listed_max_ratio_tolerance_pct
                ),
            }
            return {
                "generatedAt": utc_now_iso(),
                "settings": settings,
                "runtime": runtime_payload,
                "guadaoMaxListingRatio": config.guadao_max_listing_ratio,
                "autoListEnabled": config.auto_list_enabled,
                "autoRebuyEnabled": config.auto_rebuy_enabled,
                "specialCaseRatioRules": list(config.guadao_special_ratio_rules or []),
                "taskSchedule": config.effective_guadao_task_schedule(),
                "audit": audit,
            }
        finally:
            db.close()

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._config_lock:
            old = load_strategy_config(self.settings)
            old_dict = old.to_dict()
            config = StrategyConfig.from_dict(old_dict)
            nested_global = payload.get("global") if isinstance(payload.get("global"), dict) else {}
            nested_rules = payload.get("specialRules")
            nested_time = payload.get("timePolicy") if isinstance(payload.get("timePolicy"), dict) else None
            if nested_global:
                payload = {
                    **payload,
                    "guadaoMaxListingRatio": float(nested_global.get("maxListingRatioPct")) / 100.0,
                    "autoListEnabled": bool(nested_global.get("autoListing")),
                    "autoRebuyEnabled": bool(nested_global.get("autoRebuy")),
                    "maxListPerCycle": int(nested_global.get("maxNewListingsPerCycle")),
                    "caseMaxOpenGuadaoCount": int(nested_global.get("caseMaxOpenCount")),
                }
            if isinstance(nested_rules, list):
                payload["specialCaseRatioRules"] = [
                    {
                        "ruleId": rule.get("id"),
                        "version": rule.get("version"),
                        "marketHashName": rule.get("marketHashName"),
                        "nameCn": rule.get("displayName"),
                        "maxListingRatio": float(rule.get("maxRatioPct")) / 100.0,
                        "enabled": bool(rule.get("enabled", True)),
                    }
                    for rule in nested_rules
                    if isinstance(rule, dict)
                ]
            if nested_time is not None:
                current_schedule = config.effective_guadao_task_schedule()
                rebuy_tiers = [dict(tier) for tier in current_schedule.get("rebuyRetryTiers") or []]
                delivery_tiers = [
                    dict(tier) for tier in current_schedule.get("deliveryConfirmationTiers") or []
                ]
                for tier, minutes in zip(rebuy_tiers, nested_time.get("rebuyMinutes") or []):
                    tier["intervalSeconds"] = float(minutes) * 60.0
                for tier, minutes in zip(delivery_tiers, nested_time.get("deliveryMinutes") or []):
                    tier["intervalSeconds"] = float(minutes) * 60.0
                payload["taskSchedule"] = {
                    **current_schedule,
                    "scanIntervalSeconds": float(nested_time.get("scanMinutes")) * 60.0,
                    "steamSyncIntervalSeconds": float(nested_time.get("steamSyncSeconds")),
                    "actionConfirmationDelaysSeconds": [
                        float(value) for value in nested_time.get("actionConfirmSeconds") or []
                    ],
                    "saleEvidenceDelaysSeconds": [
                        float(value) * 60.0
                        for value in nested_time.get("soldEvidenceMinutes") or []
                    ],
                    "rebuyRetryTiers": rebuy_tiers,
                    "deliveryConfirmationTiers": delivery_tiers,
                }
                payload["staleListedRecheckHours"] = float(
                    nested_time.get("staleListedRecheckHours")
                )
                payload["staleListedMaxRatioTolerancePct"] = float(
                    nested_time.get("staleListedMaxRatioTolerancePct")
                )
            if "guadaoMaxListingRatio" in payload:
                ratio = float(payload["guadaoMaxListingRatio"])
                if not 0 < ratio <= 0.80:
                    raise ValueError("全局最大挂刀比例必须大于 0 且不超过 80%")
                config.guadao_max_listing_ratio = ratio
            if "autoListEnabled" in payload:
                config.auto_list_enabled = bool(payload["autoListEnabled"])
            if "autoRebuyEnabled" in payload:
                config.auto_rebuy_enabled = bool(payload["autoRebuyEnabled"])
            if "maxListPerCycle" in payload:
                value = int(payload["maxListPerCycle"])
                if value < 0:
                    raise ValueError("每轮最大新上架不得小于 0")
                config.max_list_per_cycle = value
            if "caseMaxOpenGuadaoCount" in payload:
                value = int(payload["caseMaxOpenGuadaoCount"])
                if value < 1:
                    raise ValueError("箱子最大活跃挂单必须至少为 1")
                config.case_max_open_guadao_count = value
            if "staleListedRecheckHours" in payload:
                value = float(payload["staleListedRecheckHours"])
                if value < 1:
                    raise ValueError("老挂单复查间隔不得低于 1 小时")
                config.stale_listed_recheck_hours = value
            if "staleListedMaxRatioTolerancePct" in payload:
                value = float(payload["staleListedMaxRatioTolerancePct"])
                if value < 0 or value > 20:
                    raise ValueError("老挂单比例容忍必须在 0 到 20 个百分点之间")
                config.stale_listed_max_ratio_tolerance_pct = value
            if "specialCaseRatioRules" in payload:
                config.guadao_special_ratio_rules = self._validate_special_ratio_rules(
                    payload["specialCaseRatioRules"],
                    global_ratio=config.guadao_max_listing_ratio,
                    confirm_high=bool(payload.get("confirmHighRatio")),
                )
            if "taskSchedule" in payload:
                config.guadao_task_schedule = self._validate_task_schedule(payload["taskSchedule"])
            save_strategy_config(self.settings, config)
            new_dict = config.to_dict()
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                writer = getattr(db, "add_strategy_config_audit", None)
                if callable(writer):
                    writer(
                        config_scope="guadao",
                        event_type="update",
                        old_value=old_dict.get("guadaoBalance", {}),
                        new_value=new_dict.get("guadaoBalance", {}),
                        diff={key: payload[key] for key in payload if key != "confirmHighRatio"},
                        actor="web",
                        reason=str(payload.get("reason") or "挂刀策略设置更新"),
                    )
            finally:
                db.close()
        self.wake()
        return self.settings_payload()

    def _validate_special_ratio_rules(
        self,
        raw_rules: Any,
        *,
        global_ratio: float,
        confirm_high: bool,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_rules, list):
            raise ValueError("specialCaseRatioRules 必须是数组")
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        existing_rules = {
            str(rule.get("marketHashName") or ""): rule
            for rule in load_strategy_config(self.settings).guadao_special_ratio_rules or []
            if isinstance(rule, dict) and str(rule.get("marketHashName") or "")
        }
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            for raw in raw_rules:
                if not isinstance(raw, dict):
                    raise ValueError("特殊箱子规则格式错误")
                market_hash_name = str(raw.get("marketHashName") or "").strip()
                if not market_hash_name or market_hash_name in seen:
                    raise ValueError("特殊箱子 marketHashName 不能为空或重复")
                item = db.get_item(market_hash_name)
                if item is None:
                    raise ValueError(f"本地饰品目录不存在: {market_hash_name}")
                if not _catalog_item_matches_guadao_case_semantics(item):
                    raise ValueError(f"特殊箱子规则只允许当前挂刀 Case 范围内的物品: {market_hash_name}")
                ratio = float(raw.get("maxListingRatio"))
                if ratio < float(global_ratio) or ratio > 0.80:
                    raise ValueError("特殊箱子比例必须不低于全局比例且不超过 80%")
                if ratio > 0.75 and not confirm_high:
                    raise ValueError("超过 75% 的特殊比例需要二次确认")
                enabled = bool(raw.get("enabled", True))
                existing = existing_rules.get(market_hash_name)
                if existing is None:
                    version = max(1, int(raw.get("version") or 1))
                    rule_id = str(raw.get("ruleId") or uuid.uuid4().hex[:12])
                else:
                    changed = (
                        abs(float(existing.get("maxListingRatio") or 0) - ratio) > 1e-12
                        or bool(existing.get("enabled", True)) != enabled
                    )
                    version = max(1, int(existing.get("version") or 1)) + (1 if changed else 0)
                    rule_id = str(
                        existing.get("ruleId") or raw.get("ruleId") or uuid.uuid4().hex[:12]
                    )
                seen.add(market_hash_name)
                result.append(
                    {
                        "ruleId": rule_id,
                        "version": version,
                        "marketHashName": market_hash_name,
                        "nameCn": str(raw.get("nameCn") or item["name_cn"] or ""),
                        "maxListingRatio": ratio,
                        "enabled": enabled,
                    }
                )
        finally:
            db.close()
        return result

    def _validate_task_schedule(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("taskSchedule 必须是对象")
        defaults = StrategyConfig.default_guadao_task_schedule()
        result = {**defaults, **raw}
        if float(result["scanIntervalSeconds"]) < 60:
            raise ValueError("完整扫描间隔不得低于 1 分钟")
        if float(result["steamSyncIntervalSeconds"]) < 30:
            raise ValueError("普通 Steam 同步间隔不得低于 30 秒")
        action = [float(value) for value in result.get("actionConfirmationDelaysSeconds") or []]
        if not action or min(action) < 2 or action != sorted(action):
            raise ValueError("动作确认间隔不得低于 2 秒且必须非递减")
        sale = [float(value) for value in result.get("saleEvidenceDelaysSeconds") or []]
        if not sale or min(sale) < 0 or sale != sorted(sale):
            raise ValueError("卖出证据复核间隔不得为负数且必须非递减")
        for key in ("rebuyRetryTiers", "deliveryConfirmationTiers"):
            previous = 0.0
            tiers = result.get(key)
            if not isinstance(tiers, list) or not tiers:
                raise ValueError(f"{key} 不能为空")
            for tier in tiers:
                if not isinstance(tier, dict):
                    raise ValueError(f"{key} 格式错误")
                interval = float(tier.get("intervalSeconds") or 0)
                if interval < 30 or interval < previous:
                    raise ValueError("C5 复查间隔不得低于 30 秒且必须非递减")
                previous = interval
        result["scanIntervalSeconds"] = float(result["scanIntervalSeconds"])
        result["steamSyncIntervalSeconds"] = float(result["steamSyncIntervalSeconds"])
        result["actionConfirmationDelaysSeconds"] = action
        result["saleEvidenceDelaysSeconds"] = sale
        return result

    def search_items(self, query: str, *, limit: int = 30) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            safe_limit = max(1, min(int(limit), 100))
            keyword = str(query or "").strip()
            like = f"%{keyword}%"
            rows = db.conn.execute(
                """
                SELECT market_hash_name, name_cn, c5_item_id, raw_json
                FROM items
                WHERE name_cn LIKE ? OR market_hash_name LIKE ?
                ORDER BY name_cn ASC
                """,
                (like, like),
            ).fetchall()
            rows = [
                row for row in rows if _catalog_item_matches_guadao_case_semantics(row)
            ][:safe_limit]
            return {
                "items": [
                    {
                        "marketHashName": row["market_hash_name"],
                        "nameCn": row["name_cn"],
                        "displayName": row["name_cn"],
                        "name": row["name_cn"],
                        "c5ItemId": row["c5_item_id"],
                    }
                    for row in rows
                ]
            }
        finally:
            db.close()

    def _public_config_audit(self, row: Any) -> dict[str, Any]:
        reason = row["reason"]
        return {
            "id": int(row["id"]),
            "scope": row["config_scope"],
            "eventType": row["event_type"],
            "oldValue": _json_dict(row["old_value_json"]),
            "newValue": _json_dict(row["new_value_json"]),
            "diff": _json_dict(row["diff_json"]),
            "actor": row["actor"],
            "reason": reason,
            "createdAt": row["created_at"],
            "at": row["created_at"],
            "summary": reason or "挂刀策略设置更新",
            "changes": json.dumps(_json_dict(row["diff_json"]), ensure_ascii=False),
        }

    def is_backend_worker_active(self) -> bool:
        if self.alive:
            return True
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            for row in db.list_executor_runtime_states():
                heartbeat = _parse_iso(str(row["heartbeat_at"] or ""))
                if heartbeat is None:
                    continue
                if heartbeat.tzinfo is None:
                    heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                if (_now_utc() - heartbeat).total_seconds() <= WORKER_HEARTBEAT_STALE_SECONDS:
                    return True
            return False
        finally:
            db.close()
