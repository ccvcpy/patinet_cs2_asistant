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
    C5_SUBMISSION_UNCONFIRMED_STATUS,
    RUNTIME_GUADAO,
    RUNTIME_PROFIT_TRADE,
    C5_ORDER_RECONCILE_DEGRADED_DELAY_SECONDS,
    C5_ORDER_RECONCILE_DELAYS_SECONDS,
    TASK_C5_DELIVERY_CONFIRM,
    TASK_C5_ORDER_RECONCILE,
    TASK_GUADAO_SCAN,
    TASK_PROFIT_CYCLE,
    TASK_PROFIT_MANUAL_EXECUTION,
    TASK_PROFIT_SELECTION_WATCH,
    TASK_REBUY_BATCH,
    TASK_REBUY_ATTEMPT,
    TASK_STALE_LISTING_RECHECK,
    TASK_STEAM_ACCOUNT_SYNC,
    TASK_STEAM_LISTING_CONFIRM,
    TASK_STEAM_SALE_EVIDENCE,
    UnifiedRuntimeController,
)
from cs2_assistant.services.strategy import load_strategy_config, save_strategy_config
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

    def _claim_stale_listing_recheck_task(self) -> dict[str, object]:
        """Move unrelated work away and claim the maintenance task for a gate test."""

        self._move_all_tasks_to_future(except_key=TASK_STALE_LISTING_RECHECK)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                next_attempt_at=utc_now_iso(),
            )
            claimed = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )
            self.assertEqual(1, len(claimed))
            task = dict(claimed[0])
            self.assertEqual(TASK_STALE_LISTING_RECHECK, task["task_type"])
            return task
        finally:
            db.close()

    def _create_sold_pending_rebuy(
        self,
        *,
        market_hash_name: str,
        suffix: str,
        steam_net_amount: float = 10.0,
        frozen_price: float = 6.8,
        frozen_ratio: float = 0.68,
    ) -> tuple[int, int, str]:
        asset_id = f"asset-batch-{suffix}"
        sold_at = "2026-07-16T10:00:00+00:00"
        db = self._open_db()
        try:
            db.upsert_pool_item(
                market_hash_name,
                1,
                status="pending_rebuy",
            )
            db.upsert_inventory_assets(
                [
                    {
                        "assetId": asset_id,
                        "marketHashName": market_hash_name,
                        "steamId": self.accounts[0].steam_id64,
                        "ifTradable": True,
                    }
                ]
            )
            db.set_asset_status(asset_id, "sold")
            sell_id = db.add_pool_operation(
                market_hash_name=market_hash_name,
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                expected_price=steam_net_amount / 0.87,
                asset_id=asset_id,
                note=json.dumps(
                    {
                        "steamSoldAt": sold_at,
                        "steamSellerNetPrice": steam_net_amount,
                        "rebuyPrice": frozen_price,
                        "listingRatioAtOpen": frozen_ratio,
                        "maxRebuyRatioAtOpen": frozen_ratio,
                        "guadaoMaxListingRatioAtOpen": 0.69,
                    }
                ),
            )
            db.update_pool_operation(sell_id, status="sold")
            rebuy_id = db.add_pool_operation(
                market_hash_name=market_hash_name,
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=frozen_price,
                note=json.dumps(
                    {
                        "sourceSellOperationId": sell_id,
                        "steamSoldAt": sold_at,
                        "steamSellerNetPrice": steam_net_amount,
                        "maxRebuyRatioAtOpen": frozen_ratio,
                    }
                ),
            )
            db.upsert_scheduled_task(
                f"rebuy:{rebuy_id}",
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_ATTEMPT,
                next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=2),
                operation_id=rebuy_id,
            )
            return sell_id, rebuy_id, asset_id
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
                        "c5TradeOrderId": "OLD-TRADE-DELIVERY",
                        "c5PayStatus": 1,
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

    def test_seed_tasks_migrates_incomplete_delivery_to_unique_submission_reconcile(self) -> None:
        db = self._open_db()
        try:
            sell_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                expected_price=1.50,
                note=json.dumps({"steamSoldAt": utc_now_iso()}),
            )
            db.update_pool_operation(sell_id, status="sold")
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.02,
                note=json.dumps(
                    {
                        "sourceSellOperationId": sell_id,
                        "c5OutTradeNo": "OUT-WITHOUT-REMOTE-ORDER",
                        "c5OrderId": "ONLY-ASSET-ORDER",
                        "c5OrderSubmittedAt": utc_now_iso(),
                        C5_DELIVERY_STATUS_KEY: "pending",
                    }
                ),
            )
            db.update_pool_operation(op_id, status="delivery_pending", actual_price=1.01)
            db.upsert_scheduled_task(
                f"delivery:{op_id}",
                source=RUNTIME_GUADAO,
                task_type=TASK_C5_DELIVERY_CONFIRM,
                next_attempt_at=utc_now_iso(),
                operation_id=op_id,
            )

            self.controller._seed_tasks(db)
            self.controller._seed_tasks(db)

            operation = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            note = json.loads(operation["note"])
            self.assertEqual("c5_submission_unconfirmed", operation["status"])
            self.assertEqual(
                "missing_complete_order_ids",
                note["c5SubmissionUnconfirmedReason"],
            )
            self.assertEqual("delivery_pending", note["c5SubmissionPreviousStatus"])
            self.assertIsNotNone(note["c5SubmissionUnconfirmedAt"])
            self.assertIsNone(db.get_scheduled_task(f"delivery:{op_id}"))
            reconcile = db.get_scheduled_task(f"reconcile:{op_id}")
            self.assertIsNotNone(reconcile)
            self.assertEqual(TASK_C5_ORDER_RECONCILE, reconcile["task_type"])
            self.assertEqual("pending", reconcile["status"])
            self.assertEqual(
                1,
                len(
                    [
                        row
                        for row in db.list_scheduled_tasks(limit=5000)
                        if str(row["operation_id"] or "") == str(op_id)
                    ]
                ),
            )
        finally:
            db.close()

        public = self.controller.operations(page=1, page_size=10)
        projected = next(row for row in public["items"] if row["id"] == sell_id)
        self.assertEqual("c5_submission_unconfirmed", projected["status"])
        self.assertEqual("C5 补仓待查证据", projected["stage"])
        self.assertEqual("C5 补仓证据复核", projected["nextTaskLabel"])
        self.assertIsNone(projected["c5DeliveryDeadlineAt"])
        self.assertEqual(1, public["summary"]["c5EvidencePending"])
        self.assertEqual(1, public["summary"]["submissionUnconfirmed"])

    def test_seed_tasks_never_treats_out_trade_no_as_delivery_evidence(self) -> None:
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.02,
                note=json.dumps(
                    {
                        "c5OutTradeNo": "OUT-ONLY",
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(op_id, status="c5_submission_unconfirmed")

            self.controller._seed_tasks(db)

            self.assertIsNone(db.get_scheduled_task(f"delivery:{op_id}"))
            reconcile = db.get_scheduled_task(f"reconcile:{op_id}")
            self.assertIsNotNone(reconcile)
            self.assertEqual(TASK_C5_ORDER_RECONCILE, reconcile["task_type"])
        finally:
            db.close()

    def test_seed_tasks_keeps_delivery_when_both_order_ids_exist_even_if_pay_status_is_not_one(self) -> None:
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.02,
                note=json.dumps(
                    {
                        "c5OutTradeNo": "OUT-PAY-ABNORMAL",
                        "c5OrderId": "ASSET-PAY-ABNORMAL",
                        "c5TradeOrderId": "TRADE-PAY-ABNORMAL",
                        "c5PayStatus": 2,
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(op_id, status="delivery_pending")

            self.controller._seed_tasks(db)

            operation = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            self.assertEqual("delivery_pending", operation["status"])
            delivery = db.get_scheduled_task(f"delivery:{op_id}")
            self.assertIsNotNone(delivery)
            self.assertEqual(TASK_C5_DELIVERY_CONFIRM, delivery["task_type"])
            self.assertIsNone(db.get_scheduled_task(f"reconcile:{op_id}"))
        finally:
            db.close()

    def test_seed_tasks_keeps_engine_recognized_asset_order_while_detail_is_unreadable(self) -> None:
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.02,
                note=json.dumps(
                    {
                        "c5OutTradeNo": "OUT-RECOGNIZED-ASSET",
                        "c5OrderId": "ASSET-RECOGNIZED",
                        "c5OrderRecognized": True,
                        "c5OrderRecognizedAt": utc_now_iso(),
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(op_id, status="delivery_pending")

            self.controller._seed_tasks(db)

            operation = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            self.assertEqual("delivery_pending", operation["status"])
            delivery = db.get_scheduled_task(f"delivery:{op_id}")
            self.assertIsNotNone(delivery)
            self.assertEqual(TASK_C5_DELIVERY_CONFIRM, delivery["task_type"])
            self.assertIsNone(db.get_scheduled_task(f"reconcile:{op_id}"))
        finally:
            db.close()

    def test_seed_tasks_cancels_stale_running_delivery_when_migrating(self) -> None:
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.02,
                note=json.dumps(
                    {
                        "c5OutTradeNo": "OUT-STALE-RUNNING",
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(op_id, status="delivery_pending")
            db.upsert_scheduled_task(
                f"delivery:{op_id}",
                source=RUNTIME_GUADAO,
                task_type=TASK_C5_DELIVERY_CONFIRM,
                next_attempt_at=utc_now_iso(),
                operation_id=op_id,
            )
            db.conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = 'running', lease_owner = 'stale-worker',
                    lease_expires_at = ?
                WHERE task_key = ?
                """,
                (
                    (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    f"delivery:{op_id}",
                ),
            )
            db.conn.commit()

            self.controller._seed_tasks(db)

            operation = db.conn.execute(
                "SELECT status FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            self.assertEqual("c5_submission_unconfirmed", operation["status"])
            obsolete = db.get_scheduled_task(f"delivery:{op_id}")
            self.assertEqual("cancelled", obsolete["status"])
            self.assertIsNone(obsolete["lease_owner"])
            self.assertEqual(
                "superseded_by_c5_submission_reconcile",
                obsolete["last_error"],
            )
            reconcile = db.get_scheduled_task(f"reconcile:{op_id}")
            self.assertEqual("pending", reconcile["status"])
        finally:
            db.close()

    def test_reconcile_task_stops_on_terminal_state_and_delivery_is_seeded_after_ids_exist(self) -> None:
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.02,
                note=json.dumps(
                    {
                        "c5OutTradeNo": "OUT-RECOVERED",
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(op_id, status="c5_submission_unconfirmed")
            db.upsert_scheduled_task(
                f"reconcile:{op_id}",
                source=RUNTIME_GUADAO,
                task_type=TASK_C5_ORDER_RECONCILE,
                next_attempt_at=utc_now_iso(),
                operation_id=op_id,
            )
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            for scheduled in db.list_scheduled_tasks(limit=5000):
                if str(scheduled["task_key"]) != f"reconcile:{op_id}":
                    db.reschedule_scheduled_task(
                        str(scheduled["task_key"]),
                        next_attempt_at=future,
                    )
            task = db.claim_due_scheduled_tasks(self.controller.worker_id, limit=1)[0]
            note = json.loads(
                db.conn.execute(
                    "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
                ).fetchone()["note"]
            )
            note.update(
                {
                    "c5OrderId": "ASSET-RECOVERED",
                    "c5TradeOrderId": "TRADE-RECOVERED",
                    "c5PayStatus": 1,
                }
            )
            db.update_pool_operation(
                op_id,
                status="delivery_pending",
                note=json.dumps(note),
            )
        finally:
            db.close()

        self.controller._reschedule_after_task(
            task,
            result={"ok": True, "state": "delivery_pending", "checked": True},
        )

        db = self._open_db()
        try:
            reconcile = db.get_scheduled_task(f"reconcile:{op_id}")
            self.assertEqual("completed", reconcile["status"])
            self.controller._seed_tasks(db)
            delivery = db.get_scheduled_task(f"delivery:{op_id}")
            self.assertIsNotNone(delivery)
            self.assertEqual(TASK_C5_DELIVERY_CONFIRM, delivery["task_type"])
        finally:
            db.close()

    def test_reconcile_task_slows_down_and_keeps_retrying_after_fast_attempts(self) -> None:
        self.assertEqual(
            (30.0, 90.0, 180.0, 300.0, 600.0),
            C5_ORDER_RECONCILE_DELAYS_SECONDS,
        )
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.02,
                note=json.dumps(
                    {
                        "c5OutTradeNo": "OUT-UNREADABLE",
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(op_id, status="c5_submission_unconfirmed")
            db.upsert_scheduled_task(
                f"reconcile:{op_id}",
                source=RUNTIME_GUADAO,
                task_type=TASK_C5_ORDER_RECONCILE,
                next_attempt_at=utc_now_iso(),
                operation_id=op_id,
            )
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            for scheduled in db.list_scheduled_tasks(limit=5000):
                if str(scheduled["task_key"]) != f"reconcile:{op_id}":
                    db.reschedule_scheduled_task(
                        str(scheduled["task_key"]),
                        next_attempt_at=future,
                    )
            db.conn.execute(
                "UPDATE scheduled_tasks SET attempt_count = 4 WHERE task_key = ?",
                (f"reconcile:{op_id}",),
            )
            db.conn.commit()
            task = db.claim_due_scheduled_tasks(self.controller.worker_id, limit=1)[0]
            self.assertEqual(5, int(task["attempt_count"]))
        finally:
            db.close()

        unreadable = {
            "ok": False,
            "state": "c5_submission_unconfirmed",
            "checked": False,
            "error": "C5 buyer order list unavailable",
        }
        with patch.object(self.controller, "_emit_guadao_runtime_event") as emit:
            self.controller._reschedule_after_task(task, result=unreadable)

            db = self._open_db()
            try:
                first_reconcile = db.get_scheduled_task(f"reconcile:{op_id}")
                first_next = datetime.fromisoformat(first_reconcile["next_attempt_at"])
                self.assertGreaterEqual(
                    first_next,
                    datetime.now(timezone.utc)
                    + timedelta(seconds=C5_ORDER_RECONCILE_DEGRADED_DELAY_SECONDS - 2),
                )
                db.reschedule_scheduled_task(
                    f"reconcile:{op_id}",
                    next_attempt_at=utc_now_iso(),
                )
                second_task = db.claim_due_scheduled_tasks(
                    self.controller.worker_id,
                    limit=1,
                )[0]
            finally:
                db.close()

            self.controller._reschedule_after_task(second_task, result=unreadable)

        degraded_events = [
            call
            for call in emit.call_args_list
            if call.kwargs.get("reconcileState") == "slow_retry"
        ]
        self.assertEqual(1, len(degraded_events))

        db = self._open_db()
        try:
            operation = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            note = json.loads(operation["note"])
            self.assertEqual("c5_submission_unconfirmed", operation["status"])
            self.assertEqual(
                "reconcile_fast_attempts_exhausted",
                note["c5SubmissionReconcileAlertCode"],
            )
            self.assertEqual(6, note["c5SubmissionReconcileAttemptCount"])
            self.assertIsNotNone(note["c5SubmissionReconcileSlowRetryAt"])
            self.assertEqual(
                C5_ORDER_RECONCILE_DEGRADED_DELAY_SECONDS,
                note["c5SubmissionReconcileNextDelaySeconds"],
            )
            self.assertIsNone(note.get("replacementRebuyOperationId"))
            reconcile = db.get_scheduled_task(f"reconcile:{op_id}")
            self.assertEqual("pending", reconcile["status"])
        finally:
            db.close()

    def test_dispatch_routes_submission_reconcile_to_read_only_engine_entrypoint(self) -> None:
        expected = {
            "ok": True,
            "operationId": 77,
            "state": "c5_submission_unconfirmed",
            "checked": 0,
            "replacements": 0,
        }
        with patch.object(
            ExecutionEngine,
            "run_guadao_c5_submission_reconcile_task",
            return_value=expected,
        ) as reconcile:
            result = self.controller._dispatch_task(
                {
                    "task_key": "reconcile:77",
                    "task_type": TASK_C5_ORDER_RECONCILE,
                    "source": RUNTIME_GUADAO,
                    "account_id": None,
                    "operation_id": 77,
                },
                enabled=False,
            )

        self.assertEqual(expected, result)
        reconcile.assert_called_once_with(77)

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

    def test_partial_account_sync_defers_only_unresolved_history_operation(self) -> None:
        account = self.accounts[0]
        config = StrategyConfig(
            guadao_task_schedule={
                "saleEvidenceDelaysSeconds": [0.0, 60.0, 180.0, 600.0],
            }
        )
        db = self._open_db()
        try:
            resolved_id = db.add_pool_operation(
                market_hash_name="Resolved Active Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-resolved-active",
                note=json.dumps(
                    {
                        "steamAccountId": account.id,
                        "steamId64": account.steam_id64,
                    }
                ),
            )
            deferred_id = db.add_pool_operation(
                market_hash_name="Deferred History Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-deferred-history",
                note=json.dumps(
                    {
                        "steamAccountId": account.id,
                        "steamId64": account.steam_id64,
                        "confirmationStatus": "listing_missing_unverified",
                    }
                ),
            )
            db.update_pool_operation(resolved_id, status="listed")
            db.update_pool_operation(deferred_id, status="listing_pending")
            due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            tasks: list[dict[str, object]] = []
            for operation_id in (resolved_id, deferred_id):
                task_key = f"sale-evidence:{operation_id}"
                db.upsert_scheduled_task(
                    task_key,
                    source=RUNTIME_GUADAO,
                    task_type=TASK_STEAM_SALE_EVIDENCE,
                    account_id=account.id,
                    operation_id=operation_id,
                    next_attempt_at=due_at,
                    payload={"tierIndex": 0},
                    status="waiting",
                )
                tasks.append(dict(db.get_scheduled_task(task_key)))
            retry_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            before = datetime.now(timezone.utc)

            with patch(
                "cs2_assistant.services.runtime_controller.load_strategy_config",
                return_value=config,
            ):
                self.controller._advance_due_steam_operation_tasks(
                    db,
                    tasks,
                    result={
                        "ok": True,
                        "partial": True,
                        "historyDeferredOperationIds": [deferred_id],
                        "historyRetryAt": retry_at.isoformat(),
                    },
                    error=None,
                )

            resolved_task = db.get_scheduled_task(f"sale-evidence:{resolved_id}")
            deferred_task = db.get_scheduled_task(f"sale-evidence:{deferred_id}")
            resolved_payload = json.loads(str(resolved_task["payload_json"]))
            deferred_payload = json.loads(str(deferred_task["payload_json"]))
            self.assertEqual(1, resolved_payload["tierIndex"])
            self.assertGreaterEqual(
                datetime.fromisoformat(str(resolved_task["next_attempt_at"])),
                before + timedelta(seconds=59),
            )
            self.assertEqual(0, deferred_payload["tierIndex"])
            self.assertGreaterEqual(
                datetime.fromisoformat(str(deferred_task["next_attempt_at"])),
                retry_at - timedelta(seconds=1),
            )
        finally:
            db.close()

    def test_sale_evidence_timer_survives_listing_missing_unverified_sync(self) -> None:
        account = self.accounts[0]
        config = StrategyConfig(
            guadao_task_schedule={
                "saleEvidenceDelaysSeconds": [0.0, 60.0, 180.0, 600.0],
            }
        )
        db = self._open_db()
        try:
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-listing-missing-unverified",
                note=json.dumps(
                    {
                        "steamAccountId": account.id,
                        "steamId64": account.steam_id64,
                        "confirmationStatus": "listing_missing_unverified",
                    }
                ),
            )
            db.update_pool_operation(op_id, status="listing_pending")
            task_key = f"sale-evidence:{op_id}"
            db.upsert_scheduled_task(
                task_key,
                source=RUNTIME_GUADAO,
                task_type=TASK_STEAM_SALE_EVIDENCE,
                account_id=account.id,
                operation_id=op_id,
                next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                payload={"tierIndex": 0},
                status="waiting",
            )
            due = [dict(db.get_scheduled_task(task_key))]
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

            advanced = db.get_scheduled_task(task_key)
            self.assertIsNotNone(advanced)
            self.assertEqual("waiting", advanced["status"])
            payload = json.loads(str(advanced["payload_json"]))
            self.assertEqual(1, payload["tierIndex"])
            self.assertGreaterEqual(
                datetime.fromisoformat(str(advanced["next_attempt_at"])),
                before + timedelta(seconds=59),
            )
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
        self.assertFalse(load_strategy_config(self.settings).profit_trade_enabled)

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

    def test_enabling_profit_trade_runs_first_cycle_immediately_then_projects_schedule(self) -> None:
        self._confirm_migration()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(TASK_PROFIT_CYCLE, next_attempt_at=future)
        finally:
            db.close()

        before = datetime.now(timezone.utc)
        runtime = self.controller.toggle_executor(RUNTIME_PROFIT_TRADE, True)
        self.assertTrue(runtime["enabled"])
        self.assertEqual("preparing", runtime["runtimeStatus"])
        self.assertTrue(load_strategy_config(self.settings).profit_trade_enabled)

        db = self._open_db()
        try:
            task = db.get_scheduled_task(TASK_PROFIT_CYCLE)
        finally:
            db.close()
        next_attempt = datetime.fromisoformat(str(task["next_attempt_at"]))
        self.assertGreaterEqual(next_attempt, before - timedelta(seconds=1))
        self.assertLessEqual(next_attempt, datetime.now(timezone.utc) + timedelta(seconds=1))

        public = self.controller.runtime_states(RUNTIME_PROFIT_TRADE)["state"]
        self.assertEqual(task["next_attempt_at"], public["nextAttemptAt"])
        self.assertFalse(public["taskRunning"])

        db = self._open_db()
        try:
            db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_PROFIT_TRADE,
                limit=1,
            )
        finally:
            db.close()
        running = self.controller.runtime_states(RUNTIME_PROFIT_TRADE)["state"]
        self.assertTrue(running["taskRunning"])
        self.assertEqual("running", running["taskStatus"])

        stale_config = load_strategy_config(self.settings)
        stale_config.profit_trade_enabled = False
        save_strategy_config(self.settings, stale_config)
        self.controller._initialize()
        self.assertTrue(load_strategy_config(self.settings).profit_trade_enabled)

    def test_profit_cycle_now_does_not_requeue_a_running_cycle(self) -> None:
        self._confirm_migration()
        self._set_runtime(
            RUNTIME_PROFIT_TRADE,
            enabled=True,
            runtime_status="running",
        )
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(
                TASK_PROFIT_CYCLE,
                next_attempt_at=utc_now_iso(),
            )
            claimed = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_PROFIT_TRADE,
                limit=1,
            )
            self.assertEqual(1, len(claimed))
            running_before = db.get_scheduled_task(TASK_PROFIT_CYCLE)
            lease_owner_before = running_before["lease_owner"]
        finally:
            db.close()

        result = self.controller.profit_cycle_now()

        self.assertTrue(result["ok"])
        self.assertFalse(result["queued"])
        self.assertTrue(result["alreadyRunning"])
        db = self._open_db()
        try:
            running_after = db.get_scheduled_task(TASK_PROFIT_CYCLE)
            self.assertEqual("running", running_after["status"])
            self.assertEqual(lease_owner_before, running_after["lease_owner"])
        finally:
            db.close()

    def test_profit_dispatch_repairs_config_from_current_runtime_switch(self) -> None:
        task = {"task_type": TASK_PROFIT_CYCLE}
        self._set_runtime(
            RUNTIME_PROFIT_TRADE,
            enabled=True,
            runtime_status="running",
        )
        stale_config = load_strategy_config(self.settings)
        stale_config.profit_trade_enabled = False
        save_strategy_config(self.settings, stale_config)
        observed: list[bool] = []

        def fake_run(
            _settings: Settings,
            *,
            config: StrategyConfig,
            new_action_guard: object,
        ) -> object:
            observed.append(config.profit_trade_enabled)
            return SimpleNamespace(to_dict=lambda: {"ok": True})

        with patch(
            "cs2_assistant.services.runtime_controller.run_profit_trade_once",
            side_effect=fake_run,
        ):
            result = self.controller._dispatch_task(task, enabled=False)

        self.assertEqual({"ok": True}, result)
        self.assertEqual([True], observed)
        self.assertTrue(load_strategy_config(self.settings).profit_trade_enabled)

        self._set_runtime(
            RUNTIME_PROFIT_TRADE,
            enabled=False,
            runtime_status="stopped",
        )
        with (
            patch.object(
                self.controller,
                "_run_profit_closure_once",
                return_value={"ok": True, "settled": []},
            ) as closure,
            patch(
                "cs2_assistant.services.runtime_controller.run_profit_trade_once"
            ) as automatic,
        ):
            result = self.controller._dispatch_task(task, enabled=True)

        self.assertEqual({"ok": True, "settled": []}, result)
        closure.assert_called_once_with()
        automatic.assert_not_called()
        self.assertFalse(load_strategy_config(self.settings).profit_trade_enabled)

    def test_guadao_scan_starvation_guard_uses_one_full_ready_scan_interval(self) -> None:
        self._confirm_migration()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        expected = float(
            load_strategy_config(self.settings).effective_guadao_task_schedule()[
                "scanIntervalSeconds"
            ]
        )
        db = self._open_db()
        try:
            self.assertEqual(
                expected,
                self.controller._guadao_scan_starvation_guard_seconds(
                    db,
                    gate={"status": "ready"},
                ),
            )
            self.assertIsNone(
                self.controller._guadao_scan_starvation_guard_seconds(
                    db,
                    gate={"status": "preparing"},
                )
            )
        finally:
            db.close()

        self._set_runtime(RUNTIME_GUADAO, enabled=False, runtime_status="stopped")
        db = self._open_db()
        try:
            self.assertIsNone(
                self.controller._guadao_scan_starvation_guard_seconds(
                    db,
                    gate={"status": "ready"},
                )
            )
        finally:
            db.close()

    def test_tick_deadline_boosts_overdue_scan_once_ahead_of_p1(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        self._move_all_tasks_to_future()
        interval = float(
            load_strategy_config(self.settings).effective_guadao_task_schedule()[
                "scanIntervalSeconds"
            ]
        )
        now = datetime.now(timezone.utc)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(
                TASK_GUADAO_SCAN,
                next_attempt_at=(now - timedelta(seconds=interval + 1)).isoformat(),
            )
            db.upsert_scheduled_task(
                "test-p1-rebuy-backlog",
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_ATTEMPT,
                next_attempt_at=now.isoformat(),
                operation_id=999_999,
                priority=1,
            )
        finally:
            db.close()

        with patch.object(
            self.controller,
            "_dispatch_task",
            return_value={"ok": True, "listed": 0},
        ) as dispatch:
            result = self.controller.tick(max_tasks=1)

        self.assertEqual([TASK_GUADAO_SCAN], result["processed"])
        dispatch.assert_called_once()
        self.assertEqual(TASK_GUADAO_SCAN, dispatch.call_args.args[0]["task_type"])
        db = self._open_db()
        try:
            scan = db.get_scheduled_task(TASK_GUADAO_SCAN)
            backlog = db.get_scheduled_task("test-p1-rebuy-backlog")
            self.assertEqual(3, scan["priority"])
            self.assertEqual("pending", backlog["status"])
            self.assertEqual(0, backlog["attempt_count"])
        finally:
            db.close()

    def test_seed_tasks_creates_independent_stale_listing_recheck_task(self) -> None:
        """The hourly stale-listing maintenance task must always be seeded globally."""

        db = self._open_db()
        try:
            task = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(TASK_STALE_LISTING_RECHECK, task["task_key"])
            self.assertEqual(TASK_STALE_LISTING_RECHECK, task["task_type"])
            self.assertEqual(RUNTIME_GUADAO, task["source"])
            self.assertEqual(0, int(task["priority"]))
            self.assertIsNone(task["account_id"])

            schedule = load_strategy_config(self.settings).effective_guadao_task_schedule()
            self.assertEqual(86400.0, float(schedule["staleListedCheckIntervalSeconds"]))
            next_attempt = datetime.fromisoformat(str(task["next_attempt_at"]))
            if next_attempt.tzinfo is None:
                next_attempt = next_attempt.replace(tzinfo=timezone.utc)
            self.assertLessEqual(
                abs(
                    (
                        next_attempt.astimezone(timezone.utc)
                        - datetime.now(timezone.utc)
                    ).total_seconds()
                ),
                5.0,
            )
        finally:
            db.close()

    def test_stale_listing_interval_malformed_config_falls_back_to_one_day(self) -> None:
        config = load_strategy_config(self.settings)
        config.guadao_task_schedule = {
            **config.effective_guadao_task_schedule(),
            "staleListedCheckIntervalSeconds": 0,
        }
        self.assertEqual(
            86400.0,
            float(config.effective_guadao_task_schedule()["staleListedCheckIntervalSeconds"]),
        )
        config.guadao_task_schedule["staleListedCheckIntervalSeconds"] = -10
        self.assertEqual(
            86400.0,
            float(config.effective_guadao_task_schedule()["staleListedCheckIntervalSeconds"]),
        )
        config.guadao_task_schedule["staleListedCheckIntervalSeconds"] = float("nan")
        self.assertEqual(
            86400.0,
            float(config.effective_guadao_task_schedule()["staleListedCheckIntervalSeconds"]),
        )

    def test_seed_tasks_repairs_stale_priority_without_rescheduling_lease(self) -> None:
        """Legacy priority is repaired without moving or stealing an active task."""

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        lease_expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        db = self._open_db()
        try:
            db.upsert_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                source=RUNTIME_GUADAO,
                task_type=TASK_STALE_LISTING_RECHECK,
                next_attempt_at=future,
                status="pending",
                priority=3,
            )
            db.conn.execute(
                """
                UPDATE scheduled_tasks
                SET source = ?, task_type = ?, account_id = ?, operation_id = ?,
                    status = 'running', lease_owner = ?, lease_expires_at = ?
                WHERE task_key = ?
                """,
                (
                    "legacy-source",
                    "legacy-task-type",
                    "legacy-account",
                    "321",
                    "legacy-worker",
                    lease_expires.isoformat(),
                    TASK_STALE_LISTING_RECHECK,
                ),
            )
            db.conn.commit()
            before = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(before)
            assert before is not None

            self.controller._seed_tasks(db)

            after = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(after)
            assert after is not None
            self.assertEqual(0, int(after["priority"]))
            self.assertEqual("running", after["status"])
            self.assertEqual("legacy-worker", after["lease_owner"])
            self.assertEqual("legacy-source", after["source"])
            self.assertEqual("legacy-task-type", after["task_type"])
            self.assertEqual("legacy-account", after["account_id"])
            self.assertEqual("321", after["operation_id"])
            self.assertEqual(before["next_attempt_at"], after["next_attempt_at"])
            self.assertEqual(before["lease_expires_at"], after["lease_expires_at"])
        finally:
            db.close()

    def test_seed_tasks_repairs_legacy_waiting_stale_task_to_pending(self) -> None:
        """A legacy waiting row must remain claimable after the runtime upgrade."""

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        db = self._open_db()
        try:
            db.upsert_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                source=RUNTIME_GUADAO,
                task_type=TASK_STALE_LISTING_RECHECK,
                next_attempt_at=future,
                status="waiting",
                priority=3,
            )
            self.controller._seed_tasks(db)
            repaired = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(repaired)
            assert repaired is not None
            self.assertEqual("pending", repaired["status"])
            self.assertEqual(0, int(repaired["priority"]))
            repaired_at = datetime.fromisoformat(str(repaired["next_attempt_at"]))
            if repaired_at.tzinfo is None:
                repaired_at = repaired_at.replace(tzinfo=timezone.utc)
            self.assertEqual(
                future.replace(microsecond=0),
                repaired_at.astimezone(timezone.utc),
            )
            self.assertIsNone(repaired["lease_owner"])
            self.assertIsNone(repaired["lease_expires_at"])
        finally:
            db.close()

    def test_seed_tasks_repairs_stale_task_metadata_without_rescheduling(self) -> None:
        """A polluted global row must be restored without changing its cadence."""

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        db = self._open_db()
        try:
            db.conn.execute(
                """
                UPDATE scheduled_tasks
                SET source = ?, task_type = ?, account_id = ?, operation_id = ?,
                    priority = ?, next_attempt_at = ?, status = 'pending',
                    payload_json = ?
                WHERE task_key = ?
                """,
                (
                    "wrong-source",
                    "wrong-task-type",
                    "wrong-account",
                    "999",
                    7,
                    future.isoformat(),
                    json.dumps({"legacy": True}, ensure_ascii=False),
                    TASK_STALE_LISTING_RECHECK,
                ),
            )
            db.conn.commit()

            self.controller._seed_tasks(db)

            repaired = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(repaired)
            assert repaired is not None
            self.assertEqual(RUNTIME_GUADAO, repaired["source"])
            self.assertEqual(TASK_STALE_LISTING_RECHECK, repaired["task_type"])
            self.assertIsNone(repaired["account_id"])
            self.assertIsNone(repaired["operation_id"])
            self.assertEqual(0, int(repaired["priority"]))
            self.assertEqual("pending", repaired["status"])
            self.assertEqual(
                future.replace(microsecond=0),
                datetime.fromisoformat(str(repaired["next_attempt_at"])).replace(
                    microsecond=0
                ),
            )
            self.assertEqual({"legacy": True}, json.loads(repaired["payload_json"] or "{}"))
        finally:
            db.close()

    def test_stale_listing_recheck_renews_lease_before_dispatch(self) -> None:
        """A large stale-listing walk must not run on an expiring 180s lease."""

        self._confirm_migration()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        self._move_all_tasks_to_future()
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                next_attempt_at=utc_now_iso(),
            )
            claimed = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
                lease_seconds=180,
            )
            self.assertEqual(1, len(claimed))
            task = dict(claimed[0])
            self.assertEqual(TASK_STALE_LISTING_RECHECK, task["task_type"])
        finally:
            db.close()

        renew_calls: list[tuple[str, str]] = []

        def fake_renew(
            _db: Database,
            renewed_task_key: str,
            worker_id: str,
            **_kwargs: object,
        ) -> bool:
            renew_calls.append((renewed_task_key, worker_id))
            return True

        with (
            patch.object(
                Database,
                "renew_scheduled_task_lease",
                autospec=True,
                side_effect=fake_renew,
            ),
            patch.object(
                self.controller,
                "_dispatch_task",
                return_value={"ok": True, "checked": 0, "eligible": 0},
            ),
        ):
            self.controller._execute_claimed_task(
                task,
                gate={"status": "ready", "validCount": 5, "totalCount": 5},
            )

        self.assertTrue(renew_calls)
        self.assertEqual((TASK_STALE_LISTING_RECHECK, self.controller.worker_id), renew_calls[0])

    def test_stale_listing_recheck_reschedules_one_day_after_every_run(self) -> None:
        """A maintenance run with no eligible rows still gets its next daily slot."""

        self._confirm_migration()
        self._move_all_tasks_to_future(except_key=TASK_STALE_LISTING_RECHECK)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                next_attempt_at=utc_now_iso(),
            )
            claimed = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )
            self.assertEqual(1, len(claimed))
            task = dict(claimed[0])
            self.assertEqual(TASK_STALE_LISTING_RECHECK, task["task_type"])
        finally:
            db.close()

        before = datetime.now(timezone.utc)
        self.controller._reschedule_after_task(
            task,
            result={"ok": True, "checked": 0, "eligible": 0},
        )

        db = self._open_db()
        try:
            scheduled = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(scheduled)
            assert scheduled is not None
            self.assertEqual("pending", scheduled["status"])
            next_attempt = datetime.fromisoformat(str(scheduled["next_attempt_at"]))
            if next_attempt.tzinfo is None:
                next_attempt = next_attempt.replace(tzinfo=timezone.utc)
            self.assertGreaterEqual(
                next_attempt.astimezone(timezone.utc),
                before + timedelta(seconds=86398.0),
            )
            self.assertLessEqual(
                next_attempt.astimezone(timezone.utc),
                datetime.now(timezone.utc) + timedelta(seconds=86402.0),
            )
            self.assertEqual(0, int(scheduled["priority"]))
        finally:
            db.close()

    def test_stale_listing_recheck_gates_reschedule_without_dispatch(self) -> None:
        """Every unavailable prerequisite only delays the maintenance task."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")

        cases = (
            (
                "c5_circuit",
                {"status": "ready", "validCount": 5, "totalCount": 5},
                "c5_ip_whitelist_circuit_open",
                "circuit",
            ),
            (
                "steam_scheduler",
                {"status": "ready", "validCount": 5, "totalCount": 5},
                "steam_scheduler_unavailable",
                "scheduler",
            ),
            (
                "cookie_gate",
                {"status": "preparing", "validCount": 4, "totalCount": 5},
                "cookie_gate_preparing",
                "cookie",
            ),
            (
                "executor_disabled",
                {"status": "ready", "validCount": 5, "totalCount": 5},
                "executor_disabled",
                "disabled",
            ),
        )

        for name, gate, expected_error, condition in cases:
            with self.subTest(name=name):
                self._set_runtime(
                    RUNTIME_GUADAO,
                    enabled=condition != "disabled",
                    runtime_status=("running" if condition != "disabled" else "stopped"),
                )
                task = self._claim_stale_listing_recheck_task()
                patches = []
                if condition == "circuit":
                    patches.append(
                        patch(
                            "cs2_assistant.services.runtime_controller.is_c5_ip_circuit_open",
                            return_value=True,
                        )
                    )
                if condition == "scheduler":
                    patches.append(
                        patch.object(self.controller, "_steam_scheduler_ready", False)
                    )
                try:
                    for item in patches:
                        item.start()
                    with patch.object(self.controller, "_dispatch_task") as dispatch:
                        self.controller._execute_claimed_task(task, gate=gate)
                    dispatch.assert_not_called()
                finally:
                    for item in reversed(patches):
                        item.stop()

                db = self._open_db()
                try:
                    scheduled = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
                    self.assertIsNotNone(scheduled)
                    assert scheduled is not None
                    self.assertEqual("pending", scheduled["status"])
                    self.assertEqual(expected_error, scheduled["last_error"])
                    self.assertIsNone(scheduled["lease_owner"])
                    self.assertIsNone(scheduled["lease_expires_at"])
                    next_attempt = datetime.fromisoformat(str(scheduled["next_attempt_at"]))
                    if next_attempt.tzinfo is None:
                        next_attempt = next_attempt.replace(tzinfo=timezone.utc)
                    self.assertGreater(next_attempt, datetime.now(timezone.utc))
                finally:
                    db.close()

    def test_stale_listing_recheck_dispatch_binds_task_key_for_action_guard(self) -> None:
        self._confirm_migration()
        task_key = TASK_STALE_LISTING_RECHECK
        task = {
            "task_key": task_key,
            "task_type": TASK_STALE_LISTING_RECHECK,
            "source": RUNTIME_GUADAO,
            "account_id": None,
            "operation_id": None,
        }
        guard_box: dict[str, object] = {}

        class FakeEngine:
            def __init__(self, _settings: Settings, *, new_action_guard=None) -> None:
                guard_box["guard"] = new_action_guard

            def run_guadao_stale_listing_recheck_task(self) -> dict[str, object]:
                guard = guard_box["guard"]
                assert callable(guard)
                return {"ok": bool(guard())}

            def close(self) -> None:
                return None

        with (
            patch(
                "cs2_assistant.services.runtime_controller.ExecutionEngine",
                FakeEngine,
            ),
            patch.object(self.controller, "_manual_task_lease_owned", return_value=True),
            patch.object(self.controller, "_new_actions_enabled", return_value=True),
            patch.object(self.controller, "_emit_guadao_runtime_event"),
        ):
            result = self.controller._dispatch_task(task, enabled=True)

        self.assertTrue(result["ok"])

    def test_stale_listing_recheck_now_requires_explicit_confirmation_and_authorizes_one_run(self) -> None:
        """A maintenance-only authorization must not toggle the full executor."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=False, runtime_status="closing_only")

        with patch.object(self.controller, "wake") as wake:
            with self.assertRaises(RuntimeError):
                self.controller.stale_listing_recheck_now(confirmed=False)
            result = self.controller.stale_listing_recheck_now(confirmed=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertFalse(result["fullExecutorEnabled"])
        self.assertTrue(result["maintenanceOnly"])
        wake.assert_called_once()

        db = self._open_db()
        try:
            task = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual("pending", task["status"])
            self.assertEqual(RUNTIME_GUADAO, task["source"])
            self.assertEqual(TASK_STALE_LISTING_RECHECK, task["task_type"])
            payload = json.loads(str(task["payload_json"] or "{}"))
            authorization = payload.get("staleMaintenanceAuthorization")
            self.assertEqual("single_run", authorization.get("mode"))
            self.assertTrue(authorization.get("requestId"))
            self.assertTrue(authorization.get("expiresAt"))
        finally:
            db.close()

    def test_stale_listing_recheck_now_allows_partial_cookie_gate(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        db = self._open_db()
        try:
            failed = self.accounts[-1]
            db.upsert_steam_cookie_health(
                failed.id,
                account_name=failed.name,
                steam_id=failed.steam_id64,
                status="invalid",
                failure_count=1,
                last_error="401",
                next_retry_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            )
        finally:
            db.close()

        with patch.object(self.controller, "wake") as wake:
            result = self.controller.stale_listing_recheck_now(confirmed=True)

        self.assertTrue(result["queued"])
        self.assertTrue(result["fullExecutorEnabled"])
        wake.assert_called_once()

    def test_stale_listing_recheck_maintenance_authorization_can_dispatch_when_disabled(self) -> None:
        """The one-shot lane permits only stale cancellation, not normal guadao."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=False, runtime_status="closing_only")
        self.controller.stale_listing_recheck_now(confirmed=True)

        db = self._open_db()
        try:
            claimed = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )
            self.assertEqual(1, len(claimed))
            task = dict(claimed[0])
        finally:
            db.close()

        guard_box: dict[str, object] = {}

        class FakeEngine:
            def __init__(self, _settings: Settings, *, new_action_guard=None) -> None:
                guard_box["guard"] = new_action_guard

            def run_guadao_stale_listing_recheck_task(self) -> dict[str, object]:
                guard = guard_box["guard"]
                assert callable(guard)
                return {"ok": bool(guard()), "removed": 0}

            def close(self) -> None:
                return None

        with (
            patch(
                "cs2_assistant.services.runtime_controller.ExecutionEngine",
                FakeEngine,
            ),
            patch.object(self.controller, "_emit_guadao_runtime_event"),
        ):
            self.controller._execute_claimed_task(
                task,
                gate={"status": "ready", "validCount": 5, "totalCount": 5},
            )

        db = self._open_db()
        try:
            scheduled = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(scheduled)
            assert scheduled is not None
            self.assertEqual("pending", scheduled["status"])
            payload = json.loads(str(scheduled["payload_json"] or "{}"))
            self.assertNotIn("staleMaintenanceAuthorization", payload)
            self.assertEqual("completed", scheduled["last_error"] or "completed")
        finally:
            db.close()

    def test_stale_listing_recheck_dispatch_error_releases_running_lease(self) -> None:
        """A failed maintenance dispatch is retryable and never leaves a lease stuck."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        task = self._claim_stale_listing_recheck_task()

        with patch.object(
            self.controller,
            "_dispatch_task",
            side_effect=RuntimeError("stale walk exploded"),
        ) as dispatch:
            self.controller._execute_claimed_task(
                task,
                gate={"status": "ready", "validCount": 5, "totalCount": 5},
            )
        dispatch.assert_called_once()

        db = self._open_db()
        try:
            scheduled = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            self.assertIsNotNone(scheduled)
            assert scheduled is not None
            self.assertIn(scheduled["status"], {"pending", "retry"})
            self.assertIn("stale walk exploded", str(scheduled["last_error"]))
            self.assertIsNone(scheduled["lease_owner"])
            self.assertIsNone(scheduled["lease_expires_at"])
            self.assertEqual(1, int(scheduled["attempt_count"]))
        finally:
            db.close()

    def test_tick_claims_stale_listing_recheck_ahead_of_large_rebuy_backlog(self) -> None:
        """P1 rebuy volume must not starve the independent P0 maintenance task."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        self._move_all_tasks_to_future()
        now = datetime.now(timezone.utc)
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                next_attempt_at=now.isoformat(),
            )
            for index in range(75):
                db.upsert_scheduled_task(
                    f"test-rebuy-backlog-{index}",
                    source=RUNTIME_GUADAO,
                    task_type=TASK_REBUY_ATTEMPT,
                    next_attempt_at=now.isoformat(),
                    operation_id=900_000 + index,
                    priority=1,
                )
        finally:
            db.close()

        with patch.object(
            self.controller,
            "_dispatch_task",
            return_value={"ok": True, "checked": 0, "eligible": 0},
        ) as dispatch:
            result = self.controller.tick(max_tasks=1)

        self.assertEqual([TASK_STALE_LISTING_RECHECK], result["processed"])
        dispatch.assert_called_once()
        self.assertEqual(
            TASK_STALE_LISTING_RECHECK,
            dispatch.call_args.args[0]["task_type"],
        )
        db = self._open_db()
        try:
            stale = db.get_scheduled_task(TASK_STALE_LISTING_RECHECK)
            backlog = db.get_scheduled_task("test-rebuy-backlog-0")
            self.assertIsNotNone(stale)
            self.assertIsNotNone(backlog)
            assert stale is not None and backlog is not None
            self.assertEqual("pending", stale["status"])
            self.assertEqual(0, int(stale["priority"]))
            self.assertEqual("pending", backlog["status"])
            self.assertEqual(0, int(backlog["attempt_count"]))
        finally:
            db.close()

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

    def test_guadao_scan_result_is_persisted_for_overview_summaries(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
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
        scan_round = {
            "generatedAt": utc_now_iso(),
            "evaluatedCount": 12,
            "candidateCount": 3,
            "executableCount": 2,
            "listedCount": 1,
            "items": [],
        }
        result = {
            "ok": True,
            "evaluated": 12,
            "candidateCount": 3,
            "executableCount": 2,
            "listed": 1,
            "scanRound": scan_round,
        }
        with patch.object(self.controller, "_dispatch_task", return_value=result):
            self.controller._execute_claimed_task(
                task,
                gate={"status": "ready", "validCount": 5, "totalCount": 5},
            )

        dashboard = self.controller.dashboard()
        self.assertEqual(scan_round, dashboard["scanRounds"][0])
        self.assertEqual(TASK_GUADAO_SCAN, dashboard["recentTaskRuns"][0]["taskType"])
        self.assertEqual(
            "评估 12 个，挂刀候选 3 个，本地可执行 2 个，新上架 1 件",
            dashboard["recentTaskRuns"][0]["summary"],
        )
        self.assertNotIn("scanRound", dashboard["recentTaskRuns"][0]["result"])

    def test_dashboard_always_exposes_stale_listing_recheck_task(self) -> None:
        """The global hourly maintenance task must not be hidden by old queue rows."""

        db = self._open_db()
        try:
            future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            for index in range(600):
                db.ensure_scheduled_task(
                    f"guadao:history-task:{index}",
                    source=RUNTIME_GUADAO,
                    task_type=TASK_REBUY_ATTEMPT,
                    next_attempt_at=future,
                    operation_id=index + 1,
                    payload={},
                    status="waiting",
                    priority=2,
                )
        finally:
            db.close()

        dashboard = self.controller.dashboard()
        task_keys = {str(task.get("taskKey") or "") for task in dashboard["tasks"]}
        self.assertIn(TASK_STALE_LISTING_RECHECK, task_keys)

    def test_tick_runs_an_overdue_steam_sync_despite_107_due_rebuys(self) -> None:
        """A perpetual P1 rebuy backlog cannot delay sale evidence past 60 seconds."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        self._move_all_tasks_to_future()
        overdue_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        sync_key = f"steam-sync:{self.accounts[0].id}"
        db = self._open_db()
        try:
            db.upsert_scheduled_task(
                sync_key,
                source=RUNTIME_GUADAO,
                task_type=TASK_STEAM_ACCOUNT_SYNC,
                next_attempt_at=overdue_at,
                account_id=self.accounts[0].id,
                priority=2,
            )
            for index in range(107):
                db.upsert_scheduled_task(
                    f"test-due-rebuy-backlog-{index}",
                    source=RUNTIME_GUADAO,
                    task_type=TASK_REBUY_ATTEMPT,
                    next_attempt_at=utc_now_iso(),
                    operation_id=910_000 + index,
                    priority=1,
                )
        finally:
            db.close()

        with patch.object(
            self.controller,
            "_dispatch_task",
            return_value={"ok": True, "sold": 0, "rebuy": 0},
        ) as dispatch:
            result = self.controller.tick(max_tasks=1)

        self.assertEqual([sync_key], result["processed"])
        dispatch.assert_called_once()
        self.assertEqual(
            TASK_STEAM_ACCOUNT_SYNC,
            dispatch.call_args.args[0]["task_type"],
        )
        db = self._open_db()
        try:
            sync = db.get_scheduled_task(sync_key)
            rebuy = db.get_scheduled_task("test-due-rebuy-backlog-0")
            self.assertIsNotNone(sync)
            self.assertIsNotNone(rebuy)
            assert sync is not None and rebuy is not None
            self.assertEqual("pending", sync["status"])
            self.assertEqual(1, int(sync["attempt_count"]))
            self.assertEqual("pending", rebuy["status"])
            self.assertEqual(0, int(rebuy["attempt_count"]))
        finally:
            db.close()

    def test_seed_replaces_107_pending_rebuy_claims_with_item_batch_tasks(self) -> None:
        """Per-operation retry clocks remain waiting; only categories are P1 work."""

        db = self._open_db()
        try:
            for index in range(107):
                name = (
                    "Revolution Case"
                    if index < 50
                    else "Kilowatt Case"
                    if index < 90
                    else "Fever Case"
                )
                db.add_pool_operation(
                    market_hash_name=name,
                    strategy="guadao",
                    operation_type=OP_REBUY_C5,
                    expected_price=1.50,
                    note=json.dumps({"sourceSellOperationId": index + 1}),
                )
            self.controller._seed_tasks(db)
            individual = db.list_scheduled_tasks(
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_ATTEMPT,
                status="waiting",
                limit=200,
            )
            batches = db.list_scheduled_tasks(
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_BATCH,
                status="pending",
                limit=20,
            )
            claimable_individual = db.list_scheduled_tasks(
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_ATTEMPT,
                status="pending",
                limit=200,
            )
        finally:
            db.close()

        self.assertEqual(107, len(individual))
        self.assertEqual(3, len(batches))
        self.assertEqual([], claimable_individual)

    def test_profit_dispatch_guard_observes_disable_after_task_was_claimed(self) -> None:
        self._set_runtime(
            RUNTIME_PROFIT_TRADE,
            enabled=True,
            runtime_status="running",
        )
        observed: list[bool] = []

        def fake_run(
            _settings: Settings,
            *,
            config: StrategyConfig,
            new_action_guard: object,
        ) -> object:
            self._set_runtime(
                RUNTIME_PROFIT_TRADE,
                enabled=False,
                runtime_status="closing_only",
            )
            self.assertTrue(config.profit_trade_enabled)
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

    def test_manual_profit_task_renews_its_lease_before_dispatch(self) -> None:
        """A multi-item manual batch must not run on the original 180s lease alone."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(
            RUNTIME_PROFIT_TRADE,
            enabled=True,
            runtime_status="running",
        )
        self._move_all_tasks_to_future()
        config = load_strategy_config(self.settings)
        config.profit_trade_enabled = True
        config.profit_trade_allow_real_execution = True
        save_strategy_config(self.settings, config)

        task_key = "profit-manual:test-lease-renewal"
        db = self._open_db()
        try:
            db.ensure_scheduled_task(
                task_key,
                source=RUNTIME_PROFIT_TRADE,
                task_type=TASK_PROFIT_MANUAL_EXECUTION,
                next_attempt_at=utc_now_iso(),
                priority=1,
                payload={
                    "requestId": "PTMAN-test-lease-renewal",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "quantity": 2,
                    "approvedExpectedRoi": 0.08,
                    "requestedAt": utc_now_iso(),
                },
            )
            task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_PROFIT_TRADE,
                limit=1,
                lease_seconds=180,
            )[0]
        finally:
            db.close()

        renew_calls: list[tuple[str, str]] = []

        def fake_renew(
            _db: Database,
            renewed_task_key: str,
            worker_id: str,
            **_kwargs: object,
        ) -> bool:
            renew_calls.append((renewed_task_key, worker_id))
            return True

        with (
            patch.object(
                Database,
                "renew_scheduled_task_lease",
                autospec=True,
                side_effect=fake_renew,
            ),
            patch.object(
                self.controller,
                "_dispatch_task",
                return_value={
                    "ok": True,
                    "summary": "manual batch completed",
                    "requestId": "PTMAN-test-lease-renewal",
                },
            ),
        ):
            self.controller._execute_claimed_task(
                task,
                gate={"status": "ready", "validCount": 5, "totalCount": 5},
            )

        self.assertTrue(
            renew_calls,
            "manual execution must renew its scheduled-task lease before entering a long dispatch",
        )
        self.assertEqual((task_key, self.controller.worker_id), renew_calls[0])

    def test_manual_profit_guard_stops_new_actions_after_task_lease_is_lost(self) -> None:
        """Losing ownership must close the new-action gate before the next item."""

        self._confirm_migration()
        self._set_runtime(
            RUNTIME_PROFIT_TRADE,
            enabled=True,
            runtime_status="running",
        )
        self._move_all_tasks_to_future()
        config = load_strategy_config(self.settings)
        config.profit_trade_enabled = True
        config.profit_trade_allow_real_execution = True
        save_strategy_config(self.settings, config)

        task_key = "profit-manual:test-lost-lease"
        db = self._open_db()
        try:
            db.ensure_scheduled_task(
                task_key,
                source=RUNTIME_PROFIT_TRADE,
                task_type=TASK_PROFIT_MANUAL_EXECUTION,
                next_attempt_at=utc_now_iso(),
                priority=1,
                payload={
                    "requestId": "PTMAN-test-lost-lease",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "quantity": 2,
                    "approvedExpectedRoi": 0.08,
                    "requestedAt": utc_now_iso(),
                },
            )
            task = dict(
                db.claim_due_scheduled_tasks(
                    self.controller.worker_id,
                    source=RUNTIME_PROFIT_TRADE,
                    limit=1,
                    lease_seconds=180,
                )[0]
            )
        finally:
            db.close()

        guard_results: list[bool] = []

        def fake_manual_execute(
            _settings: Settings,
            **kwargs: object,
        ) -> dict[str, object]:
            new_action_guard = kwargs["new_action_guard"]
            self.assertTrue(callable(new_action_guard))
            guard_results.append(bool(new_action_guard()))

            lease_db = self._open_db()
            try:
                lease_db.conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET lease_owner = ?, lease_expires_at = ?
                    WHERE task_key = ?
                    """,
                    (
                        "replacement-worker",
                        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                        task_key,
                    ),
                )
                lease_db.conn.commit()
            finally:
                lease_db.close()

            # This represents the guard check immediately before item two.
            guard_results.append(bool(new_action_guard()))
            return {"ok": False, "errors": ["lease ownership lost"]}

        with patch(
            "cs2_assistant.services.runtime_controller.execute_manual_profit_trade_request",
            side_effect=fake_manual_execute,
        ):
            self.controller._dispatch_task(task, enabled=True)

        self.assertEqual(
            [True, False],
            guard_results,
            "the next Steam purchase must be blocked as soon as the manual task loses its lease",
        )

    def test_terminal_manual_profit_task_updates_runtime_recent_runs(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()
        self._set_runtime(
            RUNTIME_PROFIT_TRADE,
            enabled=True,
            runtime_status="running",
        )
        self._move_all_tasks_to_future()

        task_key = "profit-manual:test-terminal-runtime-summary"
        request_id = "PTMAN-test-terminal-runtime-summary"
        task_result = {
            "ok": True,
            "requestId": request_id,
            "marketHashName": "AK-47 | Redline (Field-Tested)",
            "requestedQuantity": 2,
            "boughtTradeIds": [101, 102],
            "listedTradeIds": [101, 102],
            "skippedTradeIds": [],
            "errors": [],
            "summary": "manual batch bought 2 and listed 2",
        }
        db = self._open_db()
        try:
            db.ensure_scheduled_task(
                task_key,
                source=RUNTIME_PROFIT_TRADE,
                task_type=TASK_PROFIT_MANUAL_EXECUTION,
                next_attempt_at=utc_now_iso(),
                priority=1,
                payload={
                    "requestId": request_id,
                    "marketHashName": task_result["marketHashName"],
                    "quantity": 2,
                    "approvedExpectedRoi": 0.08,
                    "requestedAt": utc_now_iso(),
                },
            )
            task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_PROFIT_TRADE,
                limit=1,
                lease_seconds=180,
            )[0]
        finally:
            db.close()

        with patch.object(self.controller, "_dispatch_task", return_value=task_result):
            self.controller._execute_claimed_task(
                task,
                gate={"status": "ready", "validCount": 5, "totalCount": 5},
            )

        db = self._open_db()
        try:
            runtime = db.get_executor_runtime_state(RUNTIME_PROFIT_TRADE)
            payload = json.loads(str(runtime["payload_json"] or "{}"))
            scheduled = db.get_scheduled_task(task_key)
        finally:
            db.close()

        self.assertEqual("completed", scheduled["status"])
        self.assertTrue(payload.get("lastRunAt"))
        self.assertIn("manual batch", str(payload.get("lastRunSummary") or ""))
        recent_runs = list(payload.get("recentTaskRuns") or [])
        self.assertTrue(recent_runs, "terminal manual execution must be visible in the runtime panel")
        self.assertEqual(TASK_PROFIT_MANUAL_EXECUTION, recent_runs[0]["taskType"])
        self.assertEqual(task_key, recent_runs[0]["taskKey"])
        self.assertEqual(request_id, recent_runs[0]["result"]["requestId"])

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

    def test_enabling_executor_reuses_cached_valid_cookies_without_age_refresh(self) -> None:
        self._confirm_migration()
        old_validated_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        db = self._open_db()
        try:
            for account in self.accounts:
                db.upsert_steam_cookie_health(
                    account.id,
                    account_name=account.name,
                    steam_id=account.steam_id64,
                    status="valid",
                    batch_id="cached-cookie-batch",
                    failure_count=0,
                    last_validated_at=old_validated_at,
                )
        finally:
            db.close()

        runtime = self.controller.toggle_executor(RUNTIME_GUADAO, True)
        self.assertEqual("running", runtime["runtimeStatus"])

        db = self._open_db()
        try:
            rows = db.list_steam_cookie_health()
            self.assertEqual(["valid"] * len(self.accounts), [str(row["status"]) for row in rows])
            self.assertEqual(
                ["cached-cookie-batch"] * len(self.accounts),
                [str(row["batch_id"]) for row in rows],
            )
            with patch.object(self.controller, "_refresh_cookie_account") as refresh:
                snapshot = self.controller._cookie_gate_tick(db)
            refresh.assert_not_called()
            self.assertEqual("ready", snapshot["status"])
        finally:
            db.close()

    def test_explicit_refresh_all_still_invalidates_every_cached_cookie(self) -> None:
        self._confirm_migration()
        self._mark_all_cookies_valid()

        result = self.controller.refresh_all_cookies_now()

        db = self._open_db()
        try:
            rows = db.list_steam_cookie_health()
        finally:
            db.close()
        self.assertTrue(result["ok"])
        self.assertEqual(["unknown"] * len(self.accounts), [str(row["status"]) for row in rows])
        self.assertTrue(all(str(row["batch_id"]) == result["batchId"] for row in rows))

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

    def test_disabled_profit_selection_reuses_healthy_idle_cookie_gate(self) -> None:
        """Research P3 may use an already-healthy cookie without turning on Profit Trade."""

        self._confirm_migration()
        self._mark_all_cookies_valid()
        db = self._open_db()
        try:
            db.add_profit_trade_selection_watch(
                "AK-47 | Selection Cookie Test (Field-Tested)",
                name_cn="选品 Cookie 测试",
            )
            self.controller._seed_tasks(db)
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            for scheduled in db.list_scheduled_tasks(limit=1000):
                if str(scheduled["task_key"]) != TASK_PROFIT_SELECTION_WATCH:
                    db.reschedule_scheduled_task(
                        str(scheduled["task_key"]), next_attempt_at=future
                    )
            db.reschedule_scheduled_task(
                TASK_PROFIT_SELECTION_WATCH,
                next_attempt_at=utc_now_iso(),
            )
            with patch.object(self.controller, "_refresh_cookie_account") as refresh:
                gate = self.controller._cookie_gate_tick(db)
            self.assertEqual("idle", gate["status"])
            self.assertEqual(len(self.accounts), gate["validCount"])
            refresh.assert_not_called()
            task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_PROFIT_TRADE,
                limit=1,
            )[0]
        finally:
            db.close()

        next_due_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with patch.object(
            self.controller,
            "_dispatch_task",
            return_value={"ok": True, "activeCount": 1, "nextDueAt": next_due_at},
        ) as dispatch:
            self.controller._execute_claimed_task(task, gate=gate)

        dispatch.assert_called_once()
        self.assertFalse(dispatch.call_args.kwargs["enabled"])
        self.assertEqual(
            TASK_PROFIT_SELECTION_WATCH,
            dispatch.call_args.args[0]["task_type"],
        )

    def test_disabled_profit_selection_without_healthy_cookie_never_relogins(self) -> None:
        """Selection waits for user/executor cookie refresh; it does not create one."""

        self._confirm_migration()
        retry_at = (
            datetime.now(timezone.utc) + timedelta(minutes=45)
        ).replace(microsecond=0).isoformat()
        db = self._open_db()
        try:
            db.add_profit_trade_selection_watch(
                "M4A1-S | Selection Cookie Missing (Field-Tested)",
                name_cn="选品 Cookie 缺失",
            )
            # Make the existing health schedule explicit so the task must honor
            # it instead of retrying at the previous five-second gate cadence.
            db.upsert_steam_cookie_health(
                self.accounts[0].id,
                account_name=self.accounts[0].name,
                steam_id=self.accounts[0].steam_id64,
                status="invalid",
                next_retry_at=retry_at,
            )
            self.controller._seed_tasks(db)
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            for scheduled in db.list_scheduled_tasks(limit=1000):
                if str(scheduled["task_key"]) != TASK_PROFIT_SELECTION_WATCH:
                    db.reschedule_scheduled_task(
                        str(scheduled["task_key"]), next_attempt_at=future
                    )
            db.reschedule_scheduled_task(
                TASK_PROFIT_SELECTION_WATCH,
                next_attempt_at=utc_now_iso(),
            )
            with patch.object(self.controller, "_refresh_cookie_account") as refresh:
                gate = self.controller._cookie_gate_tick(db)
            self.assertEqual("idle", gate["status"])
            refresh.assert_not_called()
            task = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_PROFIT_TRADE,
                limit=1,
            )[0]
        finally:
            db.close()

        with patch.object(self.controller, "_dispatch_task") as dispatch:
            self.controller._execute_claimed_task(task, gate=gate)
        dispatch.assert_not_called()

        db = self._open_db()
        try:
            waiting = db.get_scheduled_task(TASK_PROFIT_SELECTION_WATCH)
        finally:
            db.close()
        self.assertEqual("selection_cookie_unavailable", waiting["last_error"])
        self.assertGreaterEqual(
            datetime.fromisoformat(str(waiting["next_attempt_at"])),
            datetime.fromisoformat(retry_at),
        )

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

    def test_c5_delivery_overdue_does_not_fail_without_explicit_remote_failure(self) -> None:
        engine = ExecutionEngine(self.settings)
        try:
            engine.config.dry_run = False
            engine.config.auto_rebuy_enabled = True
            submitted = datetime.now(timezone.utc) - timedelta(hours=13)
            note = {
                "c5OutTradeNo": "OUT-24H",
                "c5OrderId": "ORDER-24H",
                "c5TradeOrderId": "TRADE-24H",
                "c5PayStatus": 1,
                C5_DELIVERY_STATUS_KEY: "pending",
                "c5OrderSubmittedAt": submitted.isoformat(),
                "c5DeliveryDeadlineAt": (submitted + timedelta(hours=12)).isoformat(),
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
            self.assertEqual("delivery_pending", original["status"])
            self.assertEqual("pending", original_note[C5_DELIVERY_STATUS_KEY])
            self.assertTrue(original_note["c5DeliveryOverdue"])
            self.assertEqual(0, len(replacements))
            self.assertEqual(0, first["replacements"])
            self.assertEqual(0, second["replacements"])
        finally:
            engine.close()

    def test_delivery_confirmation_task_waits_for_startup_grace(self) -> None:
        self._confirm_migration()
        self._set_runtime(RUNTIME_GUADAO, enabled=True, runtime_status="running")
        db = self._open_db()
        try:
            db.reschedule_scheduled_task(
                TASK_STALE_LISTING_RECHECK,
                next_attempt_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
            task = db.get_scheduled_task(TASK_GUADAO_SCAN)
            db.upsert_scheduled_task(
                "delivery:startup-grace",
                source=RUNTIME_GUADAO,
                task_type=TASK_C5_DELIVERY_CONFIRM,
                next_attempt_at=utc_now_iso(),
                operation_id=999999,
                priority=1,
            )
            claimed = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_GUADAO,
                limit=1,
            )[0]
        finally:
            db.close()

        with patch.object(self.controller, "_dispatch_task") as dispatch:
            self.controller._execute_claimed_task(claimed, gate={"status": "ready"})

        dispatch.assert_not_called()
        db = self._open_db()
        try:
            delayed = db.get_scheduled_task("delivery:startup-grace")
        finally:
            db.close()
        self.assertIn(delayed["status"], {"pending", "retry"})
        self.assertGreater(
            datetime.fromisoformat(delayed["next_attempt_at"]),
            datetime.now(timezone.utc),
        )

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
                if task_type == TASK_REBUY_BATCH:
                    payload = json.loads(str(task["payload_json"] or "{}"))
                    self.assertEqual("Kilowatt Case", payload["marketHashName"])
                    db.update_pool_operation(created_rebuy[0], status="completed")
                    return {
                        "ok": True,
                        "marketHashName": "Kilowatt Case",
                        "successes": 1,
                    }
                self.fail(f"unexpected task dispatched: {task_type}")
            finally:
                db.close()

        with patch.object(self.controller, "_dispatch_task", side_effect=dispatch):
            result = self.controller.tick(max_tasks=2)

        self.assertEqual(
            [TASK_STEAM_ACCOUNT_SYNC, TASK_REBUY_BATCH],
            dispatched,
        )
        self.assertEqual(2, len(result["processed"]))
        db = self._open_db()
        try:
            rebuy_task = db.get_scheduled_task(f"rebuy:{created_rebuy[0]}")
            batch_task = db.get_scheduled_task(
                self.controller._rebuy_batch_task_key("Kilowatt Case")
            )
            self.assertIsNone(rebuy_task)
            self.assertIsNone(batch_task)
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
                        "itemScope": "crates_only",
                        "autoListing": False,
                        "autoRebuy": True,
                    },
                    "specialRules": [],
                    "timePolicy": {
                        "scanMinutes": 6,
                        "steamSyncSeconds": 150,
                        "steamSyncMaxStartLagSeconds": 75,
                        "staleListedCheckHours": 24,
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
        self.assertEqual("crates_only", settings["global"]["itemScope"])
        self.assertEqual("crates_only", settings["guadaoItemScope"])
        self.assertFalse(settings["global"]["autoListing"])
        self.assertTrue(settings["global"]["autoRebuy"])
        self.assertEqual(6, settings["timePolicy"]["scanMinutes"])
        self.assertEqual(150, settings["timePolicy"]["steamSyncSeconds"])
        self.assertEqual(75, settings["timePolicy"]["steamSyncMaxStartLagSeconds"])
        self.assertEqual(24, settings["timePolicy"]["staleListedCheckHours"])
        self.assertEqual([1, 3, 10], settings["timePolicy"]["rebuyMinutes"])
        self.assertEqual([1, 5, 15, 30], settings["timePolicy"]["deliveryMinutes"])
        self.assertAlmostEqual(
            1.75,
            settings["timePolicy"]["staleListedMaxRatioTolerancePct"],
        )
        self.assertAlmostEqual(0.725, holder["config"].guadao_max_listing_ratio)
        self.assertEqual("crates_only", holder["config"].guadao_item_scope)
        self.assertEqual(360.0, holder["config"].guadao_task_schedule["scanIntervalSeconds"])
        self.assertEqual(
            75.0,
            holder["config"].guadao_task_schedule["steamSyncMaxStartLagSeconds"],
        )
        self.assertEqual(
            86400.0,
            holder["config"].guadao_task_schedule["staleListedCheckIntervalSeconds"],
        )

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

    def test_special_ratio_rule_can_be_stricter_than_global_ratio(self) -> None:
        market_hash_name = "Kilowatt Case"
        db = self._open_db()
        try:
            db.upsert_items(
                [
                    CatalogItem(
                        market_hash_name=market_hash_name,
                        name_cn="千瓦武器箱",
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
                        "maxListingRatioPct": 69,
                        "steamNetFactorPct": 86.9,
                        "maxNewListingsPerCycle": 5,
                        "caseMaxOpenCount": 100,
                        "autoListing": True,
                        "autoRebuy": True,
                    },
                    "specialRules": [
                        {
                            "marketHashName": market_hash_name,
                            "displayName": "千瓦武器箱",
                            "maxRatioPct": 68,
                            "enabled": True,
                            "version": 1,
                        }
                    ],
                }
            )

        rule = updated["settings"]["specialRules"][0]
        self.assertEqual(market_hash_name, rule["marketHashName"])
        self.assertAlmostEqual(68.0, rule["maxRatioPct"])
        self.assertAlmostEqual(
            0.68,
            holder["config"].guadao_special_ratio_rules[0]["maxListingRatio"],
        )

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
        self.assertEqual(1, result["pagination"]["total"])
        self.assertFalse(result["pagination"]["hasMore"])

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

    def test_issues_only_includes_submission_unconfirmed_after_slow_retry_alert(self) -> None:
        db = self._open_db()
        try:
            ordinary_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.02,
                note=json.dumps({"c5OutTradeNo": "OUT-ORDINARY"}),
            )
            db.update_pool_operation(
                ordinary_id,
                status="c5_submission_unconfirmed",
            )
            alerted_at = utc_now_iso()
            alerted_id = db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.58,
                note=json.dumps(
                    {
                        "c5OutTradeNo": "OUT-SLOW-RETRY",
                        "c5SubmissionCoverageAlertAt": alerted_at,
                        "c5SubmissionReconcileAlertCode": (
                            "reconcile_fast_attempts_exhausted"
                        ),
                        "c5SubmissionReconcileAttemptCount": 5,
                    }
                ),
            )
            db.update_pool_operation(
                alerted_id,
                status="c5_submission_unconfirmed",
            )
        finally:
            db.close()

        issues = self.controller.issues()

        issue_by_operation = {row["operationId"]: row for row in issues["items"]}
        self.assertNotIn(ordinary_id, issue_by_operation)
        self.assertIn(alerted_id, issue_by_operation)
        alerted = issue_by_operation[alerted_id]
        self.assertEqual("c5_submission_unconfirmed", alerted["rawStatus"])
        self.assertEqual("c5", alerted["category"])
        self.assertEqual("medium", alerted["severity"])
        self.assertEqual(alerted_at, alerted["firstSeenAt"])

    def test_operations_split_c5_evidence_pending_from_confirmed_purchase_delivery(self) -> None:
        db = self._open_db()
        try:
            db.upsert_items(
                [
                    CatalogItem("Evidence Case", "待查证据箱"),
                    CatalogItem("Delivery Case", "待收货箱"),
                ]
            )
            evidence_sell_id = db.add_pool_operation(
                market_hash_name="Evidence Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                note=json.dumps({"steamAccountName": "steam-1"}),
            )
            db.update_pool_operation(evidence_sell_id, status="sold")
            evidence_rebuy_id = db.add_pool_operation(
                market_hash_name="Evidence Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.58,
                note=json.dumps(
                    {
                        "sourceSellOperationId": evidence_sell_id,
                        "c5OutTradeNo": "OUT-EVIDENCE-ONLY",
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(
                evidence_rebuy_id,
                status=C5_SUBMISSION_UNCONFIRMED_STATUS,
                actual_price=1.58,
            )

            delivery_sell_id = db.add_pool_operation(
                market_hash_name="Delivery Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                note=json.dumps({"steamAccountName": "steam-2"}),
            )
            db.update_pool_operation(delivery_sell_id, status="sold")
            delivery_rebuy_id = db.add_pool_operation(
                market_hash_name="Delivery Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.68,
                note=json.dumps(
                    {
                        "sourceSellOperationId": delivery_sell_id,
                        "c5OutTradeNo": "OUT-CONFIRMED",
                        "c5OrderId": "ASSET-ORDER-CONFIRMED",
                        "c5TradeOrderId": "TRADE-ORDER-CONFIRMED",
                        "c5OrderSubmittedAt": utc_now_iso(),
                    }
                ),
            )
            db.update_pool_operation(
                delivery_rebuy_id,
                status="delivery_pending",
                actual_price=1.68,
            )
        finally:
            db.close()

        all_operations = self.controller.operations(page_size=10)
        self.assertEqual(2, all_operations["total"])
        self.assertEqual(1, all_operations["summary"]["c5EvidencePending"])
        self.assertEqual(1, all_operations["summary"]["submissionUnconfirmed"])
        self.assertEqual(1, all_operations["summary"]["deliveryPending"])

        evidence_only = self.controller.operations(
            status=C5_SUBMISSION_UNCONFIRMED_STATUS,
            page_size=10,
        )
        self.assertEqual(1, evidence_only["total"])
        self.assertEqual("Evidence Case", evidence_only["items"][0]["marketHashName"])
        self.assertEqual("C5 补仓待查证据", evidence_only["items"][0]["stage"])
        self.assertIsNone(evidence_only["items"][0]["c5DeliveryDeadlineAt"])
        self.assertEqual(1, evidence_only["summary"]["c5EvidencePending"])
        self.assertEqual(0, evidence_only["summary"]["deliveryPending"])

        delivery_only = self.controller.operations(status="delivery_pending", page_size=10)
        self.assertEqual(1, delivery_only["total"])
        self.assertEqual("Delivery Case", delivery_only["items"][0]["marketHashName"])
        self.assertEqual("C5 已购买待收货", delivery_only["items"][0]["stage"])
        self.assertIsNotNone(delivery_only["items"][0]["c5DeliveryDeadlineAt"])
        self.assertEqual(0, delivery_only["summary"]["c5EvidencePending"])
        self.assertEqual(1, delivery_only["summary"]["deliveryPending"])

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
        self.assertEqual(
            {
                "AK-47 | Alpha",
                "P250 | Charlie",
                "USP-S | Echo",
            },
            {row["marketHashName"] for row in listed["itemOptions"]},
        )
        item = self.controller.operations(
            market_hash_name="AK-47 | Bravo",
            page_size=10,
        )
        self.assertEqual(1, item["total"])
        self.assertEqual("AK-47 | Bravo", item["items"][0]["marketHashName"])
        self.assertEqual("布拉沃", item["items"][0]["displayName"])
        self.assertEqual(
            {
                "AK-47 | Alpha",
                "AK-47 | Bravo",
                "P250 | Charlie",
                "M4A1-S | Delta",
                "USP-S | Echo",
            },
            {row["marketHashName"] for row in item["itemOptions"]},
        )
        empty_combination = self.controller.operations(
            market_hash_name="AK-47 | Bravo",
            status="completed",
            page_size=10,
        )
        self.assertEqual(0, empty_combination["total"])
        self.assertEqual([], empty_combination["itemOptions"])

    def test_operations_item_options_follow_date_range_and_counts(self) -> None:
        db = self._open_db()
        try:
            db.upsert_items(
                [
                    CatalogItem("Old Case", "旧武器箱"),
                    CatalogItem("Current Case", "当前武器箱"),
                ]
            )
            old_id = db.add_pool_operation(
                market_hash_name="Old Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                asset_id="asset-old-date-filter",
            )
            current_ids = [
                db.add_pool_operation(
                    market_hash_name="Current Case",
                    strategy="guadao",
                    operation_type=OP_SELL_STEAM,
                    asset_id=f"asset-current-date-filter-{index}",
                )
                for index in range(2)
            ]
            db.conn.execute(
                "UPDATE pool_operations SET created_at = ? WHERE id = ?",
                ("2026-07-13T12:00:00+00:00", old_id),
            )
            db.conn.executemany(
                "UPDATE pool_operations SET created_at = ? WHERE id = ?",
                [
                    ("2026-07-15T12:00:00+00:00", operation_id)
                    for operation_id in current_ids
                ],
            )
            db.conn.commit()
        finally:
            db.close()

        result = self.controller.operations(
            start_at="2026-07-14T00:00:00+00:00",
            end_at="2026-07-16T00:00:00+00:00",
            page_size=10,
        )

        self.assertEqual(2, result["total"])
        self.assertEqual(
            [
                {
                    "marketHashName": "Current Case",
                    "displayName": "当前武器箱",
                    "count": 2,
                }
            ],
            result["itemOptions"],
        )

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
                        "c5TradeOrderId": "TRADE-DEADLINE",
                        "c5PayStatus": 1,
                        "c5OutTradeNo": "OUT-DEADLINE",
                        "c5DeliveryDeadlineAt": "2026-07-01T00:00:00+00:00",
                    }
                ),
            )
            db.update_pool_operation(rebuy_id, status="delivery_pending")
        finally:
            db.close()

        payload = self.controller.operations(page=1, page_size=10)
        projected = next(row for row in payload["items"] if row["id"] == sell_id)
        self.assertEqual("ORDER-DEADLINE", projected["c5OrderId"])
        self.assertEqual("TRADE-DEADLINE", projected["c5TradeOrderId"])
        self.assertEqual("OUT-DEADLINE", projected["c5OutTradeNo"])
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
        self.assertEqual("2026-07-16T13:02:03+00:00", projected["c5DeliveryDeadlineAt"])

    def test_operations_exposes_failed_rebuy_history_behind_latest_replacement(self) -> None:
        db = self._open_db()
        try:
            db.upsert_items([CatalogItem("Revolution Case", "变革武器箱")])
            sell_id = db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy="guadao",
                operation_type=OP_SELL_STEAM,
                expected_price=2.80,
                asset_id="asset-rebuy-history",
                note=json.dumps(
                    {
                        "listingId": "listing-rebuy-history",
                        "steamSoldAt": "2026-07-17T01:00:00+00:00",
                    }
                ),
            )
            db.update_pool_operation(sell_id, status="sold")
            failed_id = db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.68,
                note=json.dumps(
                    {
                        "sourceSellOperationId": sell_id,
                        "c5OrderId": "C5-FAILED-ORDER",
                        "c5TradeOrderId": "C5-FAILED-ORDER",
                        "c5OrderPayload": {
                            "orderAssetId": "C5-FAILED-ORDER",
                            "orderId": "C5-FAILED-TRADE",
                            "payStatus": 1,
                        },
                        "c5OutTradeNo": "OUT-FAILED",
                        "c5OrderSubmittedAt": "2026-07-17T01:05:00+00:00",
                        "c5OrderCheckedAt": "2026-07-17T02:00:00+00:00",
                        "c5OrderFailedCode": "SEND_ITEMS_NOT_EXISTS",
                        "c5OrderFailedDesc": "饰品不在库存",
                    }
                ),
            )
            replacement_id = db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=1.66,
                note=json.dumps(
                    {
                        "sourceSellOperationId": sell_id,
                        "replacementForRebuyOperationId": failed_id,
                        "replacementReason": "c5_delivery_failed",
                        "replacementMaxPrice": 1.66,
                    }
                ),
            )
            failed = db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (failed_id,)
            ).fetchone()
            failed_note = json.loads(failed["note"])
            failed_note["replacementRebuyOperationId"] = replacement_id
            db.update_pool_operation(
                failed_id,
                status="c5_failed",
                actual_price=1.66,
                note=json.dumps(failed_note),
            )
        finally:
            db.close()

        payload = self.controller.operations(page=1, page_size=10)
        projected = next(row for row in payload["items"] if row["id"] == sell_id)

        self.assertEqual("sold", projected["status"])
        self.assertEqual(2, projected["rebuyAttemptCount"])
        self.assertEqual(1, projected["failedRebuyCount"])
        self.assertTrue(projected["hasPreviousRebuyFailure"])
        self.assertEqual([failed_id, replacement_id], [row["id"] for row in projected["rebuyAttempts"]])
        failed_attempt, current_attempt = projected["rebuyAttempts"]
        self.assertEqual("c5_failed", failed_attempt["status"])
        self.assertEqual("C5-FAILED-ORDER", failed_attempt["c5OrderId"])
        self.assertEqual("C5-FAILED-TRADE", failed_attempt["c5TradeOrderId"])
        self.assertEqual("OUT-FAILED", failed_attempt["c5OutTradeNo"])
        self.assertEqual("饰品不在库存", failed_attempt["failureReason"])
        self.assertEqual(replacement_id, failed_attempt["replacementOperationId"])
        self.assertFalse(failed_attempt["isCurrent"])
        self.assertEqual("pending", current_attempt["status"])
        self.assertEqual(failed_id, current_attempt["replacementForOperationId"])
        self.assertEqual(1.66, current_attempt["replacementMaxPrice"])
        self.assertTrue(current_attempt["isCurrent"])

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
        self.controller._delivery_startup_ready_at = datetime.now(timezone.utc) - timedelta(seconds=1)
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
                        "c5OrderId": "ASSET-SCHEDULER-DOWN",
                        "c5TradeOrderId": "TRADE-SCHEDULER-DOWN",
                        "c5PayStatus": 1,
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

    def test_stale_listing_result_notification_is_aggregated_and_deduplicated(self) -> None:
        self.settings.serverchan_sendkey = "test-only-sendkey"
        sent: list[tuple[str, str]] = []

        class FakeServerChanClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def send(self, title: str, body: str) -> None:
                sent.append((title, body))

        result = {
            "runId": "stale-test-run-1",
            "summary": "账号 1 个 | 到期 2 笔 | 实查 2 笔 | 撤单成功 1 笔",
            "removed": 1,
            "removeFailed": 0,
            "unmatched": 1,
            "priceDeferred": 0,
            "removedOperations": [
                {
                    "operationId": 123,
                    "marketHashName": "Revolution Case",
                    "listingId": "listing-123",
                    "assetId": "asset-123",
                    "reason": "listed more than 48 hours and no longer at market floor",
                }
            ],
            "unmatchedOperations": [
                {
                    "operationId": 124,
                    "marketHashName": "Revolution Case",
                    "listingId": "listing-124",
                    "assetId": "asset-124",
                }
            ],
        }

        with patch(
            "cs2_assistant.services.runtime_controller.ServerChanClient",
            FakeServerChanClient,
        ):
            self.controller._notify_stale_listing_recheck_result(result)
            self.controller._notify_stale_listing_recheck_result(result)

        self.assertEqual(1, len(sent))
        self.assertIn("stale-test-run-1", sent[0][1])
        self.assertIn("operation=123", sent[0][1])
        self.assertIn("未匹配", sent[0][1])
        db = self._open_db()
        try:
            runtime = db.get_executor_runtime_state(RUNTIME_GUADAO)
            payload = json.loads(runtime["payload_json"])
            self.assertIn(
                "stale-listing-recheck:stale-test-run-1",
                payload["notificationEvents"],
            )
        finally:
            db.close()

    def test_batch_refreeze_replaces_current_price_and_ratio_with_append_only_audit(self) -> None:
        sell_id, rebuy_id, _asset_id = self._create_sold_pending_rebuy(
            market_hash_name="Revolution Case",
            suffix="refreeze",
        )

        with patch.object(self.controller, "_emit_guadao_runtime_event"):
            result = self.controller.batch_refreeze_guadao_rebuys(
                [sell_id],
                rebuy_price=7.2,
                execute_now=True,
                confirmed=True,
                request_id="refreeze-request-1",
                reason="test refreeze",
            )
            replay = self.controller.batch_refreeze_guadao_rebuys(
                [sell_id],
                rebuy_price=7.2,
                execute_now=True,
                confirmed=True,
                request_id="refreeze-request-1",
                reason="test refreeze",
            )

        self.assertEqual(1, result["successCount"])
        self.assertEqual(0, result["failedCount"])
        self.assertEqual(7.2, result["results"][0]["newFrozenRebuyPrice"])
        self.assertAlmostEqual(0.72, result["results"][0]["newFrozenRebuyRatio"])
        self.assertTrue(replay["results"][0]["idempotentReplay"])

        db = self._open_db()
        try:
            sell = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (sell_id,)
            ).fetchone()
            rebuy = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (rebuy_id,)
            ).fetchone()
            sell_note = json.loads(sell["note"])
            rebuy_note = json.loads(rebuy["note"])
            self.assertEqual("sold", sell["status"])
            self.assertEqual("pending", rebuy["status"])
            self.assertEqual(7.2, rebuy["expected_price"])
            for note in (sell_note, rebuy_note):
                self.assertEqual(7.2, note["manualRebuyRefrozenPrice"])
                self.assertAlmostEqual(0.72, note["maxRebuyRatioAtOpen"])
                self.assertAlmostEqual(0.72, note["manualRebuyRefrozenRatio"])
                self.assertEqual(1, len(note["manualRebuyRefreezeHistory"]))
                history = note["manualRebuyRefreezeHistory"][0]
                self.assertEqual(6.8, history["oldFrozenRebuyPrice"])
                self.assertAlmostEqual(0.68, history["oldFrozenRebuyRatio"])
            task = db.get_scheduled_task(f"rebuy:{rebuy_id}")
            self.assertEqual("pending", task["status"])
            self.assertLessEqual(
                datetime.fromisoformat(task["next_attempt_at"]),
                datetime.now(timezone.utc) + timedelta(seconds=2),
            )
            audits = db.conn.execute(
                "SELECT * FROM guadao_operation_audit_events WHERE sell_operation_id = ?",
                (sell_id,),
            ).fetchall()
            self.assertEqual(1, len(audits))
            self.assertEqual("manual_rebuy_refrozen", audits[0]["event_type"])
            self.assertEqual(6.8, json.loads(audits[0]["old_value_json"])["expectedPrice"])
            self.assertEqual(7.2, json.loads(audits[0]["new_value_json"])["frozenRebuyPrice"])
        finally:
            db.close()

    def test_batch_manual_complete_preserves_sell_and_assets_and_recomputes_pool_state(self) -> None:
        first_sell, first_rebuy, first_asset = self._create_sold_pending_rebuy(
            market_hash_name="Dreams & Nightmares Case",
            suffix="manual-first",
        )
        second_sell, second_rebuy, second_asset = self._create_sold_pending_rebuy(
            market_hash_name="Dreams & Nightmares Case",
            suffix="manual-second",
        )
        completed_at = "2026-07-17T12:34:56+08:00"

        db = self._open_db()
        try:
            asset_count_before = int(
                db.conn.execute("SELECT COUNT(*) AS count FROM inventory_assets").fetchone()["count"]
            )
        finally:
            db.close()

        with patch.object(self.controller, "_emit_guadao_runtime_event"):
            first = self.controller.batch_complete_guadao_rebuys_manually(
                [first_sell],
                actual_rebuy_price=7.1,
                source="Buff",
                completed_at=completed_at,
                memo="external platform purchase",
                external_order_ref="BUFF-ORDER-1",
                confirmed=True,
                request_id="manual-complete-1",
            )
        self.assertEqual(1, first["successCount"])
        self.assertAlmostEqual(0.71, first["results"][0]["actualRebuyRatio"])

        db = self._open_db()
        try:
            sell = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (first_sell,)
            ).fetchone()
            rebuy = db.conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?", (first_rebuy,)
            ).fetchone()
            self.assertEqual("sold", sell["status"])
            self.assertEqual("completed", rebuy["status"])
            self.assertEqual(7.1, rebuy["expected_price"])
            self.assertEqual(7.1, rebuy["actual_price"])
            self.assertEqual("2026-07-17T04:34:56+00:00", rebuy["completed_at"])
            self.assertIsNone(db.get_scheduled_task(f"rebuy:{first_rebuy}"))
            self.assertEqual("sold", db.get_asset(first_asset)["status"])
            self.assertEqual("sold", db.get_asset(second_asset)["status"])
            self.assertEqual(
                asset_count_before,
                int(db.conn.execute("SELECT COUNT(*) AS count FROM inventory_assets").fetchone()["count"]),
            )
            pool = db.conn.execute(
                "SELECT status FROM inventory_pool WHERE market_hash_name = ?",
                ("Dreams & Nightmares Case",),
            ).fetchone()
            self.assertEqual("pending_rebuy", pool["status"])
            audit = db.conn.execute(
                "SELECT * FROM guadao_operation_audit_events WHERE sell_operation_id = ?",
                (first_sell,),
            ).fetchone()
            self.assertEqual("manual_external_rebuy_completed", audit["event_type"])
            self.assertEqual("Buff", json.loads(audit["new_value_json"])["source"])
        finally:
            db.close()

        with patch.object(self.controller, "_emit_guadao_runtime_event"):
            second = self.controller.batch_complete_guadao_rebuys_manually(
                [second_sell],
                actual_rebuy_price=7.0,
                source="Buff",
                completed_at=completed_at,
                external_order_ref="BUFF-ORDER-2",
                confirmed=True,
                request_id="manual-complete-2",
            )
        self.assertEqual(1, second["successCount"])
        db = self._open_db()
        try:
            self.assertEqual(
                "completed",
                db.conn.execute(
                    "SELECT status FROM pool_operations WHERE id = ?", (second_rebuy,)
                ).fetchone()["status"],
            )
            self.assertEqual(
                "holding",
                db.conn.execute(
                    "SELECT status FROM inventory_pool WHERE market_hash_name = ?",
                    ("Dreams & Nightmares Case",),
                ).fetchone()["status"],
            )
        finally:
            db.close()

    def test_batch_rebuy_actions_reject_unsafe_or_ambiguous_states(self) -> None:
        market_hash_name = "Kilowatt Case"
        c5_sell, c5_rebuy, _ = self._create_sold_pending_rebuy(
            market_hash_name=market_hash_name,
            suffix="blocked-c5",
        )
        running_sell, running_rebuy, _ = self._create_sold_pending_rebuy(
            market_hash_name=market_hash_name,
            suffix="blocked-running",
        )
        listed_sell, _listed_rebuy, _ = self._create_sold_pending_rebuy(
            market_hash_name=market_hash_name,
            suffix="blocked-listed",
        )
        duplicate_sell, _duplicate_rebuy, _ = self._create_sold_pending_rebuy(
            market_hash_name=market_hash_name,
            suffix="blocked-duplicate",
        )
        db = self._open_db()
        try:
            c5_row = db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (c5_rebuy,)
            ).fetchone()
            c5_note = json.loads(c5_row["note"])
            c5_note["c5OutTradeNo"] = "OUT-UNRESOLVED"
            db.update_pool_operation(c5_rebuy, note=json.dumps(c5_note))
            db.upsert_scheduled_task(
                f"rebuy:{running_rebuy}",
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_ATTEMPT,
                next_attempt_at=utc_now_iso(),
                operation_id=running_rebuy,
                status="running",
            )
            db.update_pool_operation(listed_sell, status="listed")
            db.add_pool_operation(
                market_hash_name=market_hash_name,
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=6.7,
                note=json.dumps(
                    {
                        "sourceSellOperationId": duplicate_sell,
                        "steamSellerNetPrice": 10.0,
                    }
                ),
            )
        finally:
            db.close()

        result = self.controller.batch_refreeze_guadao_rebuys(
            [c5_sell, running_sell, listed_sell, duplicate_sell],
            rebuy_price=7.0,
            execute_now=True,
            confirmed=True,
            request_id="blocked-batch",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(0, result["successCount"])
        self.assertEqual(
            {
                c5_sell: "c5_order_state_unresolved",
                running_sell: "rebuy_task_running",
                listed_sell: "not_sold_pending_rebuy",
                duplicate_sell: "multiple_pending_rebuys",
            },
            {row["operationId"]: row["code"] for row in result["results"]},
        )

    def test_batch_manual_complete_validates_time_and_terminal_sibling_states(self) -> None:
        market_hash_name = "Fracture Case"
        early_sell, _early_rebuy, _ = self._create_sold_pending_rebuy(
            market_hash_name=market_hash_name,
            suffix="manual-early",
        )
        delivery_sell, delivery_rebuy, _ = self._create_sold_pending_rebuy(
            market_hash_name=market_hash_name,
            suffix="manual-delivery",
        )
        completed_sell, _completed_rebuy, _ = self._create_sold_pending_rebuy(
            market_hash_name=market_hash_name,
            suffix="manual-completed-sibling",
        )

        with self.assertRaisesRegex(ValueError, "memo or externalOrderRef is required"):
            self.controller.batch_complete_guadao_rebuys_manually(
                [early_sell],
                actual_rebuy_price=6.9,
                source="Buff",
                completed_at="2026-07-17T12:00:00+08:00",
                confirmed=True,
            )

        early = self.controller.batch_complete_guadao_rebuys_manually(
            [early_sell],
            actual_rebuy_price=6.9,
            source="Buff",
            completed_at="2026-07-16T09:59:59+00:00",
            external_order_ref="BUFF-EARLY",
            confirmed=True,
            request_id="manual-early-time",
        )
        self.assertEqual("completed_before_steam_sale", early["results"][0]["code"])

        db = self._open_db()
        try:
            db.update_pool_operation(delivery_rebuy, status="delivery_pending")
            completed_child = db.add_pool_operation(
                market_hash_name=market_hash_name,
                strategy="guadao",
                operation_type=OP_REBUY_C5,
                expected_price=6.7,
                note=json.dumps({"sourceSellOperationId": completed_sell}),
            )
            db.update_pool_operation(completed_child, status="completed", actual_price=6.7)
        finally:
            db.close()

        blocked = self.controller.batch_refreeze_guadao_rebuys(
            [delivery_sell, completed_sell],
            rebuy_price=7.0,
            confirmed=True,
            request_id="terminal-sibling-blocks",
        )
        self.assertEqual(
            {
                delivery_sell: "c5_delivery_pending",
                completed_sell: "rebuy_already_completed",
            },
            {row["operationId"]: row["code"] for row in blocked["results"]},
        )


if __name__ == "__main__":
    unittest.main()
