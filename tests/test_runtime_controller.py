from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.accounts.store import Account
from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import CatalogItem, OP_REBUY_C5, OP_SELL_STEAM, StrategyConfig
from cs2_assistant.services.executor_engine import (
    C5_DELIVERY_FAILED,
    C5_DELIVERY_STATUS_KEY,
    ExecutionEngine,
    _read_note,
)
from cs2_assistant.services.runtime_controller import (
    RUNTIME_GUADAO,
    RUNTIME_PROFIT_TRADE,
    TASK_C5_DELIVERY_CONFIRM,
    TASK_GUADAO_SCAN,
    TASK_PROFIT_CYCLE,
    TASK_REBUY_ATTEMPT,
    TASK_STEAM_ACCOUNT_SYNC,
    TASK_STEAM_LISTING_CONFIRM,
    TASK_STEAM_SALE_EVIDENCE,
    UnifiedRuntimeController,
)
from cs2_assistant.utils import utc_now_iso


class FakeAccountStore:
    def __init__(self, accounts: list[Account]) -> None:
        self.accounts = accounts

    def list_accounts(self) -> list[Account]:
        return list(self.accounts)

    def get_account(self, account_id_or_name: str) -> Account | None:
        lookup = str(account_id_or_name)
        return next(
            (
                account
                for account in self.accounts
                if account.id == lookup or account.name == lookup
            ),
            None,
        )


class RuntimeControllerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            db_path=root / "assistant.db",
            steamdt_base_path=root / "steamdt.json",
            c5_api_key="test-only-c5-key",
        )
        self.accounts = [
            Account(
                id=f"account-{index}",
                name=f"steam-{index}",
                username=f"user-{index}",
                password="password",
                steam_id64=f"7656119800000000{index}",
                cookies=f"session-{index}",
            )
            for index in range(1, 6)
        ]
        self.controller = UnifiedRuntimeController(self.settings, poll_seconds=0.2)
        self.controller.account_store = FakeAccountStore(self.accounts)
        self.controller._initialize()

    def tearDown(self) -> None:
        self.controller.stop(timeout=0)
        self.temp_dir.cleanup()

    def _open_db(self) -> Database:
        db = Database(self.settings.db_path)
        db.initialize()
        return db

    def _confirm_migration(self) -> None:
        self.controller.confirm_migration()

    def _mark_all_cookies_valid(self) -> None:
        db = self._open_db()
        try:
            now = utc_now_iso()
            for account in self.accounts:
                db.upsert_steam_cookie_health(
                    account.id,
                    account_name=account.name,
                    steam_id=account.steam_id64,
                    status="valid",
                    batch_id="batch-ready",
                    failure_count=0,
                    last_validated_at=now,
                )
        finally:
            db.close()

    def _set_runtime(
        self,
        executor_key: str,
        *,
        enabled: bool,
        runtime_status: str,
    ) -> None:
        db = self._open_db()
        try:
            current = db.get_executor_runtime_state(executor_key)
            payload = json.loads(str(current["payload_json"] or "{}"))
            db.upsert_executor_runtime_state(
                executor_key,
                enabled=enabled,
                runtime_status=runtime_status,
                migration_hold=False,
                gate_reason=None,
                heartbeat_at=utc_now_iso(),
                payload=payload,
            )
        finally:
            db.close()

    def _move_all_tasks_to_future(self, *, except_key: str | None = None) -> None:
        db = self._open_db()
        try:
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            for task in db.list_scheduled_tasks(limit=1000):
                if str(task["task_key"]) == except_key:
                    continue
                db.reschedule_scheduled_task(
                    str(task["task_key"]),
                    next_attempt_at=future,
                )
        finally:
            db.close()

    def test_migration_hold_blocks_dispatch_until_explicit_confirmation(self) -> None:
        db = self._open_db()
        try:
            states = db.list_executor_runtime_states()
            self.assertEqual(2, len(states))
            self.assertTrue(all(not bool(row["enabled"]) for row in states))
            self.assertTrue(all(bool(row["migration_hold"]) for row in states))
            self.assertTrue(db.list_scheduled_tasks(limit=100))
        finally:
            db.close()

        with patch.object(self.controller, "_dispatch_task") as dispatch:
            result = self.controller.tick(max_tasks=20)
        dispatch.assert_not_called()
        self.assertTrue(result["processed"])

        db = self._open_db()
        try:
            tasks = db.list_scheduled_tasks(limit=100)
            self.assertTrue(all(row["status"] in {"pending", "retry"} for row in tasks))
            self.assertTrue(all(row["last_error"] == "migration_hold" for row in tasks))
        finally:
            db.close()

        confirmed = self.controller.confirm_migration()
        self.assertTrue(confirmed["ok"])
        db = self._open_db()
        try:
            states = db.list_executor_runtime_states()
            self.assertTrue(all(not bool(row["migration_hold"]) for row in states))
            self.assertTrue(all(not bool(row["enabled"]) for row in states))
        finally:
            db.close()

    def test_seed_tasks_keeps_old_delivery_pending_on_next_attempt_schedule(self) -> None:
        db = self._open_db()
        try:
            submitted = datetime.now(timezone.utc) - timedelta(days=8)
            op_id = db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.60,
                note=json.dumps(
                    {
                        "c5OrderId": "OLD-DELIVERY",
                        "c5OutTradeNo": "OUT-OLD-DELIVERY",
                        "c5OrderSubmittedAt": submitted.isoformat(),
                        C5_DELIVERY_STATUS_KEY: "pending",
                    }
                ),
            )
            db.update_pool_operation(op_id, status="delivery_pending", actual_price=1.58)
            db.conn.execute(
                "UPDATE pool_operations SET created_at = ?, completed_at = ? WHERE id = ?",
                (submitted.isoformat(), submitted.isoformat(), op_id),
            )
            db.conn.commit()

            self.controller._seed_tasks(db)

            task = db.get_scheduled_task(f"delivery:{op_id}")
            self.assertIsNotNone(task)
            self.assertEqual(TASK_C5_DELIVERY_CONFIRM, task["task_type"])
            self.assertIsNotNone(task["next_attempt_at"])
        finally:
            db.close()

    def test_seed_tasks_creates_operation_level_steam_timers_and_projects_next_attempt(self) -> None:
        account = self.accounts[0]
        created_at = datetime.now(timezone.utc).replace(microsecond=0)
        config = StrategyConfig(
            guadao_task_schedule={
                "actionConfirmationDelaysSeconds": [10.0, 20.0, 40.0],
                "saleEvidenceDelaysSeconds": [0.0, 60.0, 180.0, 600.0],
            }
        )
        db = self._open_db()
        try:
            pending_id = db.add_pool_operation(
                market_hash_name="Pending Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-pending-timer",
                note=json.dumps(
                    {
                        "steamAccountId": account.id,
                        "steamId64": account.steam_id64,
                    }
                ),
            )
            listed_id = db.add_pool_operation(
                market_hash_name="Listed Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-listed-timer",
                note=json.dumps(
                    {
                        "steamAccountId": account.id,
                        "steamId64": account.steam_id64,
                    }
                ),
            )
            db.update_pool_operation(pending_id, status="listing_pending")
            db.update_pool_operation(listed_id, status="listed")
            db.conn.execute(
                "UPDATE pool_operations SET created_at = ? WHERE id IN (?, ?)",
                (created_at.isoformat(), pending_id, listed_id),
            )
            db.conn.commit()

            with patch(
                "cs2_assistant.services.runtime_controller.load_strategy_config",
                return_value=config,
            ):
                self.controller._seed_tasks(db)

            confirm = db.get_scheduled_task(f"listing-confirm:{pending_id}")
            evidence = db.get_scheduled_task(f"sale-evidence:{listed_id}")
            self.assertIsNotNone(confirm)
            self.assertIsNotNone(evidence)
            self.assertEqual(TASK_STEAM_LISTING_CONFIRM, confirm["task_type"])
            self.assertEqual(TASK_STEAM_SALE_EVIDENCE, evidence["task_type"])
            self.assertEqual("waiting", confirm["status"])
            self.assertEqual("waiting", evidence["status"])
            self.assertEqual(
                created_at + timedelta(seconds=10),
                datetime.fromisoformat(str(confirm["next_attempt_at"])),
            )
            self.assertEqual(
                created_at,
                datetime.fromisoformat(str(evidence["next_attempt_at"])),
            )
        finally:
            db.close()

        projected = self.controller.operations(page=1, page_size=50)["items"]
        by_id = {row["id"]: row for row in projected}
        self.assertEqual(confirm["next_attempt_at"], by_id[pending_id]["nextAttemptAt"])
        self.assertEqual(evidence["next_attempt_at"], by_id[listed_id]["nextAttemptAt"])

    def test_successful_account_sync_advances_only_due_operation_timer_tier(self) -> None:
        account = self.accounts[0]
        config = StrategyConfig(
            guadao_task_schedule={
                "actionConfirmationDelaysSeconds": [10.0, 20.0, 40.0],
                "saleEvidenceDelaysSeconds": [0.0, 60.0, 180.0, 600.0],
            }
        )
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Tiered Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-tiered",
                note=json.dumps(
                    {
                        "steamAccountId": account.id,
                        "steamId64": account.steam_id64,
                    }
                ),
            )
            db.update_pool_operation(op_id, status="listed")
            due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.upsert_scheduled_task(
                f"sale-evidence:{op_id}",
                source=RUNTIME_GUADAO,
                task_type=TASK_STEAM_SALE_EVIDENCE,
                account_id=account.id,
                operation_id=op_id,
                next_attempt_at=due_at,
                payload={"tierIndex": 0},
                status="waiting",
            )
            due = [
                dict(db.get_scheduled_task(f"sale-evidence:{op_id}"))
            ]
            before = datetime.now(timezone.utc)
            with patch(
                "cs2_assistant.services.runtime_controller.load_strategy_config",
                return_value=config,
            ):
                self.controller._advance_due_steam_operation_tasks(
                    db,
                    due,
                    result={"ok": True},
                    error=None,
                )
            advanced = db.get_scheduled_task(f"sale-evidence:{op_id}")
            payload = json.loads(str(advanced["payload_json"]))
            self.assertEqual(1, payload["tierIndex"])
            next_at = datetime.fromisoformat(str(advanced["next_attempt_at"]))
            self.assertGreaterEqual(next_at, before + timedelta(seconds=59))
        finally:
            db.close()

    def test_unverified_listing_stays_on_sale_evidence_timer_without_mobile_confirmation(self) -> None:
        account = self.accounts[0]
        config = StrategyConfig(
            guadao_task_schedule={
                "actionConfirmationDelaysSeconds": [10.0, 60.0, 300.0],
                "saleEvidenceDelaysSeconds": [0.0, 60.0, 600.0],
            }
        )
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Delayed History Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-delayed-history",
                note=json.dumps(
                    {
                        "listingId": "listing-delayed-history",
                        "steamAccountId": account.id,
                        "steamId64": account.steam_id64,
                    }
                ),
            )
            db.update_pool_operation(op_id, status="listed")
            with patch(
                "cs2_assistant.services.runtime_controller.load_strategy_config",
                return_value=config,
            ):
                self.controller._seed_tasks(db)
            evidence = db.get_scheduled_task(f"sale-evidence:{op_id}")
            self.assertIsNotNone(evidence)

            note = json.loads(
                str(
                    db.conn.execute(
                        "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
                    ).fetchone()["note"]
                )
            )
            note["confirmationStatus"] = "listing_missing_unverified"
            note["listingPendingAt"] = utc_now_iso()
            db.update_pool_operation(
                op_id,
                status="listing_pending",
                note=json.dumps(note),
            )

            with patch(
                "cs2_assistant.services.runtime_controller.load_strategy_config",
                return_value=config,
            ):
                self.controller._advance_due_steam_operation_tasks(
                    db,
                    [dict(evidence)],
                    result={"ok": True},
                    error=None,
                )
                self.controller._seed_tasks(db)

            evidence_after = db.get_scheduled_task(f"sale-evidence:{op_id}")
            self.assertIsNotNone(evidence_after)
            self.assertEqual(TASK_STEAM_SALE_EVIDENCE, evidence_after["task_type"])
            self.assertEqual("waiting", evidence_after["status"])
            self.assertEqual(op_id, int(evidence_after["operation_id"]))
            self.assertIsNone(db.get_scheduled_task(f"listing-confirm:{op_id}"))
        finally:
            db.close()

    def test_executor_switches_are_independent_and_disabled_state_keeps_closure_work(self) -> None:
        self._confirm_migration()
        guadao = self.controller.toggle_executor(RUNTIME_GUADAO, True)
        self.assertTrue(guadao["enabled"])
        self.assertEqual("preparing", guadao["runtimeStatus"])

        db = self._open_db()
        try:
            profit = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
            self.assertFalse(bool(profit["enabled"]))
            sell_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-1",
                note=json.dumps(
                    {
                        "steamAccountId": self.accounts[0].id,
                        "steamId64": self.accounts[0].steam_id64,
                    }
                ),
            )
            db.update_pool_operation(sell_id, status="listed")
            db.add_profit_trade(
                trade_no="PT-runtime-closing",
                market_hash_name="AK-47 | Test",
                status="steam_bought",
                step_key="steam_bought",
                step_index=3,
            )
        finally:
            db.close()

        guadao_off = self.controller.toggle_executor(RUNTIME_GUADAO, False)
        profit_off = self.controller.toggle_executor(RUNTIME_PROFIT_TRADE, False)
        self.assertEqual("closing_only", guadao_off["runtimeStatus"])
        self.assertEqual("closing_only", profit_off["runtimeStatus"])

        task = {
            "task_type": TASK_PROFIT_CYCLE,
            "task_key": TASK_PROFIT_CYCLE,
            "source": RUNTIME_PROFIT_TRADE,
            "account_id": None,
            "operation_id": None,
        }
        with (
            patch.object(
                self.controller,
                "_run_profit_closure_once",
                return_value={"ok": True, "settled": [1]},
            ) as closure,
            patch(
                "cs2_assistant.services.runtime_controller.run_profit_trade_once"
            ) as automatic,
        ):
            result = self.controller._dispatch_task(task, enabled=False)
        closure.assert_called_once_with()
        automatic.assert_not_called()
        self.assertEqual([1], result["settled"])

    def test_disabled_guadao_scan_is_not_dispatched(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=False, runtime_status="stopped")
        self._move_all_tasks_to_future(except_key=TASK_GUADAO_SCAN)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(
                TASK_GUADAO_SCAN,
                next_attempt_at=utc_now_iso(),
            )
            task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )[0]
        finally:
            db.close()
        with patch.object(self.controller, "_dispatch_task") as dispatch:
            self.controller._execute_claimed_task(
                task,
                gate={"status": "ready", "validCount": 5, "totalCount": 5},
            )
        dispatch.assert_not_called()
        db = self._open_db()
        try:
            scan = db.get_scheduled_task(TASK_GUADAO_SCAN)
            self.assertEqual("pending", scan["status"])
            self.assertEqual("executor_disabled", scan["last_error"])
        finally:
            db.close()

    def test_profit_dispatch_guard_observes_disable_after_task_was_claimed(self) -> None:
        self._set_runtime(
            RUNTIME_PROFIT_TRADE,
            enabled=True,
            runtime_status="running",
        )
        observed: list[bool] = []

        def fake_run(_settings: Settings, *, new_action_guard: object) -> object:
            self._set_runtime(
                RUNTIME_PROFIT_TRADE,
                enabled=False,
                runtime_status="closing_only",
            )
            observed.append(bool(new_action_guard()))
            return SimpleNamespace(to_dict=lambda: {"ok": True})

        with patch(
            "cs2_assistant.services.runtime_controller.run_profit_trade_once",
            side_effect=fake_run,
        ):
            result = self.controller._dispatch_task(
                {"task_type": TASK_PROFIT_CYCLE},
                enabled=True,
            )

        self.assertEqual({"ok": True}, result)
        self.assertEqual([False], observed)

    def test_guadao_scan_guard_observes_disable_after_task_was_claimed(self) -> None:
        self._set_runtime(
            RUNTIME_GUADAO,
            enabled=True,
            runtime_status="running",
        )
        observed: list[bool] = []
        owner = self

        class FakeEngine:
            def __init__(
                self,
                _settings: Settings,
                *,
                new_action_guard: object,
            ) -> None:
                owner._set_runtime(
                    RUNTIME_GUADAO,
                    enabled=False,
                    runtime_status="closing_only",
                )
                observed.append(bool(new_action_guard()))

            def run_guadao_scan_task(self) -> dict[str, object]:
                return {"ok": True}

            def close(self) -> None:
                return None

        with patch(
            "cs2_assistant.services.runtime_controller.ExecutionEngine",
            FakeEngine,
        ):
            result = self.controller._dispatch_task(
                {
                    "task_key": TASK_GUADAO_SCAN,
                    "task_type": TASK_GUADAO_SCAN,
                    "account_id": None,
                    "operation_id": None,
                },
                enabled=True,
            )

        self.assertEqual({"ok": True}, result)
        self.assertEqual([False], observed)

    def test_disabled_guadao_still_dispatches_account_sync_for_existing_listing(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=False, runtime_status="closing_only")
        account = self.accounts[0]
        sync_key = f"steam-sync:{account.id}"
        db = self._open_db()
        try:
            sell_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-closing",
                note=json.dumps(
                    {
                        "steamAccountId": account.id,
                        "steamId64": account.steam_id64,
                    }
                ),
            )
            db.update_pool_operation(sell_id, status="listed")
        finally:
            db.close()
        self._move_all_tasks_to_future(except_key=sync_key)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(sync_key, next_attempt_at=utc_now_iso())
            task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )[0]
        finally:
            db.close()
        with patch.object(
            self.controller,
            "_dispatch_task",
            return_value={"ok": True, "sold": 0},
        ) as dispatch:
            self.controller._execute_claimed_task(
                task,
                gate={"status": "ready", "validCount": 5, "totalCount": 5},
            )
        dispatch.assert_called_once()
        self.assertFalse(dispatch.call_args.kwargs["enabled"])
        self.assertEqual(TASK_STEAM_ACCOUNT_SYNC, dispatch.call_args.args[0]["task_type"])

    def test_cookie_gate_requires_five_of_five_during_startup(self) -> None:
        self._confirm_migration()
        self.controller.toggle_executor(RUNTIME_GUADAO, True)

        def mark_refreshed(
            db: Database,
            account: Account,
            row: object,
            *,
            batch_id: str,
        ) -> None:
            db.upsert_steam_cookie_health(
                account.id,
                account_name=account.name,
                steam_id=account.steam_id64,
                status="valid",
                batch_id=batch_id,
                failure_count=0,
                last_validated_at=utc_now_iso(),
            )

        db = self._open_db()
        try:
            with patch.object(
                self.controller,
                "_refresh_cookie_account",
                side_effect=mark_refreshed,
            ) as refresh:
                snapshots = [self.controller._cookie_gate_tick(db) for _ in range(5)]
            self.assertEqual(5, refresh.call_count)
            self.assertEqual([1, 2, 3, 4, 5], [item["validCount"] for item in snapshots])
            self.assertEqual("ready", snapshots[-1]["status"])
            runtime = db.get_executor_runtime_state(RUNTIME_GUADAO)
            self.assertEqual("running", runtime["runtime_status"])
        finally:
            db.close()

    def test_runtime_cookie_loss_pauses_only_the_failed_account(self) -> None:
        """After startup, 4/5 must be degraded running, not a global startup gate."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        db = self._open_db()
        try:
            failed = self.accounts[2]
            db.upsert_steam_cookie_health(
                failed.id,
                account_name=failed.name,
                steam_id=failed.steam_id64,
                status="invalid",
                failure_count=1,
                last_error="401",
                next_retry_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            )
            snapshot = self.controller._cookie_gate_tick(db)
            runtime = db.get_executor_runtime_state(RUNTIME_GUADAO)
        finally:
            db.close()

        self.assertEqual(4, snapshot["validCount"])
        self.assertEqual("degraded", snapshot["status"])
        self.assertEqual("running", runtime["runtime_status"])
        self.assertEqual("degraded", self.controller.dashboard()["cookieGate"]["status"])

        # Degraded mode still allows global scan work, while the account-level
        # health check remains authoritative for each account sync task.
        self._move_all_tasks_to_future(except_key=TASK_GUADAO_SCAN)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(TASK_GUADAO_SCAN, next_attempt_at=utc_now_iso())
            scan_task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )[0]
        finally:
            db.close()
        with patch.object(
            self.controller,
            "_dispatch_task",
            return_value={"ok": True, "listed": 0},
        ) as scan_dispatch:
            self.controller._execute_claimed_task(scan_task, gate=snapshot)
        scan_dispatch.assert_called_once()

        failed_sync_key = f"steam-sync:{failed.id}"
        self._move_all_tasks_to_future(except_key=failed_sync_key)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(failed_sync_key, next_attempt_at=utc_now_iso())
            failed_task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )[0]
        finally:
            db.close()
        with patch.object(self.controller, "_dispatch_task") as failed_dispatch:
            self.controller._execute_claimed_task(failed_task, gate=snapshot)
        failed_dispatch.assert_not_called()
        db = self._open_db()
        try:
            failed_task_row = db.get_scheduled_task(failed_sync_key)
            self.assertEqual("account_cookie_not_valid", failed_task_row["last_error"])
        finally:
            db.close()

        valid = self.accounts[0]
        valid_sync_key = f"steam-sync:{valid.id}"
        self._move_all_tasks_to_future(except_key=valid_sync_key)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(valid_sync_key, next_attempt_at=utc_now_iso())
            valid_task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )[0]
        finally:
            db.close()
        with patch.object(
            self.controller,
            "_dispatch_task",
            return_value={"ok": True, "sold": 0},
        ) as valid_dispatch:
            self.controller._execute_claimed_task(valid_task, gate=snapshot)
        valid_dispatch.assert_called_once()

    def test_seed_tasks_preserves_future_next_attempt_at_and_active_lease(self) -> None:
        self._confirm_migration()
        future = (datetime.now(timezone.utc) + timedelta(hours=6)).replace(microsecond=0).isoformat()
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(TASK_GUADAO_SCAN, next_attempt_at=future)
            self.controller._seed_tasks(db)
            seeded = db.get_scheduled_task(TASK_GUADAO_SCAN)
            self.assertEqual(future, seeded["next_attempt_at"])
            self.assertEqual(0, seeded["attempt_count"])

            db.reschedule_scheduled_task(TASK_GUADAO_SCAN, next_attempt_at=utc_now_iso())
            claimed = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
                lease_seconds=180,
            )[0]
            self.controller._seed_tasks(db)
            protected = db.get_scheduled_task(str(claimed["task_key"]))
            self.assertEqual("running", protected["status"])
            self.assertEqual(self.controller.worker_id, protected["lease_owner"])
            self.assertEqual(1, protected["attempt_count"])
        finally:
            db.close()

    def test_c5_delivery_timeout_at_24h_fails_once_and_creates_one_replacement(self) -> None:
        engine = ExecutionEngine(self.settings)
        try:
            engine.config.dry_run = False
            engine.config.auto_rebuy_enabled = True
            submitted = datetime.now(timezone.utc) - timedelta(hours=25)
            note = {
                "c5OutTradeNo": "OUT-24H",
                "c5OrderId": "ORDER-24H",
                C5_DELIVERY_STATUS_KEY: "pending",
                "c5OrderSubmittedAt": submitted.isoformat(),
                "c5DeliveryDeadlineAt": (submitted + timedelta(hours=24)).isoformat(),
                "sourceSellOperationId": 100,
                "steamListPrice": 2.95,
                "listingRatioAtOpen": 0.68,
                "maxRebuyRatioAtOpen": 0.69,
                "guadaoMaxListingRatioAtOpen": 0.69,
                "steamNetFactorAtOpen": 0.85,
                "guadaoRatioRuleSource": "global",
                "guadaoRatioRuleId": "global",
                "guadaoRatioRuleVersion": 1,
            }
            op_id = engine.db.add_pool_operation(
                market_hash_name="Dreams & Nightmares Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=7.0,
                note=json.dumps(note),
            )
            engine.db.update_pool_operation(
                op_id,
                status="delivery_pending",
                actual_price=6.95,
            )

            def no_order_detail(op: object, current_note: dict[str, object]):
                return None, None, current_note

            with patch.object(
                engine,
                "_fetch_c5_buyer_order_detail",
                side_effect=no_order_detail,
            ):
                first = engine.run_guadao_delivery_confirmation_task(op_id)
                second = engine.run_guadao_delivery_confirmation_task(op_id)

            original = engine.db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            original_note = _read_note(original["note"])
            replacements = engine.db.conn.execute(
                """
                SELECT * FROM pool_operations
                WHERE operation_type = ? AND status = 'pending' AND id != ?
                """,
                (OP_REBUY_C5, op_id),
            ).fetchall()
            self.assertEqual(C5_DELIVERY_FAILED, original["status"])
            self.assertEqual(C5_DELIVERY_FAILED, original_note[C5_DELIVERY_STATUS_KEY])
            self.assertEqual("delivery_timeout_24h", original_note["c5OrderFailedCode"])
            self.assertEqual(1, len(replacements))
            replacement = replacements[0]
            replacement_note = _read_note(replacement["note"])
            self.assertEqual(6.95, replacement["expected_price"])
            self.assertEqual(6.95, replacement_note["replacementMaxPrice"])
            self.assertEqual(0.69, replacement_note["maxRebuyRatioAtOpen"])
            self.assertEqual(op_id, replacement_note["replacementForRebuyOperationId"])
            self.assertEqual(1, first["replacements"])
            self.assertEqual(0, second["replacements"])
        finally:
            engine.close()

    def test_same_tick_sold_result_seeds_and_claims_rebuy_once(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        account_sync_key = f"steam-sync:{self.accounts[0].id}"
        self._move_all_tasks_to_future(except_key=account_sync_key)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(account_sync_key, next_attempt_at=utc_now_iso())
        finally:
            db.close()

        dispatched: list[str] = []
        created_rebuy: list[int] = []

        def dispatch(task: object, *, enabled: bool) -> dict[str, object]:
            task_type = str(task["task_type"])
            dispatched.append(task_type)
            db = self._open_db()
            try:
                if task_type == TASK_STEAM_ACCOUNT_SYNC:
                    rebuy_id = db.add_pool_operation(
                        market_hash_name="Kilowatt Case",
                        strategy="guadao",
                        operation_type=OP_REBUY_C5,
                        expected_price=1.0,
                        note=json.dumps(
                            {
                                "sourceSellOperationId": 77,
                                "steamAccountId": self.accounts[0].id,
                            }
                        ),
                    )
                    created_rebuy.append(rebuy_id)
                    return {"ok": True, "sold": 1}
                if task_type == TASK_REBUY_ATTEMPT:
                    self.assertEqual(created_rebuy[0], int(task["operation_id"]))
                    db.update_pool_operation(int(task["operation_id"]), status="completed")
                    return {"ok": True, "rebought": 1}
                self.fail(f"unexpected task dispatched: {task_type}")
            finally:
                db.close()

        with patch.object(self.controller, "_dispatch_task", side_effect=dispatch):
            result = self.controller.tick(max_tasks=2)

        self.assertEqual(
            [TASK_STEAM_ACCOUNT_SYNC, TASK_REBUY_ATTEMPT],
            dispatched,
        )
        self.assertEqual(2, len(result["processed"]))
        db = self._open_db()
        try:
            rebuy_task = db.get_scheduled_task(f"rebuy:{created_rebuy[0]}")
            self.assertEqual("completed", rebuy_task["status"])
            self.assertEqual(1, rebuy_task["attempt_count"])
        finally:
            db.close()

    def test_settings_nested_percentages_and_time_policy_round_trip(self) -> None:
        holder = {"config": StrategyConfig()}

        def load_config(settings: Settings) -> StrategyConfig:
            return holder["config"]

        def save_config(settings: Settings, config: StrategyConfig) -> None:
            holder["config"] = config

        with (
            patch(
                "cs2_assistant.services.runtime_controller.load_strategy_config",
                side_effect=load_config,
            ),
            patch(
                "cs2_assistant.services.runtime_controller.save_strategy_config",
                side_effect=save_config,
            ),
        ):
            updated = self.controller.update_settings(
                {
                    "global": {
                        "maxListingRatioPct": 72.5,
                        "steamNetFactorPct": 86.9,
                        "maxNewListingsPerCycle": 7,
                        "caseMaxOpenCount": 88,
                        "autoListing": False,
                        "autoRebuy": True,
                    },
                    "specialRules": [],
                    "timePolicy": {
                        "scanMinutes": 6,
                        "steamSyncSeconds": 150,
                        "actionConfirmSeconds": [10, 20, 40],
                        "soldEvidenceMinutes": [0, 1, 3, 10],
                        "rebuyMinutes": [1, 3, 10],
                        "deliveryMinutes": [1, 5, 15, 30],
                        "staleListedRecheckHours": 18,
                        "staleListedMaxRatioTolerancePct": 1.75,
                    },
                    "reason": "test nested settings",
                }
            )

        settings = updated["settings"]
        self.assertAlmostEqual(72.5, settings["global"]["maxListingRatioPct"])
        self.assertAlmostEqual(86.9, settings["global"]["steamNetFactorPct"])
        self.assertEqual(7, settings["global"]["maxNewListingsPerCycle"])
        self.assertEqual(88, settings["global"]["caseMaxOpenCount"])
        self.assertFalse(settings["global"]["autoListing"])
        self.assertTrue(settings["global"]["autoRebuy"])
        self.assertEqual(6, settings["timePolicy"]["scanMinutes"])
        self.assertEqual(150, settings["timePolicy"]["steamSyncSeconds"])
        self.assertEqual([1, 3, 10], settings["timePolicy"]["rebuyMinutes"])
        self.assertEqual([1, 5, 15, 30], settings["timePolicy"]["deliveryMinutes"])
        self.assertAlmostEqual(
            1.75,
            settings["timePolicy"]["staleListedMaxRatioTolerancePct"],
        )
        self.assertAlmostEqual(0.725, holder["config"].guadao_max_listing_ratio)
        self.assertEqual(360.0, holder["config"].guadao_task_schedule["scanIntervalSeconds"])

    def test_special_ratio_above_75_percent_requires_explicit_confirmation(self) -> None:
        market_hash_name = "Dreams & Nightmares Case"
        db = self._open_db()
        try:
            db.upsert_items(
                [
                    CatalogItem(
                        market_hash_name=market_hash_name,
                        name_cn="梦魇武器箱",
                    )
                ]
            )
        finally:
            db.close()
        holder = {"config": StrategyConfig(guadao_max_listing_ratio=0.69)}

        def load_config(settings: Settings) -> StrategyConfig:
            return holder["config"]

        def save_config(settings: Settings, config: StrategyConfig) -> None:
            holder["config"] = config

        payload = {
            "global": {
                "maxListingRatioPct": 69,
                "steamNetFactorPct": 86.9,
                "maxNewListingsPerCycle": 5,
                "caseMaxOpenCount": 100,
                "autoListing": True,
                "autoRebuy": True,
            },
            "specialRules": [
                {
                    "id": "dreams-76",
                    "marketHashName": market_hash_name,
                    "displayName": "梦魇武器箱",
                    "maxRatioPct": 76,
                    "enabled": True,
                    "version": 1,
                }
            ],
        }
        with (
            patch(
                "cs2_assistant.services.runtime_controller.load_strategy_config",
                side_effect=load_config,
            ),
            patch(
                "cs2_assistant.services.runtime_controller.save_strategy_config",
                side_effect=save_config,
            ) as save,
        ):
            with self.assertRaisesRegex(ValueError, "75%"):
                self.controller.update_settings(dict(payload))
            save.assert_not_called()
            accepted = self.controller.update_settings(
                {**payload, "confirmHighRatio": True}
            )
            edited_payload = {
                **payload,
                "specialRules": [
                    {**payload["specialRules"][0], "maxRatioPct": 77, "version": 1}
                ],
                "confirmHighRatio": True,
            }
            edited = self.controller.update_settings(edited_payload)

        rules = accepted["settings"]["specialRules"]
        self.assertEqual(1, len(rules))
        self.assertEqual(market_hash_name, rules[0]["marketHashName"])
        self.assertAlmostEqual(76.0, rules[0]["maxRatioPct"])
        edited_rule = edited["settings"]["specialRules"][0]
        self.assertAlmostEqual(77.0, edited_rule["maxRatioPct"])
        self.assertIsNone(edited_rule["currentRatioPct"])
        self.assertIsNone(edited_rule["currentRatioObservedAt"])
        self.assertIsNotNone(edited_rule["updatedAt"])
        self.assertEqual(2, edited_rule["version"])
        self.assertIsNotNone(edited["settings"]["global"]["lastModifiedAt"])
        self.assertAlmostEqual(
            0.77,
            holder["config"].guadao_special_ratio_rules[0]["maxListingRatio"],
        )

        observed_at = "2026-07-16T15:00:00+00:00"
        db = self._open_db()
        try:
            db.save_guadao_case_ratio_snapshots(
                [
                    {
                        "market_hash_name": market_hash_name,
                        "observed_at": observed_at,
                        "name_cn": "梦魇武器箱",
                        "c5_sell_price": 8.09,
                        "steam_list_price": 13.61,
                        "steam_wall_price": 13.61,
                        "steam_after_tax_price": 11.83,
                        "listing_ratio": 0.7042,
                        "c5_price_source": "c5_batch",
                        "steam_price_source": "steam_orderbook",
                        "status": "ok",
                    }
                ]
            )
        finally:
            db.close()
        with patch(
            "cs2_assistant.services.runtime_controller.load_strategy_config",
            side_effect=load_config,
        ):
            observed = self.controller.settings_payload()
        observed_rule = observed["settings"]["specialRules"][0]
        self.assertAlmostEqual(70.42, observed_rule["currentRatioPct"])
        self.assertEqual(observed_at, observed_rule["currentRatioObservedAt"])

    def test_public_guadao_log_projects_related_operation_fields(self) -> None:
        event = {
            "event_id": "gdlog-1",
            "timestamp_utc": "2026-07-16T08:00:00Z",
            "source": "guadao",
            "provider": "steam",
            "component": "steam_request_scheduler",
            "operation": "request_failure",
            "market_hash_name": "Kilowatt Case",
            "trade_no": "GD-42",
            "safe_context": {
                "source": "guadao",
                "operationId": 42,
            },
        }

        projected = self.controller._public_guadao_log(event)

        self.assertEqual(42, projected["operationId"])
        self.assertEqual("GD-42", projected["tradeNo"])
        self.assertEqual("Kilowatt Case", projected["marketHashName"])

    def test_special_ratio_rule_rejects_catalog_item_outside_guadao_case_scope(self) -> None:
        market_hash_name = "AK-47 | Redline (Field-Tested)"
        db = self._open_db()
        try:
            db.upsert_items(
                [
                    CatalogItem(
                        market_hash_name=market_hash_name,
                        name_cn="AK-47 | 红线（略有磨损）",
                        raw_json={"typeName": "Rifle"},
                    )
                ]
            )
        finally:
            db.close()

        with self.assertRaisesRegex(ValueError, "Case"):
            self.controller._validate_special_ratio_rules(
                [
                    {
                        "marketHashName": market_hash_name,
                        "maxListingRatio": 0.72,
                        "enabled": True,
                    }
                ],
                global_ratio=0.69,
                confirm_high=False,
            )

    def test_special_ratio_item_search_only_returns_guadao_case_scope(self) -> None:
        case_name = "Test Filter Case"
        non_case_name = "AK-47 | Test Filter"
        db = self._open_db()
        try:
            db.upsert_items(
                [
                    CatalogItem(case_name, "测试筛选武器箱"),
                    CatalogItem(
                        non_case_name,
                        "AK-47 | 测试筛选",
                        raw_json={"typeName": "Rifle"},
                    ),
                ]
            )
        finally:
            db.close()

        result = self.controller.search_items("Test Filter", limit=10)

        self.assertEqual([case_name], [row["marketHashName"] for row in result["items"]])

    def test_issues_alias_summary_acknowledge_and_restore(self) -> None:
        db = self._open_db()
        try:
            rows = [
                (
                    "AK-47 | Steam Conflict",
                    OP_SELL_STEAM,
                    "manual_required",
                    {"manualReviewReason": "steam evidence conflict", "listingId": "L-1"},
                ),
                (
                    "Dreams & Nightmares Case",
                    OP_REBUY_C5,
                    "failed",
                    {"failedReason": "c5 order id conflict", "c5OrderId": "C5-1"},
                ),
                (
                    "P250 | Local Conflict",
                    "local_repair",
                    "listing_failed",
                    {"failedReason": "local asset mismatch"},
                ),
            ]
            operation_ids: list[int] = []
            for market_hash_name, operation_type, status, note in rows:
                op_id = db.add_pool_operation(
                    market_hash_name=market_hash_name,
                    strategy="guadao",
                    operation_type=operation_type,
                    asset_id=f"asset-{len(operation_ids) + 1}",
                    note=json.dumps(note),
                )
                db.update_pool_operation(op_id, status=status)
                operation_ids.append(op_id)
        finally:
            db.close()

        initial = self.controller.issues()
        self.assertEqual(initial["items"], initial["issues"])
        self.assertEqual(3, initial["total"])
        self.assertEqual(3, initial["summary"]["total"])
        self.assertEqual(1, initial["summary"]["steam"])
        self.assertEqual(1, initial["summary"]["c5"])
        self.assertEqual(1, initial["summary"]["local"])

        issue_id = f"pool-operation:{operation_ids[0]}"
        acknowledged = self.controller.acknowledge_issue(
            issue_id,
            acknowledged=True,
            reason="reviewed",
        )
        self.assertEqual(1, acknowledged["acknowledged"])
        hidden = self.controller.issues()
        self.assertEqual(2, hidden["total"])
        history = self.controller.issues(include_acknowledged=True)
        self.assertEqual(3, history["total"])
        self.assertEqual(1, history["summary"]["acknowledged"])

        restored = self.controller.acknowledge_issue(
            issue_id,
            acknowledged=False,
            reason="restore",
        )
        self.assertEqual(0, restored["acknowledged"])
        self.assertEqual(3, self.controller.issues()["total"])

    def test_operations_pagination_keyword_account_and_status_filters(self) -> None:
        db = self._open_db()
        try:
            items = [
                CatalogItem("AK-47 | Alpha", "阿尔法"),
                CatalogItem("AK-47 | Bravo", "布拉沃"),
                CatalogItem("P250 | Charlie", "查理"),
                CatalogItem("M4A1-S | Delta", "德尔塔"),
                CatalogItem("USP-S | Echo", "回声"),
            ]
            db.upsert_items(items)
            for index, item in enumerate(items):
                account_name = "steam-1" if index < 2 else "steam-2"
                op_id = db.add_pool_operation(
                    market_hash_name=item.market_hash_name,
                    strategy="guadao",
                    operation_type=OP_SELL_STEAM,
                    asset_id=f"asset-page-{index}",
                    note=json.dumps(
                        {
                            "steamAccountName": account_name,
                            "listingId": f"listing-{index}",
                        }
                    ),
                )
                db.update_pool_operation(
                    op_id,
                    status="listed" if index in {0, 2, 4} else "sold",
                )
        finally:
            db.close()

        page_one = self.controller.operations(page=1, page_size=2)
        page_two = self.controller.operations(page=2, page_size=2)
        self.assertEqual(5, page_one["total"])
        self.assertEqual(2, len(page_one["items"]))
        self.assertEqual(page_one["items"], page_one["operations"])
        self.assertEqual(2, len(page_two["items"]))
        self.assertTrue(
            {row["id"] for row in page_one["items"]}.isdisjoint(
                {row["id"] for row in page_two["items"]}
            )
        )
        keyword = self.controller.operations(keyword="alpha", page_size=10)
        self.assertEqual(1, keyword["total"])
        self.assertEqual("AK-47 | Alpha", keyword["items"][0]["marketHashName"])
        account = self.controller.operations(account_name="steam-1", page_size=10)
        self.assertEqual(2, account["total"])
        listed = self.controller.operations(status="listed", page_size=10)
        self.assertEqual(3, listed["total"])
        self.assertEqual(3, listed["summary"]["steamListed"])

    def test_operations_normalizes_unix_steam_sale_time_for_web_clients(self) -> None:
        db = self._open_db()
        try:
            db.upsert_items([CatalogItem("Kilowatt Case", "千瓦武器箱")])
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-unix-sold-time",
                note=json.dumps(
                    {
                        "steamAccountName": "steam-1",
                        "listingId": "listing-unix-time",
                        "steamSoldAt": 1784176769,
                    }
                ),
            )
            db.update_pool_operation(op_id, status="sold")
        finally:
            db.close()

        payload = self.controller.operations(page=1, page_size=10)
        projected = next(row for row in payload["items"] if row["id"] == op_id)
        self.assertEqual("2026-07-16T04:39:29+00:00", projected["steamSoldAt"])
        sold_event = next(
            event for event in projected["timeline"] if event["label"] == "Steam 官方确认售出"
        )
        self.assertEqual("2026-07-16T04:39:29+00:00", sold_event["at"])

    def test_operations_only_projects_c5_delivery_deadline_from_real_submission_time(self) -> None:
        db = self._open_db()
        try:
            db.upsert_items([CatalogItem("Revolution Case", "变革武器箱")])
            sell_id = db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-c5-deadline-projection",
                note=json.dumps({"listingId": "listing-c5-deadline"}),
            )
            db.update_pool_operation(sell_id, status="sold")
            rebuy_id = db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.70,
                note=json.dumps(
                    {
                        "sourceSellOperationId": sell_id,
                        "c5OrderId": "ORDER-DEADLINE",
                        "c5DeliveryDeadlineAt": "2026-07-01T00:00:00+00:00",
                    }
                ),
            )
            db.update_pool_operation(rebuy_id, status="delivery_pending")
        finally:
            db.close()

        payload = self.controller.operations(page=1, page_size=10)
        projected = next(row for row in payload["items"] if row["id"] == sell_id)
        self.assertIsNone(projected["c5OrderSubmittedAt"])
        self.assertIsNone(projected["c5DeliveryDeadlineAt"])

        submitted_at = "2026-07-16T01:02:03+00:00"
        db = self._open_db()
        try:
            rebuy = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?",
                (rebuy_id,),
            ).fetchone()
            rebuy_note = json.loads(rebuy["note"])
            rebuy_note["c5OrderSubmittedAt"] = submitted_at
            rebuy_note["c5DeliveryDeadlineAt"] = "2099-01-01T00:00:00+00:00"
            db.update_pool_operation(rebuy_id, note=json.dumps(rebuy_note))
        finally:
            db.close()

        payload = self.controller.operations(page=1, page_size=10)
        projected = next(row for row in payload["items"] if row["id"] == sell_id)
        self.assertEqual(submitted_at, projected["c5OrderSubmittedAt"])
        self.assertEqual("2026-07-17T01:02:03+00:00", projected["c5DeliveryDeadlineAt"])

    def test_scheduler_initialization_failure_blocks_steam_tasks_but_not_c5_tasks(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        self._set_runtime(RUNTIME_PROFIT_TRADE, enabled=True, runtime_status="running")

        class DormantThread:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.started = False

            def start(self) -> None:
                self.started = True

            def is_alive(self) -> bool:
                return False

            def join(self, timeout: float | None = None) -> None:
                return None

        with (
            patch(
                "cs2_assistant.services.steam_request_scheduler.configure_shared_steam_scheduler",
                side_effect=RuntimeError("scheduler init failed"),
            ),
            patch(
                "cs2_assistant.services.runtime_controller.threading.Thread",
                DormantThread,
            ),
        ):
            self.controller.start()
        self.controller._thread = None
        self.assertFalse(self.controller._steam_scheduler_ready)
        self.assertIn("scheduler init failed", str(self.controller._steam_scheduler_error))

        db = self._open_db()
        try:
            rebuy_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.0,
                note=json.dumps({"sourceSellOperationId": 10}),
            )
            delivery_id = db.add_pool_operation(
                market_hash_name="Dreams & Nightmares Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=7.0,
                note=json.dumps(
                    {
                        "c5OutTradeNo": "OUT-SCHEDULER-DOWN",
                        C5_DELIVERY_STATUS_KEY: "pending",
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(delivery_id, status="delivery_pending")
            self.controller._seed_tasks(db)
        finally:
            db.close()

        steam_task_keys = [
            TASK_GUADAO_SCAN,
            f"steam-sync:{self.accounts[0].id}",
            TASK_PROFIT_CYCLE,
        ]
        c5_task_keys = [f"rebuy:{rebuy_id}", f"delivery:{delivery_id}"]
        dispatched: list[str] = []
        blocked_errors: dict[str, str | None] = {}

        def dispatch(task: object, *, enabled: bool) -> dict[str, object]:
            dispatched.append(str(task["task_type"]))
            return {"ok": True}

        with patch.object(self.controller, "_dispatch_task", side_effect=dispatch):
            for task_key in [*steam_task_keys, *c5_task_keys]:
                self._move_all_tasks_to_future(except_key=task_key)
                db = self._open_db()
                try:
                    db.reschedule_scheduled_task(task_key, next_attempt_at=utc_now_iso())
                    task = db.claim_due_scheduled_tasks(
                        self.controller.worker_id,
                        limit=1,
                    )[0]
                finally:
                    db.close()
                self.controller._execute_claimed_task(
                    task,
                    gate={"status": "ready", "validCount": 5, "totalCount": 5},
                )
                if task_key in steam_task_keys:
                    db = self._open_db()
                    try:
                        blocked = db.get_scheduled_task(task_key)
                        blocked_errors[task_key] = blocked["last_error"]
                    finally:
                        db.close()

        self.assertEqual(
            [TASK_REBUY_ATTEMPT, TASK_C5_DELIVERY_CONFIRM],
            dispatched,
        )
        self.assertEqual(
            {task_key: "steam_scheduler_unavailable" for task_key in steam_task_keys},
            blocked_errors,
        )
        db = self._open_db()
        try:
            for task_key in steam_task_keys:
                task = db.get_scheduled_task(task_key)
                self.assertIn(task["status"], {"pending", "retry"})
            dashboard = self.controller.dashboard()
            self.assertFalse(dashboard["steamScheduler"]["ready"])
            self.assertEqual("unavailable", dashboard["steamScheduler"]["status"])
        finally:
            db.close()

    def test_stop_releases_shared_scheduler_owned_by_runtime(self) -> None:
        class DormantThread:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def start(self) -> None:
                return None

            def is_alive(self) -> bool:
                return False

            def join(self, timeout: float | None = None) -> None:
                return None

        with (
            patch(
                "cs2_assistant.services.steam_request_scheduler.configure_shared_steam_scheduler"
            ) as configure,
            patch(
                "cs2_assistant.services.steam_request_scheduler.reset_shared_steam_scheduler"
            ) as reset,
            patch(
                "cs2_assistant.services.runtime_controller.threading.Thread",
                DormantThread,
            ),
        ):
            self.controller.start()
            self.controller.stop(timeout=0)

        configure.assert_called_once()
        reset.assert_called_once_with(expected=configure.return_value)
        self.assertFalse(self.controller._owns_steam_scheduler)

    def test_serverchan_notifications_are_persistently_deduplicated(self) -> None:
        self.settings.serverchan_sendkey = "test-only-sendkey"
        self._confirm_migration()
        self._mark_all_cookies_valid()
        sent: list[tuple[str, str]] = []

        class FakeServerChanClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def send(self, title: str, body: str) -> None:
                sent.append((title, body))

        account = self.accounts[0]
        old_429 = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(
            microsecond=0
        ).isoformat()
        recovered_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        with patch(
            "cs2_assistant.services.runtime_controller.ServerChanClient",
            FakeServerChanClient,
        ):
            cookie_failure = {
                "account_id": account.id,
                "route": "market/mylistings",
                "status_code": 401,
            }
            self.controller._record_cookie_health_from_steam_event(cookie_failure)
            self.controller._record_cookie_health_from_steam_event(cookie_failure)
            cookie_recovery = {
                "account_id": account.id,
                "route": "market/mylistings",
                "status_code": 200,
            }
            self.controller._record_cookie_health_from_steam_event(cookie_recovery)
            self.controller._record_cookie_health_from_steam_event(cookie_recovery)
            self.assertEqual(2, len(sent))

            db = self._open_db()
            try:
                db.upsert_steam_route_circuit(
                    "steam:global",
                    scope="global",
                    state="open",
                    consecutive_429=3,
                    first_429_at=old_429,
                    last_429_at=old_429,
                    cooldown_until=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                    next_probe_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                    reason="429",
                )
            finally:
                db.close()
            self.controller._notify_steam_circuit_state()
            self.controller._notify_steam_circuit_state()
            self.assertEqual(4, len(sent))  # open + sustained/degraded

            db = self._open_db()
            try:
                db.upsert_steam_route_circuit(
                    "steam:global",
                    scope="global",
                    state="closed",
                    consecutive_429=0,
                    first_429_at=old_429,
                    last_429_at=old_429,
                    reason="probe_recovered",
                    payload={"recoveredAt": recovered_at},
                )
            finally:
                db.close()
            self.controller._notify_steam_circuit_state()
            self.controller._notify_steam_circuit_state()
            self.assertEqual(5, len(sent))

            db = self._open_db()
            try:
                issue_id = db.add_pool_operation(
                    market_hash_name="AK-47 | Notification Issue",
                    strategy="guadao",
                    operation_type=OP_SELL_STEAM,
                    asset_id="asset-notification",
                    note=json.dumps({"manualReviewReason": "steam evidence conflict"}),
                )
                db.update_pool_operation(issue_id, status="manual_required")
                self.controller._notify_new_guadao_issues(db)
                self.controller._notify_new_guadao_issues(db)

                timeout_id = db.add_pool_operation(
                    market_hash_name="Dreams & Nightmares Case",
                    strategy="guadao",
                    operation_type=OP_REBUY_C5,
                    expected_price=7.0,
                    note=json.dumps(
                        {
                            "c5OrderFailedCode": "delivery_timeout_24h",
                            "c5OrderId": "C5-TIMEOUT-NOTIFY",
                            "replacementRebuyOperationId": 999,
                        }
                    ),
                )
                db.update_pool_operation(timeout_id, status=C5_DELIVERY_FAILED)
                self.controller._notify_c5_delivery_timeouts(db)
                self.controller._notify_c5_delivery_timeouts(db)
            finally:
                db.close()
            self.assertEqual(7, len(sent))  # S3 issue + C5 timeout

            # A new controller instance reads the same persistent dedupe map.
            restarted = UnifiedRuntimeController(self.settings, poll_seconds=0.2)
            restarted.account_store = FakeAccountStore(self.accounts)
            restarted._initialize()
            restarted._notify_steam_circuit_state()
            db = self._open_db()
            try:
                restarted._notify_new_guadao_issues(db)
                restarted._notify_c5_delivery_timeouts(db)
                runtime = db.get_executor_runtime_state(RUNTIME_GUADAO)
                notification_events = json.loads(runtime["payload_json"])[
                    "notificationEvents"
                ]
            finally:
                db.close()
            self.assertEqual(7, len(sent))
            self.assertEqual(7, len(notification_events))


if __name__ == "__main__":
    unittest.main()
