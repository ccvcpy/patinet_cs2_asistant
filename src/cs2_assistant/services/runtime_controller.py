from __future__ import annotations

import json
import hashlib
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
    POOL_STATUS_HOLDING,
    POOL_STATUS_PENDING_REBUY,
    StrategyConfig,
    looks_like_weapon_case_name,
    normalize_guadao_item_scope,
)
from cs2_assistant.services.executor_engine import (
    C5_DELIVERY_FAILED,
    C5_DELIVERY_STATUS_KEY,
    ExecutionEngine,
    _normalize_timestamp_iso,
    _parse_iso,
    _read_note,
)
from cs2_assistant.services.c5_ip_circuit import (
    is_c5_ip_circuit_open,
    notify_c5_ip_circuit_if_pending,
    probe_c5_ip_circuit,
)
from cs2_assistant.services.c5_research_scan import (
    create_c5_research_scan,
    get_c5_research_scan,
    run_c5_research_scan_chunk,
    set_c5_research_scan_action,
)
from cs2_assistant.services.guadao_audit import (
    cancel_guadao_audit_run as cancel_guadao_audit_record,
    create_guadao_audit_run as create_guadao_audit_record,
    get_guadao_audit_run as get_guadao_audit_record,
    retry_guadao_audit_run as retry_guadao_audit_record,
    run_guadao_audit,
)
from cs2_assistant.services.profit_trade import (
    PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY,
    PROFIT_TRADE_CYCLE_INTERVAL_SECONDS,
    _profit_trade_protection_reason,
    _profit_trade_type_block_reason,
    execute_manual_profit_trade_request,
    execute_profit_trade_list_c5,
    recover_unverified_profit_trade_steam_buys,
    refresh_profit_trade_selection_watch,
    refresh_profit_trade_listings,
    refresh_profit_trade_sales,
    run_profit_trade_once,
    set_profit_trade_enabled,
)
from cs2_assistant.services.strategy import load_strategy_config, save_strategy_config
from cs2_assistant.utils import safe_float, safe_int, utc_now_iso


RUNTIME_GUADAO = "guadao"
RUNTIME_PROFIT_TRADE = "profit_trade"
RUNTIME_KEYS = (RUNTIME_GUADAO, RUNTIME_PROFIT_TRADE)

TASK_GUADAO_SCAN = "guadao_scan"
TASK_STALE_LISTING_RECHECK = "stale_listing_recheck"
TASK_STEAM_ACCOUNT_SYNC = "steam_account_sync"
TASK_STEAM_LISTING_CONFIRM = "steam_listing_confirmation"
TASK_STEAM_SALE_EVIDENCE = "steam_sale_evidence"
TASK_REBUY_ATTEMPT = "rebuy_attempt"
TASK_REBUY_BATCH = "rebuy_batch"
TASK_C5_DELIVERY_CONFIRM = "c5_delivery_confirm"
TASK_C5_ORDER_RECONCILE = "c5_order_reconcile"
TASK_PROFIT_CYCLE = "profit_cycle"
TASK_PROFIT_SELECTION_WATCH = "profit_selection_watch"
TASK_PROFIT_MANUAL_EXECUTION = "profit_manual_execution"
TASK_C5_RESEARCH_SCAN = "c5_research_scan"
TASK_GUADAO_AUDIT = "guadao_audit"

READ_ONLY_AUXILIARY_TASKS = frozenset(
    {
        TASK_C5_RESEARCH_SCAN,
        TASK_GUADAO_AUDIT,
    }
)

C5_CIRCUIT_BLOCKED_TASKS = frozenset(
    {
        TASK_GUADAO_SCAN,
        TASK_STALE_LISTING_RECHECK,
        TASK_REBUY_ATTEMPT,
        TASK_REBUY_BATCH,
        TASK_C5_DELIVERY_CONFIRM,
        TASK_C5_ORDER_RECONCILE,
        TASK_PROFIT_CYCLE,
        TASK_PROFIT_SELECTION_WATCH,
        TASK_PROFIT_MANUAL_EXECUTION,
        TASK_C5_RESEARCH_SCAN,
        TASK_GUADAO_AUDIT,
    }
)

TASK_PUBLIC_LABELS = {
    TASK_STALE_LISTING_RECHECK: "挂刀老挂单条件检查",
    TASK_GUADAO_SCAN: "挂刀候选完整扫描",
    TASK_STEAM_ACCOUNT_SYNC: "Steam 账号状态同步",
    TASK_STEAM_LISTING_CONFIRM: "Steam 挂单确认",
    TASK_STEAM_SALE_EVIDENCE: "挂单消失后确认是否卖出",
    TASK_REBUY_ATTEMPT: "C5 补仓价格不合适时再查",
    TASK_REBUY_BATCH: "C5 同品类批量补仓",
    TASK_C5_DELIVERY_CONFIRM: "C5 已购买待收货确认",
    TASK_C5_ORDER_RECONCILE: "C5 补仓证据复核",
    TASK_PROFIT_CYCLE: "Profit Trade 执行轮次",
    TASK_PROFIT_SELECTION_WATCH: "Profit Trade 选品观察",
    TASK_PROFIT_MANUAL_EXECUTION: "Profit Trade 一键执行",
    TASK_C5_RESEARCH_SCAN: "C5 条件研究扫描",
    TASK_GUADAO_AUDIT: "挂刀执行器只读对账",
}

STALE_LISTING_MAINTENANCE_AUTHORIZATION_SECONDS = 15 * 60
COOKIE_RETRY_DELAYS_SECONDS = (30, 60, 120, 300, 900)
PROFIT_SELECTION_COOKIE_UNAVAILABLE_DELAY_SECONDS = 30 * 60
WORKER_HEARTBEAT_STALE_SECONDS = 45
C5_SUBMISSION_UNCONFIRMED_STATUS = "c5_submission_unconfirmed"
C5_ORDER_RECONCILE_DELAYS_SECONDS = (30.0, 90.0, 180.0, 300.0, 600.0)
C5_ORDER_RECONCILE_MAX_ATTEMPTS = len(C5_ORDER_RECONCILE_DELAYS_SECONDS)
C5_ORDER_RECONCILE_DEGRADED_DELAY_SECONDS = 30.0 * 60.0
C5_DELIVERY_STARTUP_GRACE_SECONDS = 60.0


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


def _has_confirmed_c5_order_evidence(note: dict[str, Any]) -> bool:
    """Return whether delivery/detail checks have queryable C5 evidence.

    ``payStatus`` remains useful diagnostic data, but it is not proof that an
    already identified order disappeared.  Delivery/detail checks resolve the
    order's real current state.  A buyer/status match can safely recognize an
    order from its asset lookup ID before buyer/detail exposes the trade ID;
    only the engine may persist that explicit recognition marker.
    """

    asset_order_id = str(note.get("c5OrderId") or "").strip()
    trade_order_id = str(note.get("c5TradeOrderId") or "").strip()
    return bool(
        (asset_order_id and trade_order_id)
        or (note.get("c5OrderRecognized") is True and asset_order_id)
    )


def _public_c5_trade_order_id(note: dict[str, Any]) -> str | None:
    """Prefer the immutable quick-buy trade id over a legacy overwritten value."""
    payload = note.get("c5OrderPayload")
    if isinstance(payload, dict):
        asset_order_id = str(payload.get("orderAssetId") or "").strip()
        trade_order_id = str(payload.get("orderId") or "").strip()
        if asset_order_id and trade_order_id:
            return trade_order_id
    value = str(note.get("c5TradeOrderId") or "").strip()
    return value or None


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
        self._delivery_startup_ready_at = _now_utc() + timedelta(
            seconds=C5_DELIVERY_STARTUP_GRACE_SECONDS
        )

    # ------------------------------------------------------------------
    # Lifecycle and persistent runtime switches
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._delivery_startup_ready_at = _now_utc() + timedelta(
            seconds=C5_DELIVERY_STARTUP_GRACE_SECONDS
        )
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
            enabled = bool(
                self._steam_scheduler_ready
                and state is not None
                and bool(state["enabled"])
                and not bool(state["migration_hold"])
            )
            if not enabled:
                return False
            if is_c5_ip_circuit_open(db):
                return False
            if executor_key == RUNTIME_PROFIT_TRADE:
                config = load_strategy_config(self.settings)
                return bool(
                    config.profit_trade_enabled
                    and config.profit_trade_allow_real_execution
                )
            return True
        except Exception:
            return False
        finally:
            db.close()

    def _manual_task_lease_owned(self, task_key: str) -> bool:
        """Fail closed once this worker no longer owns the manual batch."""

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_scheduled_task(task_key)
            if (
                row is None
                or str(row["status"] or "") != "running"
                or str(row["lease_owner"] or "") != self.worker_id
            ):
                return False
            expires_at = _parse_iso(str(row["lease_expires_at"] or ""))
            if expires_at is None:
                return False
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return expires_at.astimezone(timezone.utc) > _now_utc()
        except Exception:
            return False
        finally:
            db.close()

    def _renew_manual_task_lease_loop(
        self,
        task_key: str,
        stop_event: threading.Event,
    ) -> None:
        while not stop_event.wait(45.0):
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                renewed = db.renew_scheduled_task_lease(
                    task_key,
                    self.worker_id,
                    lease_seconds=180,
                )
            except Exception:
                renewed = False
            finally:
                db.close()
            if not renewed:
                return

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
            profit_runtime = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
            if profit_runtime is not None and not bool(profit_runtime["migration_hold"]):
                set_profit_trade_enabled(
                    self.settings,
                    bool(profit_runtime["enabled"]),
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
        config_lock_held = False
        if key == RUNTIME_PROFIT_TRADE:
            # Keep the persistent runtime switch and the legacy strategy flag
            # atomic relative to a Profit Trade task entering dispatch.
            self._config_lock.acquire()
            config_lock_held = True
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_executor_runtime_state(key)
            if row is None:
                raise RuntimeError(f"runtime state missing: {key}")
            if bool(row["migration_hold"]):
                raise RuntimeError("migration_hold 尚未确认，不能开启执行器")
            # Profit Trade historically used profitTrade.enabled as its
            # business switch.  The persistent runtime must not create a
            # second, contradictory switch: keep both projections identical.
            if key == RUNTIME_PROFIT_TRADE:
                set_profit_trade_enabled(self.settings, bool(enabled))
            payload = _runtime_payload(row)
            if enabled:
                now = utc_now_iso()
                # A normal executor start reuses persisted cookies. Only an
                # account-scoped authentication failure should invalidate and
                # refresh that account; the explicit refresh-all action remains
                # the sole path that resets every account.
                self._ensure_cookie_rows(db)
                gate = self._cookie_gate_snapshot(db)
                valid_count = int(gate.get("validCount") or 0)
                total_count = int(gate.get("totalCount") or 0)
                payload["requestedAt"] = now
                payload["cookieGate"] = gate
                if total_count > 0 and valid_count == total_count:
                    status = "running"
                    reason = None
                elif valid_count > 0:
                    status = "running"
                    reason = f"Steam Cookie 部分可用 {valid_count}/{total_count}；失败账号单独刷新"
                else:
                    status = "preparing"
                    reason = f"等待可用 Steam Cookie 0/{total_count}"
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
            if enabled and key == RUNTIME_PROFIT_TRADE:
                # Enabling the persistent 10-minute loop must preserve the old
                # user-facing contract: run one cycle as soon as the Cookie
                # gate is ready, then continue on the normal interval.
                db.reschedule_scheduled_task(
                    TASK_PROFIT_CYCLE,
                    next_attempt_at=now,
                )
        finally:
            db.close()
            if config_lock_held:
                self._config_lock.release()
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

    def _update_guadao_scan_progress(self, changes: dict[str, Any]) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            state = db.get_executor_runtime_state(RUNTIME_GUADAO)
            if state is None:
                return
            payload = _runtime_payload(state)
            current = payload.get("activeScan")
            progress = dict(current) if isinstance(current, dict) else {}
            progress.setdefault("status", "running")
            progress.setdefault("startedAt", utc_now_iso())
            progress.update(changes)
            progress["updatedAt"] = utc_now_iso()
            payload["activeScan"] = progress
            db.upsert_executor_runtime_state(
                RUNTIME_GUADAO,
                enabled=bool(state["enabled"]),
                runtime_status=str(state["runtime_status"]),
                migration_hold=bool(state["migration_hold"]),
                gate_reason=state["gate_reason"],
                heartbeat_at=utc_now_iso(),
                payload=payload,
            )
        finally:
            db.close()

    @staticmethod
    def _active_stale_maintenance_authorization(
        payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        authorization = payload.get("staleMaintenanceAuthorization")
        if not isinstance(authorization, dict):
            return None
        if str(authorization.get("mode") or "") != "single_run":
            return None
        if not str(authorization.get("requestId") or "").strip():
            return None
        expires_at = _parse_iso(str(authorization.get("expiresAt") or ""))
        if expires_at is None:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at.astimezone(timezone.utc) <= (now or _now_utc()):
            return None
        return dict(authorization)

    def _stale_maintenance_authorization_active(self, task_key: str) -> bool:
        """Re-read the one-shot authorization immediately before cancellation."""

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_scheduled_task(str(task_key))
            if row is None or str(row["task_type"] or "") != TASK_STALE_LISTING_RECHECK:
                return False
            if str(row["status"] or "") != "running":
                return False
            if str(row["lease_owner"] or "") != self.worker_id:
                return False
            return (
                self._active_stale_maintenance_authorization(_task_payload(row))
                is not None
            )
        except Exception:
            return False
        finally:
            db.close()

    def _stale_maintenance_actions_enabled(self, task_key: str) -> bool:
        """Allow only the explicitly authorized stale-listing action lane."""

        if not self._steam_scheduler_ready:
            return False
        if not self._stale_maintenance_authorization_active(task_key):
            return False
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            state = db.get_executor_runtime_state(RUNTIME_GUADAO)
            if state is None or bool(state["migration_hold"]):
                return False
            return not is_c5_ip_circuit_open(db)
        except Exception:
            return False
        finally:
            db.close()

    @staticmethod
    def _clear_stale_maintenance_authorization(db: Database, task_key: str) -> None:
        row = db.get_scheduled_task(str(task_key))
        if row is None or str(row["task_type"] or "") != TASK_STALE_LISTING_RECHECK:
            return
        payload = _task_payload(row)
        if payload.pop("staleMaintenanceAuthorization", None) is None:
            return
        db.conn.execute(
            "UPDATE scheduled_tasks SET payload_json = ?, updated_at = ? "
            "WHERE task_key = ? AND status != 'running'",
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
                str(task_key),
            ),
        )
        db.conn.commit()

    def stale_listing_recheck_now(self, *, confirmed: bool) -> dict[str, Any]:
        """Queue one stale-listing maintenance pass without enabling guadao.

        This is deliberately a narrow, one-shot authorization for production
        validation.  The normal hourly task still obeys the global guadao
        switch; this path never changes that switch or queues any other task.
        """

        if not bool(confirmed):
            raise RuntimeError(
                "需要明确确认 stale_listing_recheck_only 才能授权一次老挂单维护"
            )
        now = utc_now_iso()
        expires_at = _iso_after(STALE_LISTING_MAINTENANCE_AUTHORIZATION_SECONDS)
        request_id = f"stale-maint-{uuid.uuid4().hex[:12]}"
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            state = db.get_executor_runtime_state(RUNTIME_GUADAO)
            if state is None:
                raise RuntimeError("guadao runtime state missing")
            if bool(state["migration_hold"]):
                raise RuntimeError("migration_hold 尚未确认，不能执行老挂单维护")
            if not self._steam_scheduler_ready:
                raise RuntimeError("Steam 请求调度器不可用，不能执行老挂单维护")
            if is_c5_ip_circuit_open(db):
                raise RuntimeError("C5 IP 熔断开启，不能执行老挂单维护")
            gate = self._cookie_gate_snapshot(db)
            # Stale maintenance is account-grouped and fail-safe when an
            # account cannot provide active-listing evidence. Do not let one
            # unrelated unhealthy account block every healthy account.
            if int(gate.get("validCount") or 0) <= 0:
                raise RuntimeError(
                    "没有可用 Steam Cookie，不能执行老挂单维护 "
                    f"({gate.get('validCount', 0)}/{gate.get('totalCount', 0)})"
                )
            task = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            if task is not None and str(task["status"] or "") == "running":
                return {
                    "ok": True,
                    "taskKey": TASK_STALE_LISTING_RECHECK,
                    "queued": False,
                    "alreadyRunning": True,
                    "maintenanceOnly": True,
                    "fullExecutorEnabled": bool(state["enabled"]),
                }
            existing_payload = _task_payload(task) if task is not None else {}
            existing_payload.pop("staleMaintenanceAuthorization", None)
            existing_payload["staleMaintenanceAuthorization"] = {
                "mode": "single_run",
                "requestId": request_id,
                "authorizedAt": now,
                "expiresAt": expires_at,
            }
            db.upsert_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                source=RUNTIME_GUADAO,
                task_type=TASK_STALE_LISTING_RECHECK,
                next_attempt_at=now,
                account_id=None,
                operation_id=None,
                payload=existing_payload,
                status="pending",
                priority=0,
            )
        finally:
            db.close()
        self._emit_guadao_runtime_event(
            operation=TASK_STALE_LISTING_RECHECK,
            message="已授权一次性老挂单维护；不启用普通挂刀执行器",
            level="WARNING",
            maintenanceOnly=True,
            requestId=request_id,
            expiresAt=expires_at,
        )
        self.wake()
        return {
            "ok": True,
            "taskKey": TASK_STALE_LISTING_RECHECK,
            "queued": True,
            "alreadyRunning": False,
            "maintenanceOnly": True,
            "fullExecutorEnabled": bool(state["enabled"]),
            "requestId": request_id,
            "expiresAt": expires_at,
        }

    def profit_cycle_now(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            state = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
            if state is None or not bool(state["enabled"]):
                raise RuntimeError("Profit Trade 执行器未开启")
            if bool(state["migration_hold"]):
                raise RuntimeError("migration_hold 尚未确认")
            task = db.get_scheduled_task(TASK_PROFIT_CYCLE)
            if task is not None and str(task["status"] or "") == "running":
                return {
                    "ok": True,
                    "taskKey": TASK_PROFIT_CYCLE,
                    "queued": False,
                    "alreadyRunning": True,
                }
            changed = db.reschedule_scheduled_task(
                TASK_PROFIT_CYCLE,
                next_attempt_at=utc_now_iso(),
            )
        finally:
            db.close()
        self.wake()
        return {
            "ok": bool(changed),
            "taskKey": TASK_PROFIT_CYCLE,
            "queued": bool(changed),
            "alreadyRunning": False,
        }

    def profit_selection_watch_now(self) -> dict[str, Any]:
        """Queue the isolated research watch without enabling real execution."""

        now = utc_now_iso()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            active_count = db.count_active_profit_trade_selection_watch()
            if active_count <= 0:
                raise RuntimeError("选品观察池为空，请先添加需要研究的饰品")
            task = db.get_scheduled_task(TASK_PROFIT_SELECTION_WATCH)
            if task is not None and str(task["status"] or "") == "running":
                return {
                    "ok": True,
                    "taskKey": TASK_PROFIT_SELECTION_WATCH,
                    "queued": False,
                    "alreadyRunning": True,
                    "researchOnly": True,
                    "canExecute": False,
                }
            if task is None:
                self._ensure_task(
                    db,
                    TASK_PROFIT_SELECTION_WATCH,
                    source=RUNTIME_PROFIT_TRADE,
                    task_type=TASK_PROFIT_SELECTION_WATCH,
                    next_attempt_at=now,
                    priority=3,
                )
                changed = True
            else:
                changed = db.reschedule_scheduled_task(
                    TASK_PROFIT_SELECTION_WATCH,
                    next_attempt_at=now,
                )
        finally:
            db.close()
        self.wake()
        return {
            "ok": bool(changed),
            "taskKey": TASK_PROFIT_SELECTION_WATCH,
            "queued": bool(changed),
            "alreadyRunning": False,
            "researchOnly": True,
            "canExecute": False,
        }

    def _queue_c5_research_task(self, request_id: str) -> None:
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id:
            raise ValueError("requestId is required")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.upsert_scheduled_task(
                f"c5-research:{normalized_request_id}",
                source=RUNTIME_PROFIT_TRADE,
                task_type=TASK_C5_RESEARCH_SCAN,
                next_attempt_at=utc_now_iso(),
                payload={"requestId": normalized_request_id},
                status="pending",
                priority=3,
            )
        finally:
            db.close()
        self.wake()

    def queue_c5_research_scan(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Persist and queue one read-only full-catalog C5 research scan."""

        result = create_c5_research_scan(self.settings, dict(filters or {}))
        request_id = str(result.get("requestId") or "").strip()
        if not request_id:
            raise RuntimeError("C5 research scan did not return a pollable requestId")
        if str(result.get("status") or "") not in {
            "paused",
            "cancelled",
            "completed",
            "completed_with_errors",
            "failed",
        }:
            try:
                self._queue_c5_research_task(request_id)
            except Exception:
                # Do not leave an unobservable orphan scan when the scheduler
                # write fails after the isolated research row was created.
                set_c5_research_scan_action(self.settings, request_id, "cancel")
                raise
        return result

    def control_c5_research_scan(
        self,
        request_id: str,
        action: str,
    ) -> dict[str, Any]:
        """Pause, resume, or cancel only the isolated research task."""

        result = set_c5_research_scan_action(
            self.settings,
            str(request_id or "").strip(),
            str(action or "").strip().lower(),
        )
        if str(action or "").strip().lower() == "resume":
            self._queue_c5_research_task(str(result.get("requestId") or request_id))
        else:
            self._stop_auxiliary_scheduled_task(
                f"c5-research:{str(result.get('requestId') or request_id).strip()}",
                domain_status=str(result.get("status") or action),
            )
            self.wake()
        return result

    def c5_research_scan_status(self, request_id: str) -> dict[str, Any]:
        result = get_c5_research_scan(self.settings, str(request_id or "").strip())
        result["runtimeTask"] = self._auxiliary_scheduled_task_status(
            f"c5-research:{str(request_id or '').strip()}"
        )
        return result

    def _queue_guadao_audit_task(self, request_id: str) -> None:
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id:
            raise ValueError("requestId is required")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.upsert_scheduled_task(
                f"guadao-audit:{normalized_request_id}",
                source=RUNTIME_GUADAO,
                task_type=TASK_GUADAO_AUDIT,
                next_attempt_at=utc_now_iso(),
                payload={"requestId": normalized_request_id},
                status="pending",
                priority=3,
            )
        finally:
            db.close()
        self.wake()

    @staticmethod
    def _normalized_audit_account_ids(value: Any) -> list[str]:
        if value in (None, "", "all"):
            return []
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("accountIds must be an array or 'all'")
        return list(
            dict.fromkeys(
                str(item or "").strip()
                for item in value
                if str(item or "").strip()
            )
        )

    def queue_guadao_audit_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload or {})
        end_at = str(body.get("endAt") or body.get("dateTo") or "").strip()
        if not end_at:
            raise ValueError("endAt is required")
        account_ids = self._normalized_audit_account_ids(
            body.get("accountIds", body.get("steamAccountIds"))
        )
        result = create_guadao_audit_record(
            self.settings,
            start_at=str(body.get("startAt") or body.get("dateFrom") or "").strip()
            or "2026-07-19T15:20:00+08:00",
            end_at=end_at,
            initial_balance=body.get("initialBalance", body.get("openingWallet", "2502.92")),
            initial_real_value=body.get(
                "initialRealValue", body.get("openingRealValue", "1755.474")
            ),
            account_ids=account_ids,
            expected_account_count=int(
                body.get("expectedAccountCount") or len(account_ids) or 5
            ),
            reported_comprehensive_ratio=body.get("reportedComprehensiveRatio"),
            balance_tolerance_cents=int(body.get("balanceToleranceCents") or 10),
        )
        request_id = str(result.get("requestId") or "").strip()
        if not request_id:
            raise RuntimeError("guadao audit did not return a pollable requestId")
        try:
            self._queue_guadao_audit_task(request_id)
        except Exception:
            cancel_guadao_audit_record(self.settings, request_id)
            raise
        return {
            **result,
            "httpStatus": 202,
            "accepted": True,
            "queued": True,
            "readOnly": True,
            "canExecute": False,
        }

    def retry_guadao_audit_run(self, request_id: str) -> dict[str, Any]:
        result = retry_guadao_audit_record(
            self.settings,
            str(request_id or "").strip(),
        )
        new_request_id = str(result.get("requestId") or "").strip()
        if not new_request_id:
            raise RuntimeError("guadao audit retry did not return a requestId")
        try:
            self._queue_guadao_audit_task(new_request_id)
        except Exception:
            cancel_guadao_audit_record(self.settings, new_request_id)
            raise
        return {
            **result,
            "httpStatus": 202,
            "accepted": True,
            "queued": True,
            "readOnly": True,
            "canExecute": False,
        }

    def cancel_guadao_audit_run(self, request_id: str) -> dict[str, Any]:
        normalized_request_id = str(request_id or "").strip()
        result = cancel_guadao_audit_record(self.settings, normalized_request_id)
        self._stop_auxiliary_scheduled_task(
            f"guadao-audit:{normalized_request_id}",
            domain_status=str(result.get("status") or "cancelled"),
        )
        self.wake()
        return {**result, "readOnly": True, "canExecute": False}

    def guadao_audit_run_status(self, request_id: str) -> dict[str, Any]:
        normalized_request_id = str(request_id or "").strip()
        result = get_guadao_audit_record(self.settings, normalized_request_id)
        if result is None:
            raise KeyError(f"guadao audit run not found: {normalized_request_id}")
        result["runtimeTask"] = self._auxiliary_scheduled_task_status(
            f"guadao-audit:{normalized_request_id}"
        )
        result["readOnly"] = True
        result["canExecute"] = False
        return result

    def _stop_auxiliary_scheduled_task(
        self,
        task_key: str,
        *,
        domain_status: str,
    ) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_scheduled_task(task_key)
            if row is None:
                return
            # upsert_scheduled_task deliberately preserves an active lease.
            # A running bounded chunk sees the service-level control flag;
            # a not-yet-running task becomes unclaimable immediately.
            db.upsert_scheduled_task(
                task_key,
                source=str(row["source"]),
                task_type=str(row["task_type"]),
                next_attempt_at=str(row["next_attempt_at"]),
                account_id=row["account_id"],
                operation_id=row["operation_id"],
                payload=_task_payload(row),
                status="cancelled",
                priority=int(row["priority"] or 3),
                last_error=f"domain_status:{str(domain_status or '').strip()}",
            )
        finally:
            db.close()

    def _auxiliary_scheduled_task_status(self, task_key: str) -> dict[str, Any] | None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_scheduled_task(task_key)
            if row is None:
                return None
            return {
                "taskKey": str(row["task_key"]),
                "status": str(row["status"]),
                "priority": int(row["priority"]),
                "attemptCount": int(row["attempt_count"] or 0),
                "nextAttemptAt": row["next_attempt_at"],
                "lastError": str(row["last_error"] or "") or None,
                "leaseExpiresAt": row["lease_expires_at"],
            }
        finally:
            db.close()

    def queue_profit_trade_manual_execution(
        self,
        *,
        market_hash_name: str,
        quantity: int,
        confirmed: bool,
        expected_roi: float | None = None,
        scan_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        normalized_name = str(market_hash_name or "").strip()
        requested_quantity = int(quantity)
        if not confirmed:
            raise ValueError("explicit confirmation is required for one-click execution")
        if not normalized_name:
            raise ValueError("marketHashName is required")
        confirmed_expected_roi = safe_float(expected_roi)
        confirmed_scan_id = str(scan_id or "").strip()
        confirmed_observed_at = str(observed_at or "").strip()
        if (
            confirmed_expected_roi is None
            or not confirmed_scan_id
            or not confirmed_observed_at
        ):
            raise ValueError(
                "the confirmed observation snapshot requires expectedRoi, scanId and observedAt"
            )
        if (
            requested_quantity <= 0
            or requested_quantity > PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY
        ):
            raise ValueError(
                "quantity must be between 1 and "
                f"{PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY}"
            )

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            runtime = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
            if runtime is None or not bool(runtime["enabled"]):
                raise RuntimeError("Profit Trade 执行器未开启，不能创建新的一键执行任务")
            if bool(runtime["migration_hold"]):
                raise RuntimeError("Profit Trade 仍处于 migration_hold，不能创建新的一键执行任务")
            config = load_strategy_config(self.settings)
            if not config.profit_trade_enabled:
                raise RuntimeError("Profit Trade 业务开关未开启")
            if not config.profit_trade_allow_real_execution:
                raise RuntimeError("Profit Trade 真实执行尚未开放")

            row = db.get_profit_trade_roi_watch(normalized_name)
            if row is None or not bool(row["active"]):
                raise RuntimeError("该品类已不在当前库存做 T 观察池")
            expected_roi = safe_float(row["expected_roi"])
            if expected_roi is None or expected_roi <= 0:
                raise RuntimeError("该品类当前没有可人工批准的正 ROI")
            current_scan_id = str(row["scan_id"] or "").strip()
            current_observed_at = str(row["last_observed_at"] or "").strip()
            if (
                not current_scan_id
                or not current_observed_at
                or current_scan_id != confirmed_scan_id
                or current_observed_at != confirmed_observed_at
                or round(float(expected_roi), 4)
                != round(float(confirmed_expected_roi), 4)
            ):
                raise RuntimeError(
                    "观察快照已经变化，请刷新卡片并重新确认最新 ROI 后再执行"
                )
            execution_status = str(row["execution_status"] or "").strip()
            if execution_status not in {
                "executable",
                "below_min_roi",
                "listings_cooldown",
                "listings_probe_ready",
            }:
                raise RuntimeError(
                    "该品类当前被非 ROI 风控阻断，不能一键执行："
                    f"{row['execution_reason'] or execution_status or 'unknown'}"
                )
            protection_reason = _profit_trade_protection_reason(
                config,
                asset_id=None,
                market_hash_name=normalized_name,
                steam_id=None,
            )
            if protection_reason is None:
                protection_reason = _profit_trade_type_block_reason(config, normalized_name)
            if protection_reason is not None:
                raise RuntimeError(f"该品类当前受保护：{protection_reason}")

            raw = _json_dict(row["raw_json"])
            saved_executable = safe_int(raw.get("manualExecutableQuantity"))
            available_assets = [
                asset
                for asset in db.list_assets(
                    market_hash_name=normalized_name,
                    tradable=True,
                    status="available",
                    exclude_reserved=True,
                )
                if _profit_trade_protection_reason(
                    config,
                    asset_id=str(asset["asset_id"] or "").strip(),
                    market_hash_name=normalized_name,
                    steam_id=str(asset["steam_id"] or "").strip(),
                )
                is None
            ]
            max_quantity = min(
                max(0, int(row["tradable_count"] or 0)),
                len(available_assets),
                (
                    max(0, int(saved_executable))
                    if saved_executable is not None
                    else len(available_assets)
                ),
                PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY,
            )
            if max_quantity <= 0:
                raise RuntimeError("该品类当前没有未锁定、可执行的 C5 资产")
            if requested_quantity > max_quantity:
                raise RuntimeError(
                    f"当前最多只能一键执行 {max_quantity} 件，不能执行 {requested_quantity} 件"
                )

            request_id = f"PTMAN-{uuid.uuid4().hex}"
            task_key = f"profit-manual:{request_id}"
            requested_at = utc_now_iso()
            payload = {
                "requestId": request_id,
                "marketHashName": normalized_name,
                "name": str(row["name_cn"] or normalized_name),
                "quantity": requested_quantity,
                "approvedExpectedRoi": round(float(expected_roi), 4),
                "approvedExpectedProfit": safe_float(row["expected_profit"]),
                "approvedSteamBuyPrice": safe_float(row["steam_buy_price"]),
                "approvedC5ListingPrice": safe_float(row["c5_listing_price"]),
                "approvedObservedAt": current_observed_at,
                "approvedScanId": current_scan_id,
                "requestedAt": requested_at,
                "confirmed": True,
            }
            try:
                db.conn.execute("BEGIN IMMEDIATE")
                active_task = db.conn.execute(
                    """
                    SELECT *
                    FROM scheduled_tasks
                    WHERE source = ?
                      AND task_type = ?
                      AND status IN ('pending', 'retry', 'running')
                      AND json_extract(payload_json, '$.marketHashName') = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        RUNTIME_PROFIT_TRADE,
                        TASK_PROFIT_MANUAL_EXECUTION,
                        normalized_name,
                    ),
                ).fetchone()
                if active_task is not None:
                    raise RuntimeError(
                        "该品类已经有一批一键执行任务正在排队或执行，请等待当前批次结束"
                    )
                db.ensure_scheduled_task(
                    task_key,
                    source=RUNTIME_PROFIT_TRADE,
                    task_type=TASK_PROFIT_MANUAL_EXECUTION,
                    next_attempt_at=requested_at,
                    payload=payload,
                    priority=1,
                )
                if db.conn.in_transaction:
                    db.conn.commit()
            except Exception:
                if db.conn.in_transaction:
                    db.conn.rollback()
                raise
        finally:
            db.close()
        self.wake()
        return {
            "ok": True,
            "queued": True,
            "taskKey": task_key,
            "requestId": request_id,
            "marketHashName": normalized_name,
            "quantity": requested_quantity,
            "maxQuantity": max_quantity,
            "approvedExpectedRoi": round(float(expected_roi), 4),
            "approvedExpectedProfit": safe_float(row["expected_profit"]),
            "requestedAt": requested_at,
        }

    def profit_trade_manual_execution_status(
        self,
        request_id: str,
    ) -> dict[str, Any]:
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id:
            raise ValueError("requestId is required")
        task_key = f"profit-manual:{normalized_request_id}"
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            task = db.get_scheduled_task(task_key)
            if (
                task is None
                or str(task["source"] or "") != RUNTIME_PROFIT_TRADE
                or str(task["task_type"] or "") != TASK_PROFIT_MANUAL_EXECUTION
            ):
                raise LookupError("Profit Trade one-click execution batch was not found")
            payload = _task_payload(task)
            rows = db.list_profit_trades_for_manual_request(
                normalized_request_id,
                limit=PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY,
            )
            runtime = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
            runtime_payload = _runtime_payload(runtime)
        finally:
            db.close()

        status = str(task["status"] or "pending").strip() or "pending"
        terminal = status in {"completed", "failed", "cancelled"}
        recent_run = next(
            (
                entry
                for entry in list(runtime_payload.get("recentTaskRuns") or [])
                if isinstance(entry, dict)
                and str(entry.get("taskKey") or "") == task_key
            ),
            None,
        )
        run_result = (
            dict(recent_run.get("result") or {})
            if isinstance(recent_run, dict)
            and isinstance(recent_run.get("result"), dict)
            else {}
        )

        public_trades: list[dict[str, Any]] = []
        for row in rows:
            note = _json_dict(row["note"])
            trade_error = str(
                row["error"]
                or note.get("cancelReason")
                or note.get("manualExecutionSkipReason")
                or ""
            ).strip()
            public_trades.append(
                {
                    "id": int(row["id"]),
                    "tradeNo": str(row["trade_no"] or ""),
                    "marketHashName": str(row["market_hash_name"] or ""),
                    "status": str(row["status"] or ""),
                    "stepKey": str(row["step_key"] or ""),
                    "error": trade_error or None,
                    "purchaseRequestSent": note.get("purchaseRequestSent"),
                    "listingIdObtained": note.get("listingIdObtained"),
                    "steamBuyMethod": str(note.get("steamBuyMethod") or "") or None,
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                    "completedAt": row["completed_at"],
                }
            )

        bought_statuses = {"steam_bought", "listing_c5", "c5_listed", "completed"}
        listed_statuses = {"c5_listed", "completed"}
        bought_count = len(
            list(run_result.get("boughtTradeIds") or [])
        ) or sum(
            1 for trade in public_trades if trade["status"] in bought_statuses
        )
        listed_count = len(
            list(run_result.get("listedTradeIds") or [])
        ) or sum(
            1 for trade in public_trades if trade["status"] in listed_statuses
        )
        failed_count = sum(
            1
            for trade in public_trades
            if trade["status"] in {"failed", "cancelled", "manual_required"}
        )
        requested_quantity = int(payload.get("quantity") or 0)
        error = str(
            task["last_error"]
            or (recent_run.get("error") if isinstance(recent_run, dict) else "")
            or next(
                (
                    trade["error"]
                    for trade in public_trades
                    if trade.get("error")
                ),
                "",
            )
        ).strip()
        if status == "pending":
            summary = "一键执行已排队，等待后台领取"
        elif status == "retry":
            summary = "一键执行暂时无法开始，后台已安排重试"
        elif status == "running":
            summary = "一键执行正在处理，完成后会自动更新结果"
        elif status == "completed":
            summary = (
                f"一键执行已完成：买入 {bought_count}/{requested_quantity} 件，"
                f"C5 上架 {listed_count} 件"
            )
        elif status == "cancelled":
            summary = f"一键执行已取消：{error or '任务在购买前被安全停止'}"
        else:
            summary = f"一键执行失败：{error or '后台返回失败终态'}"

        return {
            "ok": True,
            "requestId": normalized_request_id,
            "taskKey": task_key,
            "marketHashName": str(payload.get("marketHashName") or ""),
            "name": str(payload.get("name") or payload.get("marketHashName") or ""),
            "requestedQuantity": requested_quantity,
            "status": status,
            "terminal": terminal,
            "summary": summary,
            "error": error or None,
            "attemptCount": int(task["attempt_count"] or 0),
            "queuedAt": task["created_at"],
            "updatedAt": task["updated_at"],
            "completedAt": task["completed_at"],
            "nextAttemptAt": task["next_attempt_at"] if status == "retry" else None,
            "counts": {
                "created": len(public_trades),
                "bought": bought_count,
                "listed": listed_count,
                "failed": failed_count,
            },
            "trades": public_trades,
        }

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

    def _notify_stale_listing_recheck_result(
        self,
        result: dict[str, Any],
    ) -> None:
        """Send one auditable aggregate notification for a maintenance run.

        The stale-listing walk can touch many listings.  Notifications are
        therefore aggregated per run instead of sending one message per
        listing.  A run with no removal, failure, or missing evidence is
        intentionally silent so the hourly maintenance task does not become
        a notification spam source.
        """

        removed = safe_int(result.get("removed")) or 0
        remove_failed = safe_int(result.get("removeFailed")) or 0
        unmatched = safe_int(result.get("unmatched")) or 0
        price_deferred = safe_int(result.get("priceDeferred")) or 0
        if not any((removed, remove_failed, unmatched, price_deferred)):
            return
        run_id = str(result.get("runId") or utc_now_iso()).strip()
        event_key = f"stale-listing-recheck:{run_id}"
        body_lines = [
            str(result.get("summary") or "老挂单检查完成"),
            f"运行 ID: {run_id}",
            "",
        ]
        removed_rows = result.get("removedOperations")
        if isinstance(removed_rows, list) and removed_rows:
            body_lines.append("撤单成功（前 20 笔）:")
            for row in removed_rows[:20]:
                if not isinstance(row, dict):
                    continue
                body_lines.append(
                    "- "
                    f"operation={row.get('operationId') or '-'} | "
                    f"{row.get('marketHashName') or '-'} | "
                    f"listing={row.get('listingId') or '-'} | "
                    f"asset={row.get('assetId') or '-'} | "
                    f"{row.get('reason') or '-'}"
                )
        failed_rows = result.get("removeFailedOperations")
        if isinstance(failed_rows, list) and failed_rows:
            body_lines.append("撤单失败（前 20 笔）:")
            for row in failed_rows[:20]:
                if not isinstance(row, dict):
                    continue
                body_lines.append(
                    "- "
                    f"operation={row.get('operationId') or '-'} | "
                    f"{row.get('marketHashName') or '-'} | "
                    f"listing={row.get('listingId') or '-'} | "
                    f"原因={row.get('reason') or '-'}"
                )
        if unmatched:
            body_lines.append(
                f"活跃挂单未匹配 {unmatched} 笔：本轮不判定卖出、不撤单、不恢复资产。"
            )
        if price_deferred:
            body_lines.append(
                f"盘口/C5 价格证据不足延期 {price_deferred} 笔：本轮不撤单。"
            )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self._send_runtime_notification_once(
                db,
                event_key=event_key,
                title=f"[挂刀老挂单] 检查完成：撤单 {removed} 笔",
                body="\n".join(body_lines),
            )
        finally:
            db.close()

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

    def _guadao_scan_starvation_guard_seconds(
        self,
        db: Database,
        *,
        gate: dict[str, Any],
    ) -> float | None:
        """Return the maximum extra scan lag while preserving the P0 safety lane."""

        state = db.get_executor_runtime_state(RUNTIME_GUADAO)
        if (
            state is None
            or not bool(state["enabled"])
            or bool(state["migration_hold"])
            or gate.get("status") not in {"ready", "degraded"}
            or not self._steam_scheduler_ready
            or is_c5_ip_circuit_open(db)
        ):
            return None
        schedule = load_strategy_config(self.settings).effective_guadao_task_schedule()
        interval_seconds = safe_float(schedule.get("scanIntervalSeconds"))
        if interval_seconds is None or interval_seconds <= 0:
            return None
        # The scan keeps its normal P3 priority for one full interval.  Only
        # after missing an additional complete cycle may it take one atomic
        # slot ahead of P1; the database priority itself remains P3.
        return float(interval_seconds)

    def _guadao_steam_sync_deadline_guard_seconds(
        self,
        db: Database,
        *,
        gate: dict[str, Any],
    ) -> float | None:
        """Return the bounded start-lag budget for account sale synchronisation.

        C5 availability is deliberately not part of this gate: discovering a
        Steam sale and advancing its existing evidence chain remains useful
        while C5 is temporarily unavailable.  Per-account cookie readiness is
        still checked immediately before dispatch.
        """

        state = db.get_executor_runtime_state(RUNTIME_GUADAO)
        if (
            state is None
            or not bool(state["enabled"])
            or bool(state["migration_hold"])
            or gate.get("status") not in {"ready", "degraded"}
            or not self._steam_scheduler_ready
        ):
            return None
        schedule = load_strategy_config(self.settings).effective_guadao_task_schedule()
        maximum_lag_seconds = safe_float(schedule.get("steamSyncMaxStartLagSeconds"))
        if maximum_lag_seconds is None or maximum_lag_seconds <= 0:
            return None
        return float(maximum_lag_seconds)

    def tick(self, *, max_tasks: int = 20) -> dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            guadao_scan_starvation_guard_seconds: float | None = None
            guadao_steam_sync_deadline_guard_seconds: float | None = None
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                self._heartbeat(db)
                self._ensure_cookie_rows(db)
                gate = self._cookie_gate_tick(db)
                c5_circuit = probe_c5_ip_circuit(
                    self.settings,
                    db,
                    worker_id=self.worker_id,
                    api_key=(
                        self.settings.c5_api_key
                        or next(
                            (
                                account.c5_api_key
                                for account in self._accounts()
                                if account.c5_api_key
                            ),
                            None,
                        )
                    ),
                )
                notify_c5_ip_circuit_if_pending(self.settings, db)
                self._seed_tasks(db)
                guadao_scan_starvation_guard_seconds = (
                    self._guadao_scan_starvation_guard_seconds(db, gate=gate)
                )
                guadao_steam_sync_deadline_guard_seconds = (
                    self._guadao_steam_sync_deadline_guard_seconds(db, gate=gate)
                )
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
                        starvation_guard_task_key=(
                            TASK_GUADAO_SCAN
                            if guadao_scan_starvation_guard_seconds is not None
                            else None
                        ),
                        starvation_guard_after_seconds=(
                            guadao_scan_starvation_guard_seconds
                        ),
                        deadline_guard_task_type=(
                            TASK_STEAM_ACCOUNT_SYNC
                            if guadao_steam_sync_deadline_guard_seconds is not None
                            else None
                        ),
                        deadline_guard_after_seconds=(
                            guadao_steam_sync_deadline_guard_seconds
                        ),
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
            finally:
                db.close()
            return {
                "ok": True,
                "processed": processed,
                "cookieGate": gate,
                "c5Circuit": c5_circuit,
            }
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
            health_payload = _json_dict(row["payload_json"]) if row is not None else {}
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
                    "currencyId": safe_int(health_payload.get("currencyId")),
                    "currency": health_payload.get("currency"),
                    "currencyStatus": str(
                        health_payload.get("currencyStatus") or "unknown"
                    ),
                    "currencyCheckedAt": health_payload.get("currencyCheckedAt"),
                    "currencyError": health_payload.get("currencyError"),
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

        reusable = snapshot["totalCount"] > 0 and snapshot["validCount"] == snapshot["totalCount"]
        if reusable:
            self._apply_gate_state(db, ready=True, snapshot=snapshot)
            return snapshot

        now = _now_utc()
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

    def _probe_cookie_currency(self, account: Account) -> dict[str, Any]:
        """Read wallet currency once after relogin without triggering another relogin."""

        checked_at = utc_now_iso()
        try:
            from cs2_assistant.clients.steam_market import SteamMarketClient

            client = SteamMarketClient(
                cookies=account.cookies,
                steam_id64=account.steam_id64,
                identity_secret=account.identity_secret,
                device_id=account.device_id,
                account_id=account.id,
                base_url=self.settings.steam_market_base_url,
                request_source="guadao",
                allow_account_relogin=False,
            )
            wallet = client.wallet_balance()
            currency_id = safe_int(wallet.get("currency_id"))
            currency = str(wallet.get("currency") or "").strip() or None
            return {
                "currencyId": currency_id,
                "currency": currency,
                "currencyStatus": (
                    "cny" if currency_id == 23
                    else "non_cny" if currency_id is not None
                    else "unknown"
                ),
                "currencyCheckedAt": checked_at,
                "currencyError": None,
            }
        except Exception as exc:
            return {
                "currencyId": None,
                "currency": None,
                "currencyStatus": "unknown",
                "currencyCheckedAt": checked_at,
                "currencyError": str(exc)[:240],
            }

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
            currency_probe = self._probe_cookie_currency(updated)
            db.upsert_steam_cookie_health(
                account.id,
                status="valid",
                account_name=updated.name,
                steam_id=updated.steam_id64,
                batch_id=batch_id,
                failure_count=0,
                last_validated_at=utc_now_iso(),
                payload={
                    "message": "Steam Cookie 已刷新并通过市场门禁",
                    **currency_probe,
                },
            )
            currency_id = currency_probe.get("currencyId")
            self._emit_guadao_runtime_event(
                operation="cookie_refresh_completed",
                message=(
                    f"Steam Cookie 刷新完成：{updated.name or account.name}，"
                    f"币种 {currency_probe.get('currency') or '未知'} "
                    f"(currencyId={currency_id if currency_id is not None else 'unknown'})"
                ),
                level="INFO" if currency_id == 23 else "WARNING",
                accountId=updated.id,
                accountName=updated.name or account.name,
                batchId=batch_id,
                **currency_probe,
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
        self._emit_guadao_runtime_event(
            operation="cookie_refresh_failed",
            message=f"Steam Cookie 刷新失败：{account.name}，{normalized}",
            level="ERROR",
            accountId=account.id,
            accountName=account.name,
            batchId=batch_id,
            status=health_status,
            error=normalized,
            retryDelaySeconds=delay,
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

    @staticmethod
    def _terminate_obsolete_delivery_task(db: Database, operation_id: int) -> None:
        task_key = f"delivery:{int(operation_id)}"
        current = db.get_scheduled_task(task_key)
        if current is None:
            return
        if (
            str(current["status"] or "") == "cancelled"
            and str(current["last_error"] or "")
            == "superseded_by_c5_submission_reconcile"
        ):
            # Preserve the tombstone for a task that used to own a running
            # lease. This proves it was deliberately superseded and, more
            # importantly, keeps it permanently outside the claimable states.
            return
        if db.delete_scheduled_task(task_key):
            return
        # A stale/running lease may belong to another worker. Mark it
        # terminal so it cannot be reclaimed. The operation has already been
        # moved out of delivery_pending, therefore a worker that was already
        # executing can only observe the new guarded state and exit.
        now = utc_now_iso()
        db.conn.execute(
            """
            UPDATE scheduled_tasks
            SET status = 'cancelled', lease_owner = NULL,
                lease_expires_at = NULL, completed_at = ?, updated_at = ?,
                last_error = 'superseded_by_c5_submission_reconcile'
            WHERE task_key = ? AND status = 'running'
            """,
            (now, now, task_key),
        )
        db.conn.commit()

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

        accounts = list(self._accounts())
        account_ids = {account.id for account in accounts}
        for account in accounts:
            self._project_steam_account_sync_task(db, account.id)
        for sync_task in db.list_scheduled_tasks(
            source=RUNTIME_GUADAO,
            task_type=TASK_STEAM_ACCOUNT_SYNC,
            limit=5000,
        ):
            account_id = str(sync_task["account_id"] or "")
            if account_id not in account_ids and str(sync_task["status"] or "") != "running":
                db.delete_scheduled_task(str(sync_task["task_key"]))

    def _steam_operation_markers(self, db: Database, account_id: str) -> list[Any]:
        return [
            row
            for task_type in (TASK_STEAM_LISTING_CONFIRM, TASK_STEAM_SALE_EVIDENCE)
            for row in db.list_scheduled_tasks(
                source=RUNTIME_GUADAO,
                task_type=task_type,
                status="waiting",
                account_id=account_id,
                limit=5000,
            )
        ]

    def _project_steam_account_sync_task(
        self,
        db: Database,
        account_id: str,
    ) -> str | None:
        """Project one account carrier from its earliest operation timer.

        Listing confirmation and sale-evidence rows own the business clocks.
        The account sync is only their coalesced execution carrier; it must not
        acquire a second, periodic clock of its own.
        """

        sync_key = f"steam-sync:{account_id}"
        current = db.get_scheduled_task(sync_key)
        markers = self._steam_operation_markers(db, account_id)
        if not markers:
            if current is not None and str(current["status"] or "") != "running":
                db.delete_scheduled_task(sync_key)
            return None

        earliest = min(str(row["next_attempt_at"]) for row in markers)
        if current is not None and str(current["status"] or "") == "running":
            return earliest

        current_payload = _task_payload(current) if current is not None else {}
        current_error = str(current["last_error"] or "") if current is not None else ""
        manual_wakeup = bool(current_payload.get("manualSafeReviewIssueId"))
        cookie_backoff = current_error == "account_cookie_not_valid"
        if manual_wakeup and str(current["next_attempt_at"] or "") < earliest:
            next_attempt_at = str(current["next_attempt_at"])
        elif cookie_backoff and str(current["next_attempt_at"] or "") > earliest:
            next_attempt_at = str(current["next_attempt_at"])
        else:
            next_attempt_at = earliest
        db.upsert_scheduled_task(
            sync_key,
            source=RUNTIME_GUADAO,
            task_type=TASK_STEAM_ACCOUNT_SYNC,
            next_attempt_at=next_attempt_at,
            account_id=account_id,
            payload=current_payload,
            status="pending",
            priority=0 if manual_wakeup else 2,
            last_error=current_error or None,
        )
        return earliest

    def _seed_read_only_auxiliary_tasks(self, db: Database, *, now: str) -> None:
        """Recover isolated research/audit jobs after a backend restart."""

        table_names = {
            str(row["name"])
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        def lease_is_active(task_key: str) -> bool:
            task = db.get_scheduled_task(task_key)
            if task is None or str(task["status"] or "") != "running":
                return False
            expires_at = _parse_iso(str(task["lease_expires_at"] or ""))
            if expires_at is None:
                return False
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return expires_at.astimezone(timezone.utc) > _now_utc()

        if "c5_research_scan_jobs" in table_names:
            running_rows = db.conn.execute(
                "SELECT request_id FROM c5_research_scan_jobs WHERE status = 'running'"
            ).fetchall()
            for running_row in running_rows:
                running_id = str(running_row["request_id"] or "").strip()
                if not running_id or lease_is_active(f"c5-research:{running_id}"):
                    continue
                with db.conn:
                    db.conn.execute(
                        """
                        UPDATE c5_research_scan_jobs
                        SET status = 'queued', control_action = NULL,
                            next_attempt_at = NULL, updated_at = ?
                        WHERE request_id = ? AND status = 'running'
                        """,
                        (now, running_id),
                    )
            rows = db.conn.execute(
                """
                SELECT request_id, status, next_attempt_at
                FROM c5_research_scan_jobs
                WHERE status IN ('queued', 'retry')
                ORDER BY created_at ASC
                """
            ).fetchall()
            for row in rows:
                request_id = str(row["request_id"] or "").strip()
                if not request_id:
                    continue
                db.upsert_scheduled_task(
                    f"c5-research:{request_id}",
                    source=RUNTIME_PROFIT_TRADE,
                    task_type=TASK_C5_RESEARCH_SCAN,
                    next_attempt_at=str(row["next_attempt_at"] or now),
                    payload={"requestId": request_id},
                    status="retry" if str(row["status"]) == "retry" else "pending",
                    priority=3,
                )

        if "guadao_audit_runs" in table_names:
            running_rows = db.conn.execute(
                """
                SELECT request_id FROM guadao_audit_runs
                WHERE status = 'running' AND cancel_requested = 0
                """
            ).fetchall()
            for running_row in running_rows:
                running_id = str(running_row["request_id"] or "").strip()
                if not running_id or lease_is_active(f"guadao-audit:{running_id}"):
                    continue
                with db.conn:
                    db.conn.execute(
                        """
                        UPDATE guadao_audit_runs
                        SET status = 'pending', stage = 'pending',
                            error = NULL, updated_at = ?
                        WHERE request_id = ? AND status = 'running'
                          AND cancel_requested = 0
                        """,
                        (now, running_id),
                    )
            rows = db.conn.execute(
                """
                SELECT request_id
                FROM guadao_audit_runs
                WHERE status = 'pending' AND cancel_requested = 0
                ORDER BY created_at ASC
                """
            ).fetchall()
            for row in rows:
                request_id = str(row["request_id"] or "").strip()
                if not request_id:
                    continue
                db.upsert_scheduled_task(
                    f"guadao-audit:{request_id}",
                    source=RUNTIME_GUADAO,
                    task_type=TASK_GUADAO_AUDIT,
                    next_attempt_at=now,
                    payload={"requestId": request_id},
                    status="pending",
                    priority=3,
                )

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
        stale_task = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
        if stale_task is None or str(stale_task["status"] or "") in {
            "completed",
            "failed",
            "cancelled",
        }:
            db.upsert_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                source=RUNTIME_GUADAO,
                task_type=TASK_STALE_LISTING_RECHECK,
                next_attempt_at=now,
                payload={},
                status="pending",
                priority=0,
            )
        else:
            self._ensure_task(
                db,
                TASK_STALE_LISTING_RECHECK,
                source=RUNTIME_GUADAO,
                task_type=TASK_STALE_LISTING_RECHECK,
                next_attempt_at=now,
                priority=0,
            )
            # The task key is global, but old versions (or a corrupted local
            # row) may leave account-scoped metadata behind.  ``ensure`` is
            # intentionally insert-only for an existing row, so reconcile the
            # immutable identity here.  Never rewrite a ``running`` row: the
            # worker that owns it may already be dispatching the old snapshot.
            # Even an expired or malformed lease is left alone until the
            # worker/claim path releases it; this avoids racing a late worker
            # during startup repair.
            stale_status = str(stale_task["status"] or "")
            stale_metadata_needs_repair = (
                str(stale_task["source"] or "") != RUNTIME_GUADAO
                or str(stale_task["task_type"] or "") != TASK_STALE_LISTING_RECHECK
                or stale_task["account_id"] is not None
                or stale_task["operation_id"] is not None
            )
            if stale_status != "running" and stale_metadata_needs_repair:
                db.conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET source = ?, task_type = ?, account_id = NULL,
                        operation_id = NULL, updated_at = ?
                    WHERE task_key = ?
                      AND status IS ?
                      AND lease_owner IS ?
                      AND lease_expires_at IS ?
                    """,
                    (
                        RUNTIME_GUADAO,
                        TASK_STALE_LISTING_RECHECK,
                        now,
                        TASK_STALE_LISTING_RECHECK,
                        stale_task["status"],
                        stale_task["lease_owner"],
                        stale_task["lease_expires_at"],
                    ),
                )
                db.conn.commit()
            # Repair legacy priority without changing cadence, status, or lease.
            if int(stale_task["priority"] or 0) != 0:
                db.conn.execute(
                    "UPDATE scheduled_tasks SET priority = ?, updated_at = ? WHERE task_key = ?",
                    (0, now, TASK_STALE_LISTING_RECHECK),
                )
                db.conn.commit()
            # Old operation-task schedulers used ``waiting`` for nonterminal
            # work, but the generic claimant only accepts pending/retry/running.
            # This global maintenance job must never remain permanently
            # unclaimable after an upgrade; preserve its scheduled time while
            # restoring the claimable status.
            if str(stale_task["status"] or "") == "waiting":
                db.conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL,
                        completed_at = NULL, updated_at = ?
                    WHERE task_key = ? AND status = 'waiting'
                    """,
                    (now, TASK_STALE_LISTING_RECHECK),
                )
                db.conn.commit()
        self._ensure_task(
            db,
            TASK_PROFIT_CYCLE,
            source=RUNTIME_PROFIT_TRADE,
            task_type=TASK_PROFIT_CYCLE,
            next_attempt_at=now,
            priority=1,
        )
        if db.count_active_profit_trade_selection_watch() > 0:
            self._ensure_task(
                db,
                TASK_PROFIT_SELECTION_WATCH,
                source=RUNTIME_PROFIT_TRADE,
                task_type=TASK_PROFIT_SELECTION_WATCH,
                next_attempt_at=now,
                priority=3,
            )
        else:
            # A removed selection must not leave an otherwise idle P3 task
            # wakeable forever.  Running leases are deliberately left alone;
            # they will observe zero active rows and complete safely.
            db.delete_scheduled_task(TASK_PROFIT_SELECTION_WATCH)
        self._seed_read_only_auxiliary_tasks(db, now=now)
        self._seed_steam_operation_tasks(db, config)
        self._seed_rebuy_batch_tasks(db, config, now=now)
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
            has_both_order_ids = bool(
                str(note.get("c5OrderId") or "").strip()
                and str(note.get("c5TradeOrderId") or "").strip()
            )
            if not _has_confirmed_c5_order_evidence(note):
                if str(op["status"] or "") == "delivery_pending":
                    migrated_at = utc_now_iso()
                    note.setdefault("c5SubmissionUnconfirmedAt", migrated_at)
                    note.setdefault(
                        "c5SubmissionUnconfirmedReason",
                        (
                            "missing_valid_order_evidence"
                            if has_both_order_ids
                            else "missing_complete_order_ids"
                        ),
                    )
                    note.setdefault("c5SubmissionPreviousStatus", "delivery_pending")
                    db.update_pool_operation(
                        int(op["id"]),
                        status=C5_SUBMISSION_UNCONFIRMED_STATUS,
                        note=json.dumps(note, ensure_ascii=False),
                    )
                    self._emit_guadao_runtime_event(
                        operation=TASK_C5_ORDER_RECONCILE,
                        message="旧补仓流水缺少完整 C5 订单号，已转为提交结果待核对",
                        level="WARNING",
                        operationId=int(op["id"]),
                        marketHashName=str(op["market_hash_name"] or ""),
                        c5OutTradeNo=note.get("c5OutTradeNo"),
                        c5OrderId=note.get("c5OrderId"),
                        c5TradeOrderId=note.get("c5TradeOrderId"),
                        c5PayStatus=(
                            note.get("c5PayStatus")
                            if note.get("c5PayStatus") is not None
                            else (
                                note.get("c5OrderPayload", {}).get("payStatus")
                                if isinstance(note.get("c5OrderPayload"), dict)
                                else None
                            )
                        ),
                        reason=note.get("c5SubmissionUnconfirmedReason"),
                    )
                    # A delivery confirmation cannot query by outTradeNo. A
                    # non-running legacy task is therefore obsolete. A task
                    # already holding a lease is left in place so its worker
                    # can finish safely; its post-run status check will make
                    # it terminal because the operation is no longer
                    # delivery_pending.
                    self._terminate_obsolete_delivery_task(db, int(op["id"]))
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
        for op in db.list_pool_operations_by_type(
            OP_REBUY_C5,
            status=C5_SUBMISSION_UNCONFIRMED_STATUS,
            limit=5000,
        ):
            # Never allow an old delivery task and a submission reconcile task
            # to be claimable for the same operation.
            self._terminate_obsolete_delivery_task(db, int(op["id"]))
            self._ensure_task(
                db,
                f"reconcile:{op['id']}",
                source=RUNTIME_GUADAO,
                task_type=TASK_C5_ORDER_RECONCILE,
                next_attempt_at=now,
                operation_id=int(op["id"]),
                priority=1,
                payload={"createdAt": op["created_at"]},
            )

    @staticmethod
    def _rebuy_batch_task_key(market_hash_name: str) -> str:
        digest = hashlib.sha256(str(market_hash_name).encode("utf-8")).hexdigest()[:16]
        return f"rebuy-batch:{digest}"

    def _seed_rebuy_batch_tasks(self, db: Database, config: StrategyConfig, *, now: str) -> None:
        """Keep per-operation clocks waiting and wake one bounded batch per item.

        A legacy per-operation rebuy task that already owns a lease is never
        touched.  It can finish under the old implementation once; every
        other pending operation is migrated to a non-claimable ``waiting``
        clock and is subsequently owned by its category batch task.
        """

        pending = db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=5000)
        earliest_by_name: dict[str, str] = {}
        active_names: set[str] = set()
        for op in pending:
            name = str(op["market_hash_name"] or "").strip()
            if not name:
                continue
            task_key = f"rebuy:{int(op['id'])}"
            existing = db.get_scheduled_task(task_key)
            if existing is None:
                db.upsert_scheduled_task(
                    task_key,
                    source=RUNTIME_GUADAO,
                    task_type=TASK_REBUY_ATTEMPT,
                    next_attempt_at=now,
                    operation_id=int(op["id"]),
                    priority=1,
                    payload={"createdAt": op["created_at"]},
                    status="waiting",
                )
                scheduled_at = now
            elif str(existing["status"] or "") == "running":
                # The owner may be a pre-upgrade quick-buy call.  Exclude it
                # from the batch until it releases its own final evidence.
                continue
            else:
                scheduled_at = str(existing["next_attempt_at"] or now)
                db.upsert_scheduled_task(
                    task_key,
                    source=RUNTIME_GUADAO,
                    task_type=TASK_REBUY_ATTEMPT,
                    next_attempt_at=scheduled_at,
                    operation_id=int(op["id"]),
                    priority=1,
                    payload={"createdAt": op["created_at"]},
                    status="waiting",
                )
            active_names.add(name)
            current_earliest = earliest_by_name.get(name)
            if current_earliest is None or scheduled_at < current_earliest:
                earliest_by_name[name] = scheduled_at

        for task in db.list_scheduled_tasks(
            source=RUNTIME_GUADAO,
            task_type=TASK_REBUY_BATCH,
            limit=5000,
        ):
            payload = _task_payload(task)
            name = str(payload.get("marketHashName") or "").strip()
            if name not in active_names:
                db.delete_scheduled_task(str(task["task_key"]))

        # A successful batch moves the operation to delivery_pending.  Its
        # former waiting clock must not accumulate forever or reappear after a
        # later migration.  Never remove a running legacy quick-buy task.
        for task in db.list_scheduled_tasks(
            source=RUNTIME_GUADAO,
            task_type=TASK_REBUY_ATTEMPT,
            limit=5000,
        ):
            task_key = str(task["task_key"] or "")
            if not task_key.startswith("rebuy:") or str(task["status"] or "") == "running":
                continue
            operation_id = safe_int(task["operation_id"])
            op = (
                db.conn.execute(
                    "SELECT status FROM pool_operations WHERE id = ?", (operation_id,)
                ).fetchone()
                if operation_id is not None
                else None
            )
            if op is None or str(op["status"] or "") != "pending":
                db.delete_scheduled_task(task_key)

        for name, earliest in earliest_by_name.items():
            db.upsert_scheduled_task(
                self._rebuy_batch_task_key(name),
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_BATCH,
                next_attempt_at=earliest,
                payload={"marketHashName": name},
                priority=1,
                status="pending",
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
            if (
                bool(state["migration_hold"])
                and task_type not in READ_ONLY_AUXILIARY_TASKS
            ):
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(60),
                    worker_id=self.worker_id,
                    error="migration_hold",
                )
                return
            enabled = bool(state["enabled"])
            stale_maintenance_authorized = (
                task_type == TASK_STALE_LISTING_RECHECK
                and self._active_stale_maintenance_authorization(_task_payload(task))
                is not None
            )
            if task_type in C5_CIRCUIT_BLOCKED_TASKS and is_c5_ip_circuit_open(db):
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(60),
                    worker_id=self.worker_id,
                    error="c5_ip_whitelist_circuit_open",
                )
                return
            if (
                task_type == TASK_C5_DELIVERY_CONFIRM
                and _now_utc() < self._delivery_startup_ready_at
            ):
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=self._delivery_startup_ready_at.isoformat(),
                    worker_id=self.worker_id,
                    error="delivery_startup_grace",
                )
                return
            if (
                not self._steam_scheduler_ready
                and task_type in {
                    TASK_GUADAO_SCAN,
                    TASK_STALE_LISTING_RECHECK,
                    TASK_STEAM_ACCOUNT_SYNC,
                    TASK_PROFIT_CYCLE,
                    TASK_PROFIT_MANUAL_EXECUTION,
                    TASK_PROFIT_SELECTION_WATCH,
                    TASK_C5_RESEARCH_SCAN,
                    TASK_GUADAO_AUDIT,
                }
            ):
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(30),
                    worker_id=self.worker_id,
                    error="steam_scheduler_unavailable",
                )
                return
            # A research-only selection scan is deliberately not an executor
            # start: when both executors are stopped, the shared cookie gate
            # remains ``idle`` and must not begin a five-account auto-login
            # batch merely to read a P3 orderbook.  It may reuse all already
            # healthy cookies, otherwise it waits for a user/real executor
            # refresh rather than silently relogging in the background.
            if task_type == TASK_PROFIT_SELECTION_WATCH:
                valid_count = int(gate.get("validCount") or 0)
                total_count = int(gate.get("totalCount") or 0)
                selection_cookie_ready = total_count > 0 and valid_count == total_count
                if not selection_cookie_ready:
                    retry_at = _parse_iso(str(gate.get("nextRetryAt") or ""))
                    if retry_at is not None and retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    minimum_retry = _now_utc() + timedelta(
                        seconds=PROFIT_SELECTION_COOKIE_UNAVAILABLE_DELAY_SECONDS
                    )
                    next_attempt_at = max(
                        minimum_retry,
                        retry_at.astimezone(timezone.utc) if retry_at is not None else minimum_retry,
                    ).isoformat()
                    db.reschedule_scheduled_task(
                        task_key,
                        next_attempt_at=next_attempt_at,
                        worker_id=self.worker_id,
                        error="selection_cookie_unavailable",
                    )
                    return
            gate_ready = gate.get("status") in {"ready", "degraded"}
            if (
                task_type in {
                    TASK_GUADAO_SCAN,
                    TASK_STALE_LISTING_RECHECK,
                    TASK_PROFIT_CYCLE,
                    TASK_PROFIT_MANUAL_EXECUTION,
                }
                and (enabled or stale_maintenance_authorized)
                and not gate_ready
            ):
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(5),
                    worker_id=self.worker_id,
                    error="cookie_gate_preparing",
                )
                return
            if (
                task_type == TASK_STALE_LISTING_RECHECK
                and not enabled
                and not stale_maintenance_authorized
            ):
                schedule = load_strategy_config(self.settings).effective_guadao_task_schedule()
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=_iso_after(
                        float(schedule["staleListedCheckIntervalSeconds"])
                    ),
                    worker_id=self.worker_id,
                    error="executor_disabled",
                )
                self._clear_stale_maintenance_authorization(db, task_key)
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
                    next_attempt_at=_iso_after(PROFIT_TRADE_CYCLE_INTERVAL_SECONDS),
                    worker_id=self.worker_id,
                    error="executor_disabled_no_closure_work",
                )
                return
            if task_type == TASK_PROFIT_MANUAL_EXECUTION and not enabled:
                db.complete_scheduled_task(
                    task_key,
                    self.worker_id,
                    status="cancelled",
                    error="executor_disabled_before_manual_execution",
                )
                return
            if task_type in {TASK_PROFIT_MANUAL_EXECUTION, TASK_GUADAO_AUDIT, TASK_STALE_LISTING_RECHECK}:
                if not db.renew_scheduled_task_lease(
                    task_key,
                    self.worker_id,
                    lease_seconds=180,
                ):
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
                    db.complete_scheduled_task(
                        task_key,
                        worker_id=self.worker_id,
                        status="completed",
                        error="executor_disabled_no_account_work",
                    )
                    return
                dispatch_task = {
                    **dict(task),
                    "_dueOperationTasks": self._due_steam_operation_tasks(db, account_id),
                }
        finally:
            db.close()

        lease_stop: threading.Event | None = None
        lease_thread: threading.Thread | None = None
        if task_type in {TASK_PROFIT_MANUAL_EXECUTION, TASK_GUADAO_AUDIT, TASK_STALE_LISTING_RECHECK}:
            lease_stop = threading.Event()
            lease_thread = threading.Thread(
                target=self._renew_manual_task_lease_loop,
                args=(task_key, lease_stop),
                name=f"task-lease-{task_key[-12:]}",
                daemon=True,
            )
            lease_thread.start()
        try:
            if task_type == TASK_GUADAO_SCAN:
                self._update_guadao_scan_progress(
                    {
                        "status": "running",
                        "startedAt": utc_now_iso(),
                        "evaluatedCount": 0,
                        "candidateCount": 0,
                        "listedCount": 0,
                        "currentStep": "正在启动完整扫描",
                    }
                )
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
        finally:
            if lease_stop is not None:
                lease_stop.set()
            if lease_thread is not None:
                lease_thread.join(timeout=1.0)
        self._reschedule_after_task(dispatch_task, result=result)

    def _dispatch_task(self, task: Any, *, enabled: bool) -> dict[str, Any]:
        task_type = str(task["task_type"])
        if task_type == TASK_PROFIT_CYCLE:
            # The value captured when the task was claimed can already be
            # stale if the user toggled the executor meanwhile. Serialize the
            # final runtime/config projection and always obey the latest
            # persistent switch before starting the long-running cycle.
            with self._config_lock:
                db = Database(self.settings.db_path)
                try:
                    db.initialize()
                    runtime = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
                    enabled = bool(
                        runtime is not None
                        and bool(runtime["enabled"])
                        and not bool(runtime["migration_hold"])
                    )
                finally:
                    db.close()
                config = load_strategy_config(self.settings)
                if enabled and not config.profit_trade_enabled:
                    # Repair states created by the first runtime migration,
                    # where runtime.enabled could be true while the legacy
                    # strategy flag remained false.
                    config = set_profit_trade_enabled(self.settings, True)
                elif not enabled and config.profit_trade_enabled:
                    config = set_profit_trade_enabled(self.settings, False)
            if enabled:
                report = run_profit_trade_once(
                    self.settings,
                    config=config,
                    new_action_guard=lambda: self._new_actions_enabled(
                        RUNTIME_PROFIT_TRADE
                    ),
                )
                return report.to_dict()
            return self._run_profit_closure_once()
        if task_type == TASK_PROFIT_MANUAL_EXECUTION:
            payload = _task_payload(task)
            with self._config_lock:
                db = Database(self.settings.db_path)
                try:
                    db.initialize()
                    runtime = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
                    enabled = bool(
                        runtime is not None
                        and bool(runtime["enabled"])
                        and not bool(runtime["migration_hold"])
                    )
                finally:
                    db.close()
                config = load_strategy_config(self.settings)
            if not enabled:
                raise RuntimeError("Profit Trade runtime was disabled before one-click execution")
            return execute_manual_profit_trade_request(
                self.settings,
                request_id=str(payload.get("requestId") or ""),
                market_hash_name=str(payload.get("marketHashName") or ""),
                quantity=int(payload.get("quantity") or 0),
                approved_expected_roi=float(payload.get("approvedExpectedRoi") or 0),
                approved_scan_id=str(payload.get("approvedScanId") or "") or None,
                approved_observed_at=str(payload.get("approvedObservedAt") or "") or None,
                requested_at=str(payload.get("requestedAt") or "") or None,
                config=config,
                new_action_guard=lambda: (
                    self._manual_task_lease_owned(str(task["task_key"]))
                    and self._new_actions_enabled(RUNTIME_PROFIT_TRADE)
                ),
                refresh_config_each_item=True,
            )
        if task_type == TASK_PROFIT_SELECTION_WATCH:
            # Research selections are an explicit observation authorization,
            # not a real Profit Trade action.  They must remain usable while
            # Profit Trade itself is disabled, provided the shared Cookie gate
            # and Steam scheduler are healthy.
            return refresh_profit_trade_selection_watch(
                self.settings,
                config=load_strategy_config(self.settings),
            )
        if task_type == TASK_C5_RESEARCH_SCAN:
            payload = _task_payload(task)
            return run_c5_research_scan_chunk(
                self.settings,
                str(payload.get("requestId") or ""),
            )
        if task_type == TASK_GUADAO_AUDIT:
            payload = _task_payload(task)
            return run_guadao_audit(
                self.settings,
                str(payload.get("requestId") or ""),
            )
        self._emit_guadao_runtime_event(
            operation=task_type,
            message="挂刀到期任务开始执行",
            taskKey=str(task["task_key"]),
            accountId=task["account_id"],
            operationId=task["operation_id"],
        )
        action_guard = None
        if task_type == TASK_GUADAO_SCAN:
            action_guard = lambda: self._new_actions_enabled(RUNTIME_GUADAO)
        elif task_type == TASK_STALE_LISTING_RECHECK:
            task_key = str(task["task_key"])
            action_guard = lambda: (
                self._manual_task_lease_owned(task_key)
                and (
                    self._new_actions_enabled(RUNTIME_GUADAO)
                    or self._stale_maintenance_actions_enabled(task_key)
                )
            )
        engine = ExecutionEngine(
            self.settings,
            new_action_guard=action_guard,
        )
        if task_type == TASK_GUADAO_SCAN:
            engine._scan_progress_callback = self._update_guadao_scan_progress
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
            elif task_type == TASK_REBUY_BATCH:
                payload = _task_payload(task)
                result = engine.run_guadao_rebuy_batch_task(
                    str(payload.get("marketHashName") or "")
                )
            elif task_type == TASK_STALE_LISTING_RECHECK:
                result = engine.run_guadao_stale_listing_recheck_task()
            elif task_type == TASK_C5_DELIVERY_CONFIRM:
                result = engine.run_guadao_delivery_confirmation_task(int(task["operation_id"]))
            elif task_type == TASK_C5_ORDER_RECONCILE:
                result = engine.run_guadao_c5_submission_reconcile_task(
                    int(task["operation_id"])
                )
            else:
                result = {"ok": False, "error": f"unknown task type: {task_type}"}
            self._emit_guadao_runtime_event(
                operation=task_type,
                message="挂刀到期任务执行完成",
                level="INFO" if result.get("ok", True) else "ERROR",
                taskKey=str(task["task_key"]),
                result=result,
            )
            if task_type == TASK_STALE_LISTING_RECHECK:
                self._notify_stale_listing_recheck_result(result)
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
        history_deferred_ids = {
            int(value)
            for value in list((result or {}).get("historyDeferredOperationIds") or [])
            if safe_int(value) is not None
        }
        history_retry_at = _parse_iso(
            str((result or {}).get("historyRetryAt") or "")
        )
        if history_retry_at is not None and history_retry_at.tzinfo is None:
            history_retry_at = history_retry_at.replace(tzinfo=timezone.utc)
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
            waiting_for_sale_evidence = (
                raw_status == "listing_pending"
                and str(note.get("confirmationStatus") or "")
                == "listing_missing_unverified"
            )
            remains_due_kind = (
                task_type == TASK_STEAM_LISTING_CONFIRM
                and raw_status == "listing_pending"
                and not waiting_for_sale_evidence
            ) or (
                task_type == TASK_STEAM_SALE_EVIDENCE
                and (
                    raw_status == "listed"
                    or waiting_for_sale_evidence
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
            history_deferred = (
                task_type == TASK_STEAM_SALE_EVIDENCE
                and operation_id in history_deferred_ids
            )
            if history_deferred:
                # A shared account history walk may be delayed while the same
                # MyListings round already resolved other operations.  Keep
                # this operation's tier and wait for the next normal evidence
                # interval or the circuit Retry-After, whichever is later.
                next_index = tier_index
                delay = float(delays[min(tier_index + 1, len(delays) - 1)])
            elif request_failed:
                next_index = tier_index
                delay = min(30.0, float(delays[min(tier_index, len(delays) - 1)] or 30.0))
            else:
                next_index = min(tier_index + 1, len(delays) - 1)
                delay = float(delays[next_index])
            next_attempt_at = _iso_after(max(2.0, delay))
            if history_deferred and history_retry_at is not None:
                normal_next = _parse_iso(next_attempt_at)
                if normal_next is None or normal_next < history_retry_at:
                    next_attempt_at = history_retry_at.astimezone(timezone.utc).isoformat()
            db.upsert_scheduled_task(
                task_key,
                source=RUNTIME_GUADAO,
                task_type=task_type,
                next_attempt_at=next_attempt_at,
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
                last_error=(
                    str((result or {}).get("historyError") or "")
                    if history_deferred
                    else error
                    or (str(result.get("error") or "") if result else None)
                )
                or None,
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
        next_attempt_override: str | None = None
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
                account_id = str(task.get("account_id") or "")
                next_attempt_override = (
                    self._project_steam_account_sync_task(db, account_id)
                    if account_id
                    else None
                )
                terminal = next_attempt_override is None
                next_seconds = None
            elif task_type == TASK_PROFIT_CYCLE:
                next_seconds = PROFIT_TRADE_CYCLE_INTERVAL_SECONDS
            elif task_type == TASK_STALE_LISTING_RECHECK:
                next_seconds = float(schedule["staleListedCheckIntervalSeconds"])
            elif task_type == TASK_PROFIT_SELECTION_WATCH:
                active_count = safe_int((result or {}).get("activeCount"))
                if active_count is not None and active_count <= 0:
                    terminal = True
                    next_seconds = None
                else:
                    next_due_at = str((result or {}).get("nextDueAt") or "").strip()
                    if next_due_at:
                        next_attempt_override = next_due_at
                        next_seconds = None
                    else:
                        # Unexpected local failures should not turn a P3
                        # research task into a tight retry loop.
                        next_seconds = PROFIT_TRADE_CYCLE_INTERVAL_SECONDS
            elif task_type == TASK_C5_RESEARCH_SCAN:
                scan_status = str((result or {}).get("status") or "").strip().lower()
                if error:
                    next_seconds = 30.0
                elif scan_status in {
                    "paused",
                    "cancelled",
                    "completed",
                    "completed_with_errors",
                    "failed",
                }:
                    terminal = True
                    next_seconds = None
                else:
                    next_due_at = str((result or {}).get("nextAttemptAt") or "").strip()
                    if next_due_at:
                        next_attempt_override = next_due_at
                        next_seconds = None
                    else:
                        # Each invocation handles a bounded chunk.  A short
                        # hand-off keeps the P3 job moving without monopolizing
                        # the shared runtime worker.
                        next_seconds = 1.0
            elif task_type == TASK_GUADAO_AUDIT:
                # One audit attempt holds a renewed lease for its complete
                # read-only evidence walk. Every domain verdict is terminal
                # for this scheduled attempt.
                terminal = True
                next_seconds = None
            elif task_type == TASK_PROFIT_MANUAL_EXECUTION:
                terminal = True
                next_seconds = None
            elif task_type == TASK_REBUY_BATCH:
                market_hash_name = str(
                    _task_payload(task).get("marketHashName") or ""
                ).strip()
                next_attempts: list[str] = []
                if market_hash_name:
                    for op in db.list_pool_operations_by_type(
                        OP_REBUY_C5,
                        status="pending",
                        limit=5000,
                    ):
                        if str(op["market_hash_name"] or "") != market_hash_name:
                            continue
                        individual_key = f"rebuy:{int(op['id'])}"
                        individual = db.get_scheduled_task(individual_key)
                        if individual is not None and str(individual["status"] or "") == "running":
                            # A one-time legacy quick-buy caller still owns
                            # this operation. It cannot be reacquired by the
                            # batch lane until it releases final evidence.
                            continue
                        created = _parse_iso(op["created_at"]) or _now_utc()
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        age = max(0.0, (_now_utc() - created).total_seconds())
                        interval = _tier_interval_seconds(
                            schedule.get("rebuyRetryTiers"),
                            age_seconds=age,
                            minimum_seconds=30.0,
                        )
                        next_attempt_at = _iso_after(interval)
                        db.upsert_scheduled_task(
                            individual_key,
                            source=RUNTIME_GUADAO,
                            task_type=TASK_REBUY_ATTEMPT,
                            next_attempt_at=next_attempt_at,
                            operation_id=int(op["id"]),
                            payload={"createdAt": op["created_at"]},
                            status="waiting",
                            priority=1,
                        )
                        next_attempts.append(next_attempt_at)
                if not next_attempts:
                    terminal = True
                    next_seconds = None
                else:
                    next_attempt_override = min(next_attempts)
                    next_seconds = None
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
                    # Missing submission time means the 12-hour review clock has not
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
            elif task_type == TASK_C5_ORDER_RECONCILE:
                operation_id = int(task["operation_id"])
                op = db.conn.execute(
                    "SELECT * FROM pool_operations WHERE id = ?",
                    (operation_id,),
                ).fetchone()
                if (
                    op is None
                    or str(op["status"] or "") != C5_SUBMISSION_UNCONFIRMED_STATUS
                ):
                    terminal = True
                    next_seconds = None
                else:
                    attempt_count = max(1, int(task["attempt_count"] or 0))
                    if attempt_count >= C5_ORDER_RECONCILE_MAX_ATTEMPTS:
                        note = _read_note(op["note"])
                        alerted_at = str(
                            note.get("c5SubmissionCoverageAlertAt") or ""
                        ).strip()
                        first_alert = not alerted_at
                        if first_alert:
                            alerted_at = utc_now_iso()
                        note.update(
                            {
                                "c5SubmissionCoverageAlertAt": alerted_at,
                                "c5SubmissionReconcileSlowRetryAt": alerted_at,
                                "c5SubmissionReconcileAlertCode": (
                                    "reconcile_fast_attempts_exhausted"
                                ),
                                "c5SubmissionReconcileAttemptCount": attempt_count,
                                "c5SubmissionReconcileLastError": error
                                or str((result or {}).get("error") or "")
                                or None,
                                "c5SubmissionReconcileNextDelaySeconds": (
                                    C5_ORDER_RECONCILE_DEGRADED_DELAY_SECONDS
                                ),
                            }
                        )
                        db.update_pool_operation(
                            operation_id,
                            note=json.dumps(note, ensure_ascii=False),
                        )
                        if first_alert:
                            self._emit_guadao_runtime_event(
                                operation=TASK_C5_ORDER_RECONCILE,
                                message=(
                                    "C5 补仓提交结果暂未核对清楚，已转为慢速持续复核；"
                                    "确认远端终态前不会重复补仓"
                                ),
                                level="WARNING",
                                operationId=operation_id,
                                marketHashName=str(op["market_hash_name"] or ""),
                                attemptCount=attempt_count,
                                c5OutTradeNo=note.get("c5OutTradeNo"),
                                error=note.get("c5SubmissionReconcileLastError"),
                                reconcileState="slow_retry",
                            )
                        next_seconds = C5_ORDER_RECONCILE_DEGRADED_DELAY_SECONDS
                    else:
                        next_seconds = C5_ORDER_RECONCILE_DELAYS_SECONDS[
                            min(
                                attempt_count - 1,
                                len(C5_ORDER_RECONCILE_DELAYS_SECONDS) - 1,
                            )
                        ]
            else:
                terminal = True
                next_seconds = None

            if terminal:
                terminal_error = error
                if (
                    terminal_error is None
                    and result is not None
                    and result.get("ok") is False
                ):
                    result_errors = [
                        str(value)
                        for value in list(result.get("errors") or [])
                        if str(value).strip()
                    ]
                    terminal_error = (
                        "；".join(result_errors)
                        or str(result.get("error") or "manual execution failed")
                    )
                scheduled_status = "failed" if terminal_error else "completed"
                domain_status = str((result or {}).get("status") or "").strip().lower()
                if task_type == TASK_C5_RESEARCH_SCAN:
                    if domain_status == "cancelled":
                        scheduled_status = "cancelled"
                    elif domain_status == "failed":
                        scheduled_status = "failed"
                        terminal_error = terminal_error or str(
                            (result or {}).get("lastError")
                            or (result or {}).get("error")
                            or "C5 research scan failed"
                        )
                elif task_type == TASK_GUADAO_AUDIT and domain_status == "cancelled":
                    scheduled_status = "cancelled"
                db.complete_scheduled_task(
                    task_key,
                    self.worker_id,
                    status=scheduled_status,
                    error=terminal_error,
                )
                if task_type == TASK_PROFIT_MANUAL_EXECUTION:
                    state = db.get_executor_runtime_state(str(task["source"]))
                    if state is not None:
                        runtime_payload = _runtime_payload(state)
                        completed_at = utc_now_iso()
                        result_payload = dict(result or {})
                        summary = str(
                            terminal_error
                            or result_payload.get("summary")
                            or "Profit Trade 一键执行已完成"
                        )
                        runtime_payload["lastRunAt"] = completed_at
                        runtime_payload["lastRunSummary"] = summary
                        task_payload = _task_payload(task)
                        recent_runs = list(
                            runtime_payload.get("recentTaskRuns") or []
                        )
                        recent_runs.insert(
                            0,
                            {
                                "id": f"{task_key}:{completed_at}",
                                "completedAt": completed_at,
                                "taskKey": task_key,
                                "taskType": task_type,
                                "label": TASK_PUBLIC_LABELS.get(
                                    task_type,
                                    task_type.replace("_", " "),
                                ),
                                "marketHashName": str(
                                    result_payload.get("marketHashName")
                                    or task_payload.get("marketHashName")
                                    or ""
                                )
                                or None,
                                "ok": not bool(terminal_error)
                                and bool(result_payload.get("ok", True)),
                                "summary": summary,
                                "error": terminal_error
                                or result_payload.get("error"),
                                "result": result_payload,
                            },
                        )
                        runtime_payload["recentTaskRuns"] = recent_runs[:60]
                        db.upsert_executor_runtime_state(
                            str(task["source"]),
                            enabled=bool(state["enabled"]),
                            runtime_status=str(state["runtime_status"]),
                            migration_hold=bool(state["migration_hold"]),
                            gate_reason=state["gate_reason"],
                            heartbeat_at=utc_now_iso(),
                            payload=runtime_payload,
                        )
            else:
                next_attempt_at = next_attempt_override or _iso_after(float(next_seconds or 60.0))
                db.reschedule_scheduled_task(
                    task_key,
                    next_attempt_at=next_attempt_at,
                    worker_id=self.worker_id,
                    error=error or (None if result is None else str(result.get("error") or "") or None),
                    status="retry" if error else "pending",
                )
                if task_type == TASK_STALE_LISTING_RECHECK:
                    self._clear_stale_maintenance_authorization(db, task_key)
                state = db.get_executor_runtime_state(str(task["source"]))
                if state is not None and task_type not in READ_ONLY_AUXILIARY_TASKS:
                    runtime_payload = _runtime_payload(state)
                    completed_at = utc_now_iso()
                    runtime_payload["lastRunAt"] = completed_at
                    runtime_payload["lastRunSummary"] = (
                        f"{task_type} 失败：{error}"
                        if error
                        else f"{task_type} 已完成"
                    )
                    if str(task["source"]) == RUNTIME_GUADAO:
                        task_payload = _task_payload(task)
                        operation_id = safe_int(task["operation_id"])
                        market_hash_name = str(
                            task_payload.get("marketHashName") or ""
                        ).strip() or None
                        if market_hash_name is None and operation_id is not None:
                            operation_row = db.conn.execute(
                                "SELECT market_hash_name FROM pool_operations WHERE id = ?",
                                (operation_id,),
                            ).fetchone()
                            if operation_row is not None:
                                market_hash_name = str(
                                    operation_row["market_hash_name"] or ""
                                ).strip() or None
                        result_payload = dict(result or {})
                        public_result = {
                            key: value
                            for key, value in result_payload.items()
                            if key != "scanRound"
                        }
                        if error:
                            run_summary = f"执行失败：{error}"
                        elif task_type == TASK_GUADAO_SCAN:
                            run_summary = (
                                f"评估 {int(result_payload.get('evaluated') or 0)} 个，"
                                f"挂刀候选 {int(result_payload.get('candidateCount') or 0)} 个，"
                                f"本地可执行 {int(result_payload.get('executableCount') or 0)} 个，"
                                f"新上架 {int(result_payload.get('listed') or 0)} 件"
                            )
                        elif task_type == TASK_STEAM_ACCOUNT_SYNC:
                            if result_payload.get("partial"):
                                run_summary = (
                                    "部分完成："
                                    f"MyListings 解决 "
                                    f"{int(result_payload.get('myListingsResolved') or 0)} 笔，"
                                    f"历史确认卖出 "
                                    f"{int(result_payload.get('historySold') or 0)} 笔，"
                                    f"历史延期 "
                                    f"{int(result_payload.get('historyDeferred') or 0)} 笔"
                                )
                            else:
                                run_summary = (
                                    f"MyListings 解决 "
                                    f"{int(result_payload.get('myListingsResolved') or 0)} 笔，"
                                    f"确认挂单 {int(result_payload.get('confirmed') or 0)} 笔，"
                                    f"确认卖出 {int(result_payload.get('sold') or 0)} 笔"
                                )
                        elif task_type == TASK_REBUY_ATTEMPT:
                            run_summary = (
                                f"补仓成功 {int(result_payload.get('rebought') or 0)} 笔，"
                                f"当前状态 {result_payload.get('status') or '未知'}"
                            )
                        elif task_type == TASK_REBUY_BATCH:
                            price_batch_floor = safe_float(result_payload.get("priceBatchFloor"))
                            concrete_floor = safe_float(result_payload.get("concreteFloor"))
                            price_batch_label = (
                                f"¥{price_batch_floor:.2f}"
                                if price_batch_floor is not None
                                else "未知"
                            )
                            concrete_floor_label = (
                                f"¥{concrete_floor:.2f}"
                                if concrete_floor is not None
                                else "未知"
                            )
                            run_summary = (
                                f"到期 {int(result_payload.get('dueOperations') or 0)} 笔，"
                                f"聚合最低 {price_batch_label}，逐单最低 {concrete_floor_label}，"
                                f"具体在售 {int(result_payload.get('concreteListingsRead') or 0)} 件，"
                                f"差价快买 {int(result_payload.get('quickBuySuccesses') or 0)}/"
                                f"{int(result_payload.get('quickBuyAttempts') or 0)} 笔，"
                                f"批量匹配 {int(result_payload.get('normalBatchMatched') or 0)} 笔，"
                                f"提交成功 {int(result_payload.get('successes') or 0)} 笔，"
                                f"C5 请求 {int(result_payload.get('c5RequestCount') or 0)} 次，"
                                f"耗时 {float(result_payload.get('elapsedMs') or 0) / 1000:.2f}s"
                            )
                        elif task_type == TASK_C5_DELIVERY_CONFIRM:
                            run_summary = (
                                f"当前状态 {result_payload.get('status') or '未知'}，"
                                f"创建替换补仓 {int(result_payload.get('replacements') or 0)} 笔"
                            )
                        elif task_type == TASK_C5_ORDER_RECONCILE:
                            run_summary = (
                                f"提交结果 {result_payload.get('state') or '待核对'}，"
                                f"创建替换补仓 {int(result_payload.get('replacements') or 0)} 笔"
                            )
                        else:
                            run_summary = str(
                                result_payload.get("summary")
                                or result_payload.get("reason")
                                or "任务已完成"
                            )
                        recent_runs = list(runtime_payload.get("recentTaskRuns") or [])
                        recent_runs.insert(
                            0,
                            {
                                "id": f"{task_key}:{completed_at}",
                                "completedAt": completed_at,
                                "taskKey": task_key,
                                "taskType": task_type,
                                "label": TASK_PUBLIC_LABELS.get(
                                    task_type,
                                    task_type.replace("_", " "),
                                ),
                                "accountId": task["account_id"],
                                "accountName": self._public_account_name(task["account_id"]),
                                "operationId": operation_id,
                                "marketHashName": market_hash_name,
                                "ok": not bool(error) and bool(result_payload.get("ok", True)),
                                "summary": run_summary,
                                "error": error or result_payload.get("error"),
                                "result": public_result,
                            },
                        )
                        runtime_payload["recentTaskRuns"] = recent_runs[:60]
                        scan_round = result_payload.get("scanRound")
                        if task_type == TASK_GUADAO_SCAN and isinstance(scan_round, dict):
                            scan_rounds = list(runtime_payload.get("recentScanRounds") or [])
                            scan_rounds.insert(0, scan_round)
                            runtime_payload["recentScanRounds"] = scan_rounds[:12]
                    if task_type == TASK_GUADAO_SCAN:
                        runtime_payload.pop("activeScan", None)
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
                   OR (operation_type = ? AND status IN ('pending','delivery_pending','c5_submission_unconfirmed','manual_required','failed','c5_failed'))
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
            for runtime_key, task_key in (
                (RUNTIME_GUADAO, TASK_GUADAO_SCAN),
                (RUNTIME_PROFIT_TRADE, TASK_PROFIT_CYCLE),
            ):
                task = db.get_scheduled_task(task_key)
                if runtime_key in states:
                    states[runtime_key]["nextAttemptAt"] = (
                        task["next_attempt_at"] if task is not None else None
                    )
                    states[runtime_key]["taskStatus"] = (
                        str(task["status"] or "") if task is not None else None
                    )
                    states[runtime_key]["taskRunning"] = bool(
                        task is not None and str(task["status"] or "") == "running"
                    )
            return {
                "state": states.get(key) if key else None,
                "states": states,
                "cookieGate": self._cookie_gate_snapshot(db),
                "c5ApiCircuit": self._public_c5_api_circuit(
                    db.get_c5_api_circuit()
                ),
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
            # The queue can contain thousands of per-operation history rows.
            # Always expose the global stale-listing maintenance task even when
            # its next-attempt timestamp falls outside that chronological page;
            # otherwise the new hourly task is invisible in the dashboard while
            # it is still active and schedulable.
            stale_task_row = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            if stale_task_row is not None and not any(
                str(row["task_key"] or "") == TASK_STALE_LISTING_RECHECK
                for row in raw_task_rows
            ):
                raw_task_rows = [stale_task_row, *raw_task_rows]
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
            c5_evidence_pending = self._operation_count(
                db,
                OP_REBUY_C5,
                [C5_SUBMISSION_UNCONFIRMED_STATUS],
            )
            counts = {
                "activeListings": self._operation_count(db, OP_SELL_STEAM, ["listed"]),
                "pendingListingConfirmations": self._operation_count(
                    db,
                    OP_SELL_STEAM,
                    ["listing_pending"],
                ),
                "pendingRebuys": self._operation_count(db, OP_REBUY_C5, ["pending"]),
                "deliveryPending": self._operation_count(db, OP_REBUY_C5, ["delivery_pending"]),
                # `c5EvidencePending` is the public business name.  Retain
                # `submissionUnconfirmed` for existing callers while the UI
                # migrates from the old technical wording.
                "c5EvidencePending": c5_evidence_pending,
                "submissionUnconfirmed": c5_evidence_pending,
                "issues": len(self._issue_rows(db, include_acknowledged=False)),
            }
            queue_snapshot = {}
            snapshot_loader = getattr(db, "get_steam_queue_snapshot", None)
            if callable(snapshot_loader):
                queue_snapshot = snapshot_loader()
            circuits_loader = getattr(db, "list_steam_route_circuits", None)
            circuits = [self._public_circuit(row) for row in circuits_loader()] if callable(circuits_loader) else []
            c5_api_circuit = self._public_c5_api_circuit(db.get_c5_api_circuit())
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
                    "rebuyReferenceFloor": safe_float(rule.get("rebuyReferenceFloor")),
                    "enabled": bool(rule.get("enabled", True)),
                    **self._latest_case_ratio_snapshot(db, market_hash_name),
                    }
                )
            public_issues = self._issue_rows(db, include_acknowledged=False)
            guadao_runtime_payload = (
                guadao_runtime.get("payload", {})
                if isinstance(guadao_runtime.get("payload"), dict)
                else {}
            )
            scan_task_row = db.get_scheduled_task(TASK_GUADAO_SCAN)
            current_scan = (
                dict(guadao_runtime_payload.get("activeScan") or {})
                if scan_task_row is not None
                and str(scan_task_row["status"] or "") == "running"
                and isinstance(guadao_runtime_payload.get("activeScan"), dict)
                else None
            )
            return {
                "generatedAt": utc_now_iso(),
                "backend": {"online": True, "workerAlive": self.alive, "lastError": self._last_error},
                "runtime": guadao_runtime,
                "runtimes": runtimes,
                "cookieGate": cookie,
                "c5ApiCircuit": c5_api_circuit,
                "counts": counts,
                "summary": {
                    "activeListings": counts["activeListings"],
                    "pendingListingConfirmations": counts["pendingListingConfirmations"],
                    "pendingRebuys": counts["pendingRebuys"],
                    "c5EvidencePending": counts["c5EvidencePending"],
                    "deliveryPending": counts["deliveryPending"],
                    "issueCount": counts["issues"],
                    "steamHeatPct": min(100.0, recent_request_count / 30.0 * 100.0),
                },
                "tasks": tasks,
                "dueTasks": due_tasks,
                "taskQueue": task_queue,
                "recentTaskRuns": list(
                    guadao_runtime_payload.get("recentTaskRuns") or []
                ),
                "currentScan": current_scan,
                "scanRounds": list(
                    guadao_runtime_payload.get("recentScanRounds") or []
                ),
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

    def _public_c5_api_circuit(self, row: Any | None) -> dict[str, Any]:
        if row is None:
            return {"state": "closed", "blocked": False}
        return {
            "state": row["state"],
            "blocked": str(row["state"]) == "open",
            "errorCode": row["error_code"],
            "requestIp": row["request_ip"],
            "triggerSource": row["trigger_source"],
            "triggerOperation": row["trigger_operation"],
            "firstErrorAt": row["first_error_at"],
            "lastErrorAt": row["last_error_at"],
            "nextProbeAt": row["next_probe_at"],
            "alertSentAt": row["alert_sent_at"],
            "recoveredAt": row["recovered_at"],
            "recoveryAlertSentAt": row["recovery_alert_sent_at"],
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
        market_hash_name: str | None = None,
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
                if str(task["status"] or "") not in {
                    "pending",
                    "retry",
                    "running",
                    "waiting",
                }:
                    continue
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
                children = sorted(
                    rebuy_by_sell.get(int(sell["id"]), []),
                    key=lambda row: int(row["id"]),
                )
                child = max(children, key=lambda row: int(row["id"])) if children else None
                projected.append(
                    self._public_operation(
                        sell,
                        rebuy=child,
                        rebuy_attempts=children,
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
            item_filter = str(market_hash_name or "").strip()
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
            # Item options are a facet of the current query.  Apply keyword,
            # account, status and date filters first, but deliberately exclude
            # the selected item itself so users can switch between the other
            # items available under the same conditions.
            item_option_map: dict[str, dict[str, Any]] = {}
            for item in projected:
                item_market_hash_name = str(item.get("marketHashName") or "").strip()
                if not item_market_hash_name:
                    continue
                option = item_option_map.setdefault(
                    item_market_hash_name,
                    {
                        "marketHashName": item_market_hash_name,
                        "displayName": str(
                            item.get("displayName") or item_market_hash_name
                        ),
                        "count": 0,
                    },
                )
                option["count"] += 1
            item_options = sorted(
                item_option_map.values(),
                key=lambda option: (
                    -int(option["count"]),
                    str(option["displayName"]).casefold(),
                    str(option["marketHashName"]).casefold(),
                ),
            )
            if item_filter:
                projected = [
                    row
                    for row in projected
                    if row.get("marketHashName") == item_filter
                ]
            projected.sort(key=lambda row: str(row.get("updatedAt") or row.get("createdAt") or ""), reverse=True)
            total = len(projected)
            safe_page_size = max(1, min(int(page_size), 100))
            safe_page = max(1, int(page))
            start = (safe_page - 1) * safe_page_size
            page_rows = projected[start : start + safe_page_size]
            c5_evidence_pending = sum(
                row["status"] == C5_SUBMISSION_UNCONFIRMED_STATUS
                for row in projected
            )
            summary = {
                "total": total,
                "pendingConfirmation": sum(row["status"] == "listing_pending" for row in projected),
                "steamListed": sum(row["status"] == "listed" for row in projected),
                "pendingRebuy": sum(row["status"] == "sold" for row in projected),
                "deliveryPending": sum(row["status"] == "delivery_pending" for row in projected),
                "c5EvidencePending": c5_evidence_pending,
                # Compatibility for clients that still render the old field.
                "submissionUnconfirmed": c5_evidence_pending,
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
                "itemOptions": item_options,
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

    @staticmethod
    def _normalize_guadao_batch_ids(operation_ids: Any) -> list[int]:
        if not isinstance(operation_ids, list):
            raise ValueError("operationIds must be an array")
        normalized: list[int] = []
        for raw in operation_ids:
            operation_id = safe_int(raw)
            if operation_id is None or operation_id <= 0:
                raise ValueError("operationIds contains an invalid operation ID")
            if operation_id not in normalized:
                normalized.append(operation_id)
        if not normalized:
            raise ValueError("operationIds must not be empty")
        if len(normalized) > 100:
            raise ValueError("a batch may contain at most 100 operations")
        return normalized

    @staticmethod
    def _rebuy_has_remote_order_evidence(note: dict[str, Any]) -> bool:
        # batch_buy returns an explicit failedList for requests that were
        # rejected before an order was created.  Keep the outTradeNo for
        # audit/idempotency, but do not mistake that failed request id for a
        # remote C5 order.  Any actual order id still wins and blocks manual
        # refreezing until its terminal state is reconciled.
        if (
            str(note.get("c5BatchSubmissionState") or "").strip().lower()
            == "rejected"
            and not str(note.get("c5OrderId") or "").strip()
            and not str(note.get("c5TradeOrderId") or "").strip()
        ):
            return False
        return any(
            note.get(key) not in (None, "", False)
            for key in (
                "c5OutTradeNo",
                "c5OrderId",
                "c5TradeOrderId",
                "c5OrderSubmittedAt",
            )
        )

    @staticmethod
    def _rebuy_steam_net_amount(
        sell_note: dict[str, Any],
        rebuy_note: dict[str, Any],
    ) -> float | None:
        net_amount = safe_float(
            rebuy_note.get("steamSellerNetPrice")
            or sell_note.get("steamSellerNetPrice")
        )
        if net_amount is not None and net_amount > 0:
            return net_amount
        steam_list_price = safe_float(
            rebuy_note.get("steamListPrice") or sell_note.get("steamListPrice")
        )
        steam_net_factor = safe_float(
            rebuy_note.get("steamNetFactorAtOpen")
            or sell_note.get("steamNetFactorAtOpen")
        )
        if steam_list_price and steam_net_factor and steam_list_price > 0 and steam_net_factor > 0:
            return steam_list_price * steam_net_factor
        return None

    @staticmethod
    def _append_manual_rebuy_history(
        note: dict[str, Any],
        event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        existing = note.get("manualRebuyRefreezeHistory")
        history = [dict(item) for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
        return [*history[-49:], dict(event)]

    def _guadao_batch_pairs(
        self,
        db: Database,
        operation_ids: list[int],
    ) -> dict[int, tuple[Any | None, Any | None, list[Any]]]:
        placeholders = ",".join("?" for _ in operation_ids)
        sell_rows = db.conn.execute(
            f"SELECT * FROM pool_operations WHERE id IN ({placeholders})",
            tuple(operation_ids),
        ).fetchall()
        sells = {int(row["id"]): row for row in sell_rows}
        children_by_sell: dict[int, list[Any]] = {}
        for rebuy in db.list_pool_operations_by_type(OP_REBUY_C5, limit=50_000):
            source_sell_id = safe_int(
                _read_note(rebuy["note"]).get("sourceSellOperationId")
            )
            if source_sell_id in operation_ids:
                children_by_sell.setdefault(int(source_sell_id), []).append(rebuy)
        pairs: dict[int, tuple[Any | None, Any | None, list[Any]]] = {}
        for operation_id in operation_ids:
            children = sorted(
                children_by_sell.get(operation_id, []),
                key=lambda row: int(row["id"]),
            )
            pending_children = [
                row for row in children if str(row["status"] or "") == "pending"
            ]
            current = pending_children[0] if len(pending_children) == 1 else None
            pairs[operation_id] = (sells.get(operation_id), current, children)
        return pairs

    @staticmethod
    def _batch_pair_error(
        db: Database,
        sell: Any | None,
        rebuy: Any | None,
        children: list[Any],
    ) -> tuple[str, str] | None:
        if sell is None:
            return "operation_not_found", "挂刀流水不存在"
        if str(sell["strategy"] or "") != RUNTIME_GUADAO or str(
            sell["operation_type"] or ""
        ) != OP_SELL_STEAM:
            return "not_guadao_sell_operation", "不是挂刀 Steam 卖出流水"
        if str(sell["status"] or "") != "sold":
            return "not_sold_pending_rebuy", "仅已卖出待补仓流水允许批量操作"
        pending_children = [
            row for row in children if str(row["status"] or "") == "pending"
        ]
        if len(pending_children) > 1:
            return "multiple_pending_rebuys", "存在多个待补仓子流水，必须先人工确认唯一当前流水"
        if any(str(row["status"] or "") == "delivery_pending" for row in children):
            return "c5_delivery_pending", "同一卖出流水存在 C5 发货确认中的补仓"
        if any(str(row["status"] or "") == "completed" for row in children):
            return "rebuy_already_completed", "同一卖出流水已经存在完成的补仓"
        if rebuy is None:
            return "pending_rebuy_not_found", "未找到对应的待补仓子流水"
        if str(rebuy["status"] or "") != "pending":
            return "rebuy_not_pending", "对应补仓流水已不再处于等待状态"
        rebuy_note = _read_note(rebuy["note"])
        if UnifiedRuntimeController._rebuy_has_remote_order_evidence(rebuy_note):
            return "c5_order_state_unresolved", "存在 C5 订单证据，必须先确认远端终态"
        task = db.get_scheduled_task(f"rebuy:{int(rebuy['id'])}")
        if task is not None and str(task["status"] or "") == "running":
            return "rebuy_task_running", "补仓任务正在执行，请等待本轮结束后再操作"
        return None

    @staticmethod
    def _batch_result(
        operation_id: int,
        *,
        ok: bool,
        code: str,
        message: str,
        sell: Any | None = None,
        rebuy: Any | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "operationId": operation_id,
            "tradeNo": f"GD-{operation_id}",
            "rebuyOperationId": int(rebuy["id"]) if rebuy is not None else None,
            "marketHashName": str(sell["market_hash_name"]) if sell is not None else None,
            "ok": ok,
            "code": code,
            "message": message,
            **extra,
        }

    @staticmethod
    def _existing_guadao_batch_audit(
        db: Database,
        *,
        event_type: str,
        sell_operation_id: int,
        request_id: str,
    ) -> dict[str, Any] | None:
        row = db.conn.execute(
            """
            SELECT result_json
            FROM guadao_operation_audit_events
            WHERE event_type = ? AND sell_operation_id = ? AND request_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (event_type, int(sell_operation_id), str(request_id)),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["result_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {**payload, "idempotentReplay": True}

    @staticmethod
    def _insert_guadao_batch_audit(
        db: Database,
        *,
        event_type: str,
        sell_operation_id: int,
        rebuy_operation_id: int,
        batch_id: str,
        request_id: str,
        reason: str | None,
        old_value: dict[str, Any],
        new_value: dict[str, Any],
        result: dict[str, Any],
        created_at: str,
    ) -> None:
        db.conn.execute(
            """
            INSERT INTO guadao_operation_audit_events (
                event_type, sell_operation_id, rebuy_operation_id,
                batch_id, request_id, actor, reason,
                old_value_json, new_value_json, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'web_user', ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                int(sell_operation_id),
                int(rebuy_operation_id),
                str(batch_id),
                str(request_id),
                reason,
                json.dumps(old_value, ensure_ascii=False),
                json.dumps(new_value, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                created_at,
            ),
        )

    def batch_refreeze_guadao_rebuys(
        self,
        operation_ids: Any,
        *,
        rebuy_price: float,
        execute_now: bool = True,
        confirmed: bool = False,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("confirmed=true is required")
        price = round(float(rebuy_price), 2)
        if price <= 0:
            raise ValueError("rebuyPrice must be positive")
        ids = self._normalize_guadao_batch_ids(operation_ids)
        batch_id = f"GDRF-{uuid.uuid4().hex[:12]}"
        request_key = str(request_id or uuid.uuid4().hex).strip()[:100]
        safe_reason = str(reason or "").strip()[:300] or "用户批量重设补仓冻结价格"
        now = utc_now_iso()
        db = Database(self.settings.db_path)
        results: list[dict[str, Any]] = []
        try:
            db.initialize()
            pairs = self._guadao_batch_pairs(db, ids)
            market_hash_names = {
                str(sell["market_hash_name"])
                for sell, _rebuy, _children in pairs.values()
                if sell is not None
            }
            if len(market_hash_names) > 1:
                raise ValueError("批量重设补仓价仅支持同一物品品类")
            for operation_id in ids:
                sell, rebuy, children = pairs[operation_id]
                replay = self._existing_guadao_batch_audit(
                    db,
                    event_type="manual_rebuy_refrozen",
                    sell_operation_id=operation_id,
                    request_id=request_key,
                )
                if replay is not None:
                    results.append(replay)
                    continue
                blocked = self._batch_pair_error(db, sell, rebuy, children)
                if blocked is not None:
                    results.append(
                        self._batch_result(
                            operation_id,
                            ok=False,
                            code=blocked[0],
                            message=blocked[1],
                            sell=sell,
                            rebuy=rebuy,
                        )
                    )
                    continue
                sell_note = _read_note(sell["note"])
                rebuy_note = _read_note(rebuy["note"])
                steam_net_amount = self._rebuy_steam_net_amount(sell_note, rebuy_note)
                if steam_net_amount is None or steam_net_amount <= 0:
                    results.append(
                        self._batch_result(
                            operation_id,
                            ok=False,
                            code="steam_net_amount_missing",
                            message="缺少 Steam 卖出税后到手，无法重算冻结比例",
                            sell=sell,
                            rebuy=rebuy,
                        )
                    )
                    continue
                new_ratio = round(price / steam_net_amount, 6)
                old_price = safe_float(rebuy["expected_price"]) or safe_float(
                    sell_note.get("rebuyPrice")
                )
                old_ratio = safe_float(rebuy_note.get("maxRebuyRatioAtOpen")) or safe_float(
                    sell_note.get("maxRebuyRatioAtOpen")
                )
                event = {
                    "batchId": batch_id,
                    "action": "manual_refreeze_and_retry",
                    "at": now,
                    "oldFrozenRebuyPrice": old_price,
                    "newFrozenRebuyPrice": price,
                    "oldFrozenRebuyRatio": old_ratio,
                    "newFrozenRebuyRatio": new_ratio,
                    "steamNetAmount": round(steam_net_amount, 6),
                    "executeNow": bool(execute_now),
                    "requestId": request_key,
                    "reason": safe_reason,
                }
                sell_updated = {
                    **sell_note,
                    "rebuyPrice": price,
                    "maxRebuyRatioAtOpen": new_ratio,
                    "currentRebuyRatio": new_ratio,
                    "manualRebuyRefrozenPrice": price,
                    "manualRebuyRefrozenRatio": new_ratio,
                    "manualRebuySteamNetAmount": round(steam_net_amount, 6),
                    "manualRebuyRefrozenAt": now,
                    "manualRebuyRefreezeBatchId": batch_id,
                    "manualRebuyRefreezeHistory": self._append_manual_rebuy_history(
                        sell_note, event
                    ),
                }
                rebuy_updated = {
                    **rebuy_note,
                    "maxRebuyRatioAtOpen": new_ratio,
                    "currentRebuyRatio": new_ratio,
                    "manualRebuyRefrozenPrice": price,
                    "manualRebuyRefrozenRatio": new_ratio,
                    "manualRebuySteamNetAmount": round(steam_net_amount, 6),
                    "manualRebuyRefrozenAt": now,
                    "manualRebuyRefreezeBatchId": batch_id,
                    "manualRebuyRefreezeHistory": self._append_manual_rebuy_history(
                        rebuy_note, event
                    ),
                }
                if safe_int(rebuy_note.get("replacementForRebuyOperationId")) is not None:
                    rebuy_updated["replacementMaxPrice"] = price
                    rebuy_updated["replacementPricePolicy"] = "manual_refreeze"
                task_key = f"rebuy:{int(rebuy['id'])}"
                success_result = self._batch_result(
                    operation_id,
                    ok=True,
                    code="rebuy_refrozen",
                    message="已重设冻结价格与比例，并重新安排补仓" if execute_now else "已重设冻结价格与比例",
                    sell=sell,
                    rebuy=rebuy,
                    oldFrozenRebuyPrice=old_price,
                    newFrozenRebuyPrice=price,
                    oldFrozenRebuyRatio=old_ratio,
                    newFrozenRebuyRatio=new_ratio,
                    steamNetAmount=round(steam_net_amount, 6),
                    executeNow=bool(execute_now),
                )
                try:
                    db.conn.execute("BEGIN IMMEDIATE")
                    current_sell, current_rebuy, current_children = self._guadao_batch_pairs(
                        db, [operation_id]
                    )[operation_id]
                    current_blocked = self._batch_pair_error(
                        db, current_sell, current_rebuy, current_children
                    )
                    if current_blocked is not None:
                        raise RuntimeError(current_blocked[0])
                    current_task = db.get_scheduled_task(task_key)
                    if (
                        int(current_rebuy["id"]) != int(rebuy["id"])
                        or str(current_sell["note"] or "") != str(sell["note"] or "")
                        or str(current_rebuy["note"] or "") != str(rebuy["note"] or "")
                        or safe_float(current_rebuy["expected_price"])
                        != safe_float(rebuy["expected_price"])
                    ):
                        raise RuntimeError("rebuy_data_changed")
                    db.conn.execute(
                        "UPDATE pool_operations SET note = ? WHERE id = ?",
                        (json.dumps(sell_updated, ensure_ascii=False), operation_id),
                    )
                    db.conn.execute(
                        "UPDATE pool_operations SET expected_price = ?, note = ? WHERE id = ?",
                        (
                            price,
                            json.dumps(rebuy_updated, ensure_ascii=False),
                            int(rebuy["id"]),
                        ),
                    )
                    if execute_now and current_task is not None:
                        db.conn.execute(
                            """
                            UPDATE scheduled_tasks
                            SET status = 'pending', next_attempt_at = ?, last_error = NULL,
                                lease_owner = NULL, lease_expires_at = NULL,
                                completed_at = NULL, updated_at = ?
                            WHERE task_key = ?
                            """,
                            (now, now, task_key),
                        )
                    self._insert_guadao_batch_audit(
                        db,
                        event_type="manual_rebuy_refrozen",
                        sell_operation_id=operation_id,
                        rebuy_operation_id=int(rebuy["id"]),
                        batch_id=batch_id,
                        request_id=request_key,
                        reason=safe_reason,
                        old_value={
                            "expectedPrice": old_price,
                            "listingRatioAtOpen": safe_float(sell_note.get("listingRatioAtOpen")),
                            "maxRebuyRatioAtOpen": old_ratio,
                            "guadaoMaxListingRatioAtOpen": safe_float(sell_note.get("guadaoMaxListingRatioAtOpen")),
                            "replacementMaxPrice": safe_float(rebuy_note.get("replacementMaxPrice")),
                        },
                        new_value={
                            "frozenRebuyPrice": price,
                            "frozenRebuyRatio": new_ratio,
                            "steamNetAmount": round(steam_net_amount, 6),
                            "executeNow": bool(execute_now),
                        },
                        result=success_result,
                        created_at=now,
                    )
                    db.conn.commit()
                except Exception as exc:
                    db.conn.rollback()
                    results.append(
                        self._batch_result(
                            operation_id,
                            ok=False,
                            code="state_changed",
                            message=f"流水状态刚刚发生变化，请刷新后重试：{exc}",
                            sell=sell,
                            rebuy=rebuy,
                        )
                    )
                    continue
                if execute_now and db.get_scheduled_task(task_key) is None:
                    db.upsert_scheduled_task(
                        task_key,
                        source=RUNTIME_GUADAO,
                        task_type=TASK_REBUY_ATTEMPT,
                        next_attempt_at=now,
                        operation_id=int(rebuy["id"]),
                        priority=1,
                        payload={"manualRebuyRefreezeBatchId": batch_id},
                    )
                self._emit_guadao_runtime_event(
                    operation="manual_rebuy_refrozen",
                    message="用户批量重设了本笔补仓冻结价格与冻结比例",
                    operationId=operation_id,
                    rebuyOperationId=int(rebuy["id"]),
                    marketHashName=str(sell["market_hash_name"]),
                    batchId=batch_id,
                    oldFrozenRebuyPrice=old_price,
                    newFrozenRebuyPrice=price,
                    oldFrozenRebuyRatio=old_ratio,
                    newFrozenRebuyRatio=new_ratio,
                    executeNow=bool(execute_now),
                    requestId=request_key,
                    reason=safe_reason,
                )
                results.append(success_result)
        finally:
            db.close()
        success_count = sum(bool(item["ok"]) for item in results)
        if success_count and execute_now:
            self.wake()
        return {
            "ok": success_count > 0,
            "batchId": batch_id,
            "requestId": request_key,
            "successCount": success_count,
            "failedCount": len(results) - success_count,
            "results": results,
        }

    def batch_complete_guadao_rebuys_manually(
        self,
        operation_ids: Any,
        *,
        actual_rebuy_price: float,
        source: str,
        completed_at: str,
        memo: str | None = None,
        external_order_ref: str | None = None,
        confirmed: bool = False,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("confirmed=true is required")
        price = round(float(actual_rebuy_price), 2)
        if price <= 0:
            raise ValueError("actualRebuyPrice must be positive")
        normalized_source = str(source or "").strip()
        if not normalized_source:
            raise ValueError("source is required")
        parsed_completed_at = _parse_iso(str(completed_at or ""))
        if parsed_completed_at is None or parsed_completed_at.tzinfo is None:
            raise ValueError("completedAt must be an ISO 8601 timestamp with timezone")
        parsed_completed_at = parsed_completed_at.astimezone(timezone.utc)
        if parsed_completed_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("completedAt must not be in the future")
        completed_iso = parsed_completed_at.isoformat()
        safe_memo = str(memo or "").strip()[:500] or None
        safe_external_ref = str(external_order_ref or "").strip()[:200] or None
        if not safe_memo and not safe_external_ref:
            raise ValueError("memo or externalOrderRef is required")
        ids = self._normalize_guadao_batch_ids(operation_ids)
        batch_id = f"GDMC-{uuid.uuid4().hex[:12]}"
        request_key = str(request_id or uuid.uuid4().hex).strip()[:100]
        safe_reason = str(reason or "").strip()[:300] or "用户确认已在其他平台完成补仓"
        recorded_at = utc_now_iso()
        db = Database(self.settings.db_path)
        results: list[dict[str, Any]] = []
        try:
            db.initialize()
            pairs = self._guadao_batch_pairs(db, ids)
            market_hash_names = {
                str(sell["market_hash_name"])
                for sell, _rebuy, _children in pairs.values()
                if sell is not None
            }
            if len(market_hash_names) > 1:
                raise ValueError("批量手动完结仅支持同一物品品类")
            for operation_id in ids:
                sell, rebuy, children = pairs[operation_id]
                replay = self._existing_guadao_batch_audit(
                    db,
                    event_type="manual_external_rebuy_completed",
                    sell_operation_id=operation_id,
                    request_id=request_key,
                )
                if replay is not None:
                    results.append(replay)
                    continue
                blocked = self._batch_pair_error(db, sell, rebuy, children)
                if blocked is not None:
                    results.append(
                        self._batch_result(
                            operation_id,
                            ok=False,
                            code=blocked[0],
                            message=blocked[1],
                            sell=sell,
                            rebuy=rebuy,
                        )
                    )
                    continue
                sell_note = _read_note(sell["note"])
                rebuy_note = _read_note(rebuy["note"])
                steam_net_amount = self._rebuy_steam_net_amount(sell_note, rebuy_note)
                if steam_net_amount is None or steam_net_amount <= 0:
                    results.append(
                        self._batch_result(
                            operation_id,
                            ok=False,
                            code="steam_net_amount_missing",
                            message="缺少 Steam 卖出税后到手，无法计算实际闭环比例",
                            sell=sell,
                            rebuy=rebuy,
                        )
                    )
                    continue
                sold_at = _parse_iso(
                    str(
                        rebuy_note.get("steamSoldAt")
                        or sell_note.get("steamSoldAt")
                        or sell["completed_at"]
                        or ""
                    )
                )
                if sold_at is not None:
                    if sold_at.tzinfo is None:
                        sold_at = sold_at.replace(tzinfo=timezone.utc)
                    if parsed_completed_at < sold_at.astimezone(timezone.utc):
                        results.append(
                            self._batch_result(
                                operation_id,
                                ok=False,
                                code="completed_before_steam_sale",
                                message="外部补仓完成时间不能早于 Steam 官方卖出时间",
                                sell=sell,
                                rebuy=rebuy,
                            )
                        )
                        continue
                actual_ratio = round(price / steam_net_amount, 6)
                old_price = safe_float(rebuy["expected_price"]) or safe_float(
                    sell_note.get("rebuyPrice")
                )
                old_ratio = safe_float(rebuy_note.get("maxRebuyRatioAtOpen")) or safe_float(
                    sell_note.get("maxRebuyRatioAtOpen")
                )
                event = {
                    "batchId": batch_id,
                    "action": "manual_external_rebuy_completed",
                    "at": recorded_at,
                    "completedAt": completed_iso,
                    "oldFrozenRebuyPrice": old_price,
                    "newFrozenRebuyPrice": price,
                    "oldFrozenRebuyRatio": old_ratio,
                    "newFrozenRebuyRatio": actual_ratio,
                    "steamNetAmount": round(steam_net_amount, 6),
                    "source": normalized_source,
                    "externalOrderRef": safe_external_ref,
                    "requestId": request_key,
                    "reason": safe_reason,
                }
                common_updates = {
                    "rebuyPrice": price,
                    "maxRebuyRatioAtOpen": actual_ratio,
                    "currentRebuyRatio": actual_ratio,
                    "manualRebuyRefrozenPrice": price,
                    "manualRebuyRefrozenRatio": actual_ratio,
                    "manualRebuySteamNetAmount": round(steam_net_amount, 6),
                    "manualRebuyRefrozenAt": recorded_at,
                    "manualRebuyRefreezeBatchId": batch_id,
                    "manualExternalRebuyCompletedAt": completed_iso,
                    "manualExternalRebuyRecordedAt": recorded_at,
                    "manualExternalRebuySource": normalized_source,
                    "manualExternalRebuyMemo": safe_memo,
                    "manualExternalOrderRef": safe_external_ref,
                }
                sell_updated = {
                    **sell_note,
                    **common_updates,
                    "manualRebuyRefreezeHistory": self._append_manual_rebuy_history(
                        sell_note, event
                    ),
                }
                rebuy_updated = {
                    **rebuy_note,
                    **common_updates,
                    C5_DELIVERY_STATUS_KEY: "manual_external_completed",
                    "manualRebuyRefreezeHistory": self._append_manual_rebuy_history(
                        rebuy_note, event
                    ),
                }
                task_key = f"rebuy:{int(rebuy['id'])}"
                success_result = self._batch_result(
                    operation_id,
                    ok=True,
                    code="manual_external_completed",
                    message="已按其他平台实际补仓价格手动完结",
                    sell=sell,
                    rebuy=rebuy,
                    actualRebuyPrice=price,
                    actualRebuyRatio=actual_ratio,
                    steamNetAmount=round(steam_net_amount, 6),
                    source=normalized_source,
                    completedAt=completed_iso,
                )
                try:
                    db.conn.execute("BEGIN IMMEDIATE")
                    current_sell, current_rebuy, current_children = self._guadao_batch_pairs(
                        db, [operation_id]
                    )[operation_id]
                    current_blocked = self._batch_pair_error(
                        db, current_sell, current_rebuy, current_children
                    )
                    if current_blocked is not None:
                        raise RuntimeError(current_blocked[0])
                    if (
                        int(current_rebuy["id"]) != int(rebuy["id"])
                        or str(current_sell["note"] or "") != str(sell["note"] or "")
                        or str(current_rebuy["note"] or "") != str(rebuy["note"] or "")
                        or safe_float(current_rebuy["expected_price"])
                        != safe_float(rebuy["expected_price"])
                    ):
                        raise RuntimeError("rebuy_data_changed")
                    db.conn.execute(
                        "UPDATE pool_operations SET note = ? WHERE id = ?",
                        (json.dumps(sell_updated, ensure_ascii=False), operation_id),
                    )
                    db.conn.execute(
                        """
                        UPDATE pool_operations
                        SET status = 'completed', expected_price = ?, actual_price = ?,
                            note = ?, completed_at = ?
                        WHERE id = ?
                        """,
                        (
                            price,
                            price,
                            json.dumps(rebuy_updated, ensure_ascii=False),
                            completed_iso,
                            int(rebuy["id"]),
                        ),
                    )
                    db.conn.execute(
                        "DELETE FROM scheduled_tasks WHERE task_key = ? AND status != 'running'",
                        (task_key,),
                    )
                    open_rebuy_count = int(
                        db.conn.execute(
                            """
                            SELECT COUNT(*) AS count
                            FROM pool_operations
                            WHERE operation_type = ? AND market_hash_name = ?
                              AND status IN ('pending', 'delivery_pending')
                            """,
                            (OP_REBUY_C5, str(sell["market_hash_name"])),
                        ).fetchone()["count"]
                    )
                    next_pool_status = (
                        POOL_STATUS_PENDING_REBUY
                        if open_rebuy_count > 0
                        else POOL_STATUS_HOLDING
                    )
                    db.conn.execute(
                        "UPDATE inventory_pool SET status = ?, updated_at = ? WHERE market_hash_name = ?",
                        (next_pool_status, recorded_at, str(sell["market_hash_name"])),
                    )
                    self._insert_guadao_batch_audit(
                        db,
                        event_type="manual_external_rebuy_completed",
                        sell_operation_id=operation_id,
                        rebuy_operation_id=int(rebuy["id"]),
                        batch_id=batch_id,
                        request_id=request_key,
                        reason=safe_reason,
                        old_value={
                            "expectedPrice": old_price,
                            "listingRatioAtOpen": safe_float(sell_note.get("listingRatioAtOpen")),
                            "maxRebuyRatioAtOpen": old_ratio,
                            "guadaoMaxListingRatioAtOpen": safe_float(sell_note.get("guadaoMaxListingRatioAtOpen")),
                            "replacementMaxPrice": safe_float(rebuy_note.get("replacementMaxPrice")),
                        },
                        new_value={
                            "actualRebuyPrice": price,
                            "actualRebuyRatio": actual_ratio,
                            "steamNetAmount": round(steam_net_amount, 6),
                            "source": normalized_source,
                            "completedAt": completed_iso,
                            "externalOrderRef": safe_external_ref,
                        },
                        result=success_result,
                        created_at=recorded_at,
                    )
                    db.conn.commit()
                except Exception as exc:
                    db.conn.rollback()
                    results.append(
                        self._batch_result(
                            operation_id,
                            ok=False,
                            code="state_changed",
                            message=f"流水状态刚刚发生变化，请刷新后重试：{exc}",
                            sell=sell,
                            rebuy=rebuy,
                        )
                    )
                    continue
                self._emit_guadao_runtime_event(
                    operation="manual_external_rebuy_completed",
                    message="用户确认已在其他平台补仓并批量手动完结",
                    operationId=operation_id,
                    rebuyOperationId=int(rebuy["id"]),
                    marketHashName=str(sell["market_hash_name"]),
                    batchId=batch_id,
                    actualRebuyPrice=price,
                    actualRebuyRatio=actual_ratio,
                    steamNetAmount=round(steam_net_amount, 6),
                    source=normalized_source,
                    completedAt=completed_iso,
                    externalOrderRef=safe_external_ref,
                    requestId=request_key,
                    reason=safe_reason,
                )
                results.append(success_result)
        finally:
            db.close()
        success_count = sum(bool(item["ok"]) for item in results)
        return {
            "ok": success_count > 0,
            "batchId": batch_id,
            "requestId": request_key,
            "successCount": success_count,
            "failedCount": len(results) - success_count,
            "results": results,
        }

    def _public_rebuy_attempt(self, row: Any, *, is_current: bool) -> dict[str, Any]:
        note = _read_note(row["note"])
        status = str(row["status"] or "")
        is_failed = status == "failed" or status.endswith("_failed")
        submitted_at = _normalize_timestamp_iso(note.get("c5OrderSubmittedAt"))
        deadline_at = None
        submitted = _parse_iso(submitted_at)
        if (
            submitted is not None
            and status in {"delivery_pending", "completed"}
            and _has_confirmed_c5_order_evidence(note)
        ):
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            deadline_at = (
                submitted.astimezone(timezone.utc) + timedelta(hours=12)
            ).isoformat()
        failure_reason = None
        failure_code = None
        failure_at = None
        if is_failed:
            failure_reason = (
                note.get("c5OrderFailedDesc")
                or note.get("failedReason")
                or note.get("replacementFailedDesc")
                or note.get("c5OrderStatusName")
                or "C5 补仓失败"
            )
            failure_code = (
                note.get("c5OrderFailedCode")
                or note.get("failedCode")
                or note.get("replacementFailedCode")
            )
            failure_at = _normalize_timestamp_iso(
                note.get("c5DeliveryTimedOutAt")
                or note.get("c5OrderCheckedAt")
                or row["completed_at"]
            )
        stage = {
            "pending": "等待补仓价格复查",
            C5_SUBMISSION_UNCONFIRMED_STATUS: "C5 补仓待查证据",
            "delivery_pending": "C5 已购买待收货",
            "completed": "补仓完成",
            "c5_failed": "C5 发货失败",
            "failed": "补仓失败",
        }.get(status, status.replace("_", " ") or "状态未知")
        return {
            "id": int(row["id"]),
            "operationId": f"GD-{int(row['id'])}",
            "status": status,
            "stage": stage,
            "isCurrent": bool(is_current),
            "createdAt": row["created_at"],
            "completedAt": row["completed_at"],
            "expectedPrice": row["expected_price"],
            "actualPrice": row["actual_price"],
            "c5OrderId": note.get("c5OrderId"),
            "c5TradeOrderId": _public_c5_trade_order_id(note),
            "c5OutTradeNo": note.get("c5OutTradeNo"),
            "c5OrderSubmittedAt": submitted_at,
            "c5DeliveryDeadlineAt": deadline_at,
            "failureAt": failure_at,
            "failureCode": failure_code,
            "failureReason": failure_reason,
            "replacementOperationId": safe_int(
                note.get("replacementRebuyOperationId")
            ),
            "replacementForOperationId": safe_int(
                note.get("replacementForRebuyOperationId")
            ),
            "replacementReason": note.get("replacementReason"),
            "replacementMaxPrice": safe_float(note.get("replacementMaxPrice")),
        }

    def _public_operation(
        self,
        row: Any,
        *,
        rebuy: Any | None = None,
        rebuy_attempts: list[Any] | None = None,
        task: Any | None = None,
    ) -> dict[str, Any]:
        note = _read_note(row["note"])
        rebuy_note = _read_note(rebuy["note"]) if rebuy is not None else {}
        operation_type = str(row["operation_type"])
        raw_status = str(rebuy["status"] if rebuy is not None else row["status"])
        if rebuy is not None:
            if raw_status == "pending":
                status, step, stage = "sold", 4, "已卖出待补仓"
            elif raw_status == C5_SUBMISSION_UNCONFIRMED_STATUS:
                status, step, stage = (
                    C5_SUBMISSION_UNCONFIRMED_STATUS,
                    5,
                    "C5 补仓待查证据",
                )
            elif raw_status == "delivery_pending":
                status, step, stage = "delivery_pending", 5, "C5 已购买待收货"
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
            ("manualRebuyRefrozenAt", "人工重设补仓冻结价格与比例"),
            ("manualExternalRebuyCompletedAt", "其他平台补仓手动完结"),
            (
                "c5OrderSubmittedAt",
                "C5 补仓请求已提交"
                if raw_status == C5_SUBMISSION_UNCONFIRMED_STATUS
                else "C5 补仓已下单",
            ),
            ("c5OrderCheckedAt", "C5 发货状态复查"),
            ("c5DeliveryOverdueAt", "C5 已超过12小时，继续查询订单详情"),
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
        if (
            submitted_at is not None
            and raw_status in {"delivery_pending", "completed"}
            and _has_confirmed_c5_order_evidence({**note, **rebuy_note})
        ):
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
            c5_delivery_deadline_at = (
                submitted_at.astimezone(timezone.utc) + timedelta(hours=12)
            ).isoformat()
        attempt_rows = list(rebuy_attempts or ([rebuy] if rebuy is not None else []))
        attempt_rows.sort(key=lambda item: int(item["id"]))
        current_rebuy_id = int(rebuy["id"]) if rebuy is not None else None
        public_rebuy_attempts = [
            self._public_rebuy_attempt(
                attempt,
                is_current=(current_rebuy_id is not None and int(attempt["id"]) == current_rebuy_id),
            )
            for attempt in attempt_rows
        ]
        failed_rebuy_count = sum(
            attempt["status"] == "failed"
            or str(attempt["status"]).endswith("_failed")
            for attempt in public_rebuy_attempts
        )
        pending_attempt_count = sum(
            str(attempt["status"] or "") == "pending" for attempt in attempt_rows
        )
        has_terminal_or_delivery_attempt = any(
            str(attempt["status"] or "") in {"delivery_pending", "completed"}
            for attempt in attempt_rows
        )
        batch_block_reason = None
        if status != "sold":
            batch_block_reason = "仅已卖出待补仓流水允许批量操作"
        elif rebuy is None or pending_attempt_count != 1:
            batch_block_reason = "待补仓子流水不是唯一当前流水"
        elif has_terminal_or_delivery_attempt:
            batch_block_reason = "存在发货确认中或已完成的补仓流水"
        elif self._rebuy_has_remote_order_evidence(rebuy_note):
            batch_block_reason = "存在 C5 订单证据，必须先确认远端终态"
        elif task is not None and str(task["status"] or "") == "running":
            batch_block_reason = "补仓任务正在执行"
        current_rebuy_ratio = safe_float(
            rebuy_note.get("currentRebuyRatio")
            or note.get("currentRebuyRatio")
            or rebuy_note.get("maxRebuyRatioAtOpen")
            or note.get("maxRebuyRatioAtOpen")
            or note.get("listingRatioAtOpen")
        )
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
            # Keep the C5 asset-order id, trade-order id and client-generated
            # outTradeNo separate.  They have different meanings when
            # diagnosing an HTTP-200 response that did not create an order.
            "c5OrderId": rebuy_note.get("c5OrderId") or note.get("c5OrderId"),
            "c5TradeOrderId": (
                _public_c5_trade_order_id(rebuy_note)
                or _public_c5_trade_order_id(note)
            ),
            "c5OutTradeNo": (
                rebuy_note.get("c5OutTradeNo") or note.get("c5OutTradeNo")
            ),
            "steamAccountId": note.get("steamAccountId"),
            "steamAccountName": account_name,
            "accountName": account_name,
            "steamId": note.get("steamId64"),
            "listingRatioAtOpen": note.get("listingRatioAtOpen"),
            "maxRebuyRatioAtOpen": note.get("maxRebuyRatioAtOpen"),
            "currentRebuyRatio": current_rebuy_ratio,
            "frozenRebuyPrice": (
                safe_float(rebuy_note.get("manualRebuyRefrozenPrice"))
                or safe_float(note.get("manualRebuyRefrozenPrice"))
                or (safe_float(rebuy["expected_price"]) if rebuy is not None else None)
                or safe_float(note.get("rebuyPrice"))
            ),
            "manualRebuyRefrozenAt": (
                rebuy_note.get("manualRebuyRefrozenAt")
                or note.get("manualRebuyRefrozenAt")
            ),
            "manualRebuyRefreezeHistory": (
                rebuy_note.get("manualRebuyRefreezeHistory")
                or note.get("manualRebuyRefreezeHistory")
                or []
            ),
            "manualExternalRebuySource": rebuy_note.get("manualExternalRebuySource"),
            "actualRebuyRatio": safe_float(
                rebuy_note.get("manualRebuyRefrozenRatio")
                if raw_status == "completed"
                else None
            ),
            "guadaoMaxListingRatioAtOpen": note.get("guadaoMaxListingRatioAtOpen"),
            "ratioRuleSource": note.get("guadaoRatioRuleSource"),
            "ratioRuleId": note.get("guadaoRatioRuleId"),
            "ratioRuleVersion": note.get("guadaoRatioRuleVersion"),
            "steamListPrice": note.get("steamListPrice"),
            "steamNetAmount": self._rebuy_steam_net_amount(note, rebuy_note),
            "steamSoldAt": _normalize_timestamp_iso(
                rebuy_note.get("steamSoldAt") or note.get("steamSoldAt")
            ),
            "c5OrderSubmittedAt": c5_order_submitted_at,
            "c5DeliveryDeadlineAt": c5_delivery_deadline_at,
            "rebuyAttemptCount": len(public_rebuy_attempts),
            "failedRebuyCount": failed_rebuy_count,
            "hasPreviousRebuyFailure": failed_rebuy_count > 0,
            "rebuyAttempts": public_rebuy_attempts,
            "rebuyOperationId": int(rebuy["id"]) if rebuy is not None else None,
            "batchActionEligible": batch_block_reason is None,
            "batchActionBlockReason": batch_block_reason,
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
              AND o.status IN (
                  'manual_required','listing_failed','failed',
                  'c5_submission_unconfirmed'
              )
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
            if (
                raw_status == C5_SUBMISSION_UNCONFIRMED_STATUS
                and not note.get("c5SubmissionCoverageAlertAt")
            ):
                continue
            reason = str(
                note.get("manualReviewReason")
                or note.get("c5SubmissionReconcileAlertCode")
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
                or note.get("c5SubmissionCoverageAlertAt")
                or note.get("staleListedManualRequiredAt")
                or row["created_at"]
                or ""
            ) or None
            last_seen_at = str(
                note.get("lastCheckedAt")
                or note.get("c5SubmissionLastCheckedAt")
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
                    "rebuyReferenceFloor": safe_float(rule.get("rebuyReferenceFloor")),
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
                    "itemScope": normalize_guadao_item_scope(config.guadao_item_scope),
                    "autoListing": bool(config.auto_list_enabled),
                    "autoRebuy": bool(config.auto_rebuy_enabled),
                    "lastModifiedAt": latest_config_at,
                },
                "specialRules": special_rules,
                "timePolicy": {
                    "scanMinutes": float(schedule.get("scanIntervalSeconds") or 300.0) / 60.0,
                    "steamSyncMaxStartLagSeconds": float(
                        schedule.get("steamSyncMaxStartLagSeconds") or 60.0
                    ),
                    "staleListedCheckHours": float(
                        schedule.get("staleListedCheckIntervalSeconds") or 86400.0
                    )
                    / 3600.0,
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
                "guadaoItemScope": normalize_guadao_item_scope(config.guadao_item_scope),
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
                "guadaoItemScope": normalize_guadao_item_scope(config.guadao_item_scope),
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
                if "itemScope" in nested_global:
                    payload["guadaoItemScope"] = nested_global.get("itemScope")
            if isinstance(nested_rules, list):
                payload["specialCaseRatioRules"] = [
                    {
                        "ruleId": rule.get("id"),
                        "version": rule.get("version"),
                        "marketHashName": rule.get("marketHashName"),
                        "nameCn": rule.get("displayName"),
                        "maxListingRatio": float(rule.get("maxRatioPct")) / 100.0,
                        "rebuyReferenceFloor": rule.get("rebuyReferenceFloor"),
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
                    "steamSyncMaxStartLagSeconds": float(
                        nested_time.get(
                            "steamSyncMaxStartLagSeconds",
                            current_schedule["steamSyncMaxStartLagSeconds"],
                        )
                    ),
                    "staleListedCheckIntervalSeconds": float(
                        nested_time.get(
                            "staleListedCheckHours",
                            float(current_schedule["staleListedCheckIntervalSeconds"])
                            / 3600.0,
                        )
                    )
                    * 3600.0,
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
            if "guadaoItemScope" in payload:
                config.guadao_item_scope = normalize_guadao_item_scope(
                    payload["guadaoItemScope"]
                )
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
                    raise ValueError("老挂单比例最多可放宽 0 到 20 个百分点")
                config.stale_listed_max_ratio_tolerance_pct = value
            if "specialCaseRatioRules" in payload:
                config.guadao_special_ratio_rules = self._validate_special_ratio_rules(
                    payload["specialCaseRatioRules"],
                    global_ratio=config.guadao_max_listing_ratio,
                    confirm_high=bool(payload.get("confirmHighRatio")),
                )
            if "taskSchedule" in payload:
                validated_schedule = self._validate_task_schedule(payload["taskSchedule"])
                config.guadao_task_schedule = validated_schedule
                payload["taskSchedule"] = validated_schedule
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
                if ratio <= 0 or ratio > 0.80:
                    raise ValueError("特殊箱子比例必须大于 0 且不超过 80%")
                raw_floor = raw.get("rebuyReferenceFloor")
                rebuy_reference_floor = None
                if raw_floor not in (None, ""):
                    rebuy_reference_floor = round(float(raw_floor), 2)
                    if rebuy_reference_floor <= 0:
                        raise ValueError("特殊箱子开单参考价下限必须大于 0")
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
                        or safe_float(existing.get("rebuyReferenceFloor"))
                        != rebuy_reference_floor
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
                        "rebuyReferenceFloor": rebuy_reference_floor,
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
        # Account synchronisation is projected from operation timers. Drop
        # the retired periodic interval when old clients or configs send it.
        result.pop("steamSyncIntervalSeconds", None)
        if float(result["scanIntervalSeconds"]) < 60:
            raise ValueError("完整扫描间隔不得低于 1 分钟")
        if float(result["steamSyncMaxStartLagSeconds"]) < 1:
            raise ValueError("Steam 同步最长开始等待不得低于 1 秒")
        if float(result["staleListedCheckIntervalSeconds"]) < 3600:
            raise ValueError("查找超过 48 小时挂单的间隔不得低于 1 小时")
        action = [float(value) for value in result.get("actionConfirmationDelaysSeconds") or []]
        if not action or min(action) < 2 or action != sorted(action):
            raise ValueError("动作确认间隔不得低于 2 秒且必须非递减")
        sale = [float(value) for value in result.get("saleEvidenceDelaysSeconds") or []]
        if not sale or min(sale) < 0 or sale != sorted(sale):
            raise ValueError("挂单消失后的卖出检查间隔不能为负数，并且必须从短到长排列")
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
        result["steamSyncMaxStartLagSeconds"] = float(result["steamSyncMaxStartLagSeconds"])
        result["staleListedCheckIntervalSeconds"] = float(
            result["staleListedCheckIntervalSeconds"]
        )
        result["actionConfirmationDelaysSeconds"] = action
        result["saleEvidenceDelaysSeconds"] = sale
        return result

    def search_items(
        self,
        query: str,
        *,
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            safe_limit = max(1, min(int(limit), 100))
            safe_offset = max(0, int(offset))
            keyword = str(query or "").strip()
            rows = db.search_items(keyword, limit=None)
            matching_rows = [
                row for row in rows if _catalog_item_matches_guadao_case_semantics(row)
            ]
            total = len(matching_rows)
            page_rows = matching_rows[safe_offset : safe_offset + safe_limit]
            next_offset = safe_offset + len(page_rows)
            return {
                "items": [
                    {
                        "marketHashName": row["market_hash_name"],
                        "nameCn": row["name_cn"],
                        "displayName": row["name_cn"],
                        "name": row["name_cn"],
                        "c5ItemId": row["c5_item_id"],
                    }
                    for row in page_rows
                ],
                "pagination": {
                    "offset": safe_offset,
                    "limit": safe_limit,
                    "total": total,
                    "hasMore": next_offset < total,
                    "nextOffset": next_offset if next_offset < total else None,
                },
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
