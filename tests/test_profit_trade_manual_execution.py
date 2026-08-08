from __future__ import annotations

import inspect
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import MarketState, StrategyConfig
import cs2_assistant.services.profit_trade as profit_trade_module
import cs2_assistant.services.runtime_controller as runtime_controller_module
from cs2_assistant.services.profit_trade import execute_manual_profit_trade_request
from cs2_assistant.services.runtime_controller import (
    RUNTIME_PROFIT_TRADE,
    TASK_PROFIT_MANUAL_EXECUTION,
    UnifiedRuntimeController,
)
from cs2_assistant.services.strategy import save_strategy_config


MARKET_HASH_NAME = "AK-47 | Redline (Field-Tested)"
SNAPSHOT_EXPECTED_ROI = 0.0525
SNAPSHOT_SCAN_ID = "PTSCAN-manual-queue"
SNAPSHOT_OBSERVED_AT = "2026-07-23T01:02:03+00:00"


def profit_config(**overrides: object) -> StrategyConfig:
    values: dict[str, object] = {
        "profit_trade_enabled": True,
        "profit_trade_allow_real_execution": True,
        "profit_trade_min_roi": 0.07,
        "profit_trade_min_item_value": 1.0,
        "profit_trade_require_c5_recent_sales": False,
        "profit_trade_require_c5_market_depth": False,
        "profit_trade_manual_review_roi": 1.0,
        "profit_trade_balance_discount": 0.69,
        "profit_trade_daily_steam_budget": 10_000.0,
    }
    values.update(overrides)
    return StrategyConfig(**values)


class FakeManualMarketService:
    def __init__(self, *, c5_price: float = 75.0, steam_price: float = 100.0) -> None:
        self.c5_price = c5_price
        self.steam_price = steam_price
        self.calls: list[list[dict]] = []

    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        self.calls.append(list(items))
        return [
            MarketState(
                market_hash_name=str(item["market_hash_name"]),
                name_cn=str(item.get("name_cn") or item["market_hash_name"]),
                c5_sell_price=self.c5_price,
                c5_price_source="c5_batch",
                steam_sell_price=self.steam_price,
                steam_price_source="steam_orderbook",
            )
            for item in items
        ]


class ProfitTradeManualQueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.temp_dir.name) / "assistant.db",
            c5_api_key="test-c5-key",
        )
        self.config = profit_config()
        save_strategy_config(self.settings, self.config)
        self.controller = UnifiedRuntimeController(self.settings, poll_seconds=0.2)
        db = self._open_db()
        try:
            db.upsert_executor_runtime_state(
                RUNTIME_PROFIT_TRADE,
                enabled=True,
                runtime_status="running",
                migration_hold=False,
                payload={},
            )
        finally:
            db.close()

    def tearDown(self) -> None:
        self.controller.stop(timeout=0)
        self.temp_dir.cleanup()

    def _open_db(self) -> Database:
        db = Database(self.settings.db_path)
        db.initialize()
        return db

    def _seed_watch(
        self,
        *,
        quantity: int = 2,
        expected_roi: float = SNAPSHOT_EXPECTED_ROI,
        execution_status: str = "below_min_roi",
        execution_reason: str = "ROI below automatic threshold",
        scan_id: str = SNAPSHOT_SCAN_ID,
        observed_at: str = SNAPSHOT_OBSERVED_AT,
    ) -> None:
        db = self._open_db()
        try:
            db.upsert_inventory_assets(
                [
                    {
                        "assetId": f"asset-{index}",
                        "marketHashName": MARKET_HASH_NAME,
                        "steamId": "steam-a",
                        "ifTradable": True,
                    }
                    for index in range(1, quantity + 1)
                ]
            )
            db.record_profit_trade_roi_scan(
                [
                    {
                        "market_hash_name": MARKET_HASH_NAME,
                        "name_cn": "AK-47 | Redline",
                        "steam_buy_price": 100.0,
                        "steam_price_source": "steam_orderbook",
                        "c5_listing_price": 75.0,
                        "c5_price_source": "c5_batch",
                        "c5_expected_net_price": 74.25,
                        "balance_discount": 0.69,
                        "expected_profit": 5.25,
                        "expected_roi": expected_roi,
                        "min_roi": 0.07,
                        "manual_review_roi": 1.0,
                        "inventory_count": quantity,
                        "tradable_count": quantity,
                        "risk_status": "passed",
                        "execution_status": execution_status,
                        "execution_reason": execution_reason,
                        "raw": {"manualExecutableQuantity": quantity},
                    }
                ],
                scan_id=scan_id,
                observed_at=observed_at,
            )
        finally:
            db.close()

    def _queue(
        self,
        *,
        quantity: int,
        expected_roi: float = SNAPSHOT_EXPECTED_ROI,
        scan_id: str = SNAPSHOT_SCAN_ID,
        observed_at: str = SNAPSHOT_OBSERVED_AT,
    ) -> dict:
        kwargs: dict[str, object] = {
            "market_hash_name": MARKET_HASH_NAME,
            "quantity": quantity,
            "confirmed": True,
        }
        parameters = inspect.signature(
            self.controller.queue_profit_trade_manual_execution
        ).parameters
        if {"expected_roi", "scan_id", "observed_at"}.issubset(parameters):
            kwargs.update(
                {
                    "expected_roi": expected_roi,
                    "scan_id": scan_id,
                    "observed_at": observed_at,
                }
            )
        return self.controller.queue_profit_trade_manual_execution(
            **kwargs,
        )

    def _set_runtime_enabled(self, enabled: bool) -> None:
        db = self._open_db()
        try:
            db.upsert_executor_runtime_state(
                RUNTIME_PROFIT_TRADE,
                enabled=enabled,
                runtime_status="running" if enabled else "stopped",
                migration_hold=False,
                payload={},
            )
        finally:
            db.close()

    def test_manual_queue_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit confirmation"):
            self.controller.queue_profit_trade_manual_execution(
                market_hash_name=MARKET_HASH_NAME,
                quantity=1,
                confirmed=False,
            )

    def test_manual_queue_enforces_runtime_and_real_execution_gates(self) -> None:
        self._seed_watch(quantity=1)
        self._set_runtime_enabled(False)
        with self.assertRaisesRegex(RuntimeError, "Profit Trade"):
            self._queue(quantity=1)

        self._set_runtime_enabled(True)
        save_strategy_config(
            self.settings,
            profit_config(profit_trade_allow_real_execution=False),
        )
        with self.assertRaisesRegex(RuntimeError, "Profit Trade"):
            self._queue(quantity=1)

    def test_manual_queue_requires_complete_snapshot_binding(self) -> None:
        self._seed_watch(quantity=1)

        with self.assertRaisesRegex(ValueError, "snapshot|scanId|observedAt|expectedRoi"):
            self.controller.queue_profit_trade_manual_execution(
                market_hash_name=MARKET_HASH_NAME,
                quantity=1,
                confirmed=True,
            )

    def test_positive_roi_below_automatic_threshold_is_queued_persistently(self) -> None:
        self._seed_watch(quantity=2)

        result = self._queue(quantity=2)

        self.assertTrue(result["queued"])
        self.assertEqual(2, result["quantity"])
        self.assertEqual(2, result["maxQuantity"])
        self.assertAlmostEqual(0.0525, result["approvedExpectedRoi"])
        db = self._open_db()
        try:
            task = db.get_scheduled_task(result["taskKey"])
        finally:
            db.close()
        self.assertIsNotNone(task)
        self.assertEqual(TASK_PROFIT_MANUAL_EXECUTION, task["task_type"])
        self.assertEqual("pending", task["status"])
        payload = json.loads(str(task["payload_json"]))
        self.assertEqual(result["requestId"], payload["requestId"])
        self.assertEqual(2, payload["quantity"])
        self.assertAlmostEqual(0.0525, payload["approvedExpectedRoi"])
        self.assertEqual(SNAPSHOT_SCAN_ID, payload["approvedScanId"])
        self.assertEqual(SNAPSHOT_OBSERVED_AT, payload["approvedObservedAt"])

    def test_manual_execution_status_tracks_pending_running_and_failed(self) -> None:
        self._seed_watch(quantity=1)
        queued = self._queue(quantity=1)

        pending = self.controller.profit_trade_manual_execution_status(
            queued["requestId"]
        )
        self.assertEqual("pending", pending["status"])
        self.assertFalse(pending["terminal"])
        self.assertEqual(1, pending["requestedQuantity"])
        self.assertEqual([], pending["trades"])

        db = self._open_db()
        try:
            claimed = db.claim_due_scheduled_tasks(
                "test-worker",
                source=RUNTIME_PROFIT_TRADE,
                limit=1,
            )
        finally:
            db.close()
        self.assertEqual(1, len(claimed))

        running = self.controller.profit_trade_manual_execution_status(
            queued["requestId"]
        )
        self.assertEqual("running", running["status"])
        self.assertFalse(running["terminal"])

        db = self._open_db()
        try:
            self.assertTrue(
                db.complete_scheduled_task(
                    queued["taskKey"],
                    "test-worker",
                    status="failed",
                    error="Steam listings HTTP 400 and fallback could not continue",
                )
            )
        finally:
            db.close()

        failed = self.controller.profit_trade_manual_execution_status(
            queued["requestId"]
        )
        self.assertEqual("failed", failed["status"])
        self.assertTrue(failed["terminal"])
        self.assertIn("HTTP 400", failed["error"])
        self.assertIn("失败", failed["summary"])

    def test_manual_queue_rejects_quantity_above_current_executable_assets(self) -> None:
        self._seed_watch(quantity=2)

        with self.assertRaisesRegex(RuntimeError, "3"):
            self._queue(quantity=3)

    def test_manual_queue_rejects_duplicate_active_batch_for_same_item(self) -> None:
        self._seed_watch(quantity=2)
        first = self._queue(quantity=1)

        with self.assertRaisesRegex(RuntimeError, "one-click|一键|任务"):
            self._queue(quantity=1)

        db = self._open_db()
        try:
            rows = db.list_scheduled_tasks(source=RUNTIME_PROFIT_TRADE, limit=100)
        finally:
            db.close()
        matching = [
            row
            for row in rows
            if row["task_type"] == TASK_PROFIT_MANUAL_EXECUTION
        ]
        self.assertEqual([first["taskKey"]], [row["task_key"] for row in matching])

    def test_manual_queue_rejects_non_roi_risk_block(self) -> None:
        self._seed_watch(
            quantity=1,
            execution_status="c5_risk_blocked",
            execution_reason="C5 depth is insufficient",
        )

        with self.assertRaisesRegex(RuntimeError, "C5 depth"):
            self._queue(quantity=1)

    def test_manual_queue_rejects_stale_dialog_snapshot_after_roi_changes(self) -> None:
        self._seed_watch(quantity=2)
        self._seed_watch(
            quantity=2,
            expected_roi=0.0400,
            scan_id="PTSCAN-newer-worse-roi",
            observed_at="2026-07-23T01:12:03+00:00",
        )

        with self.assertRaisesRegex(RuntimeError, "snapshot|changed|refresh|ROI"):
            self._queue(quantity=1)

        db = self._open_db()
        try:
            tasks = db.list_scheduled_tasks(
                source=RUNTIME_PROFIT_TRADE,
                task_type=TASK_PROFIT_MANUAL_EXECUTION,
                limit=100,
            )
        finally:
            db.close()
        self.assertEqual([], tasks)

    def test_concurrent_same_item_queue_creates_exactly_one_active_task(self) -> None:
        self._seed_watch(quantity=2)
        insertion_barrier = threading.Barrier(2)
        start_barrier = threading.Barrier(3)
        original_ensure = Database.ensure_scheduled_task
        successes: list[dict] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def synchronized_ensure(
            db: Database,
            *args: object,
            **kwargs: object,
        ) -> object:
            try:
                insertion_barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
            return original_ensure(db, *args, **kwargs)

        def submit() -> None:
            start_barrier.wait(timeout=2)
            try:
                result = self._queue(quantity=1)
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)
            else:
                with result_lock:
                    successes.append(result)

        with patch.object(
            Database,
            "ensure_scheduled_task",
            new=synchronized_ensure,
        ):
            threads = [threading.Thread(target=submit) for _ in range(2)]
            for thread in threads:
                thread.start()
            start_barrier.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], RuntimeError)

        db = self._open_db()
        try:
            active_tasks = db.conn.execute(
                """
                SELECT *
                FROM scheduled_tasks
                WHERE source = ?
                  AND task_type = ?
                  AND status IN ('pending', 'retry', 'running')
                  AND json_extract(payload_json, '$.marketHashName') = ?
                """,
                (
                    RUNTIME_PROFIT_TRADE,
                    TASK_PROFIT_MANUAL_EXECUTION,
                    MARKET_HASH_NAME,
                ),
            ).fetchall()
        finally:
            db.close()
        self.assertEqual(1, len(active_tasks))

    def test_dispatch_routes_persistent_manual_task_to_batch_executor(self) -> None:
        task = {
            "task_type": TASK_PROFIT_MANUAL_EXECUTION,
            "task_key": "profit-manual:request-1",
            "payload_json": json.dumps(
                {
                    "requestId": "request-1",
                    "marketHashName": MARKET_HASH_NAME,
                    "quantity": 2,
                    "approvedExpectedRoi": 0.0525,
                    "approvedScanId": "PTSCAN-1",
                    "approvedObservedAt": "2026-07-23T01:02:03+00:00",
                    "requestedAt": "2026-07-23T01:03:00+00:00",
                }
            ),
        }
        expected = {"ok": True, "requestId": "request-1"}
        with patch.object(
            runtime_controller_module,
            "execute_manual_profit_trade_request",
            return_value=expected,
        ) as execute:
            result = self.controller._dispatch_task(task, enabled=True)

        self.assertEqual(expected, result)
        execute.assert_called_once()
        kwargs = execute.call_args.kwargs
        self.assertEqual("request-1", kwargs["request_id"])
        self.assertEqual(MARKET_HASH_NAME, kwargs["market_hash_name"])
        self.assertEqual(2, kwargs["quantity"])
        self.assertAlmostEqual(0.0525, kwargs["approved_expected_roi"])
        self.assertEqual("PTSCAN-1", kwargs["approved_scan_id"])
        self.assertTrue(callable(kwargs["new_action_guard"]))


class ProfitTradeManualExecutionServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.temp_dir.name) / "assistant.db",
            c5_api_key="test-c5-key",
        )
        self.config = profit_config()
        self.inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": f"asset-{index}",
                    "marketHashName": MARKET_HASH_NAME,
                    "name": "AK-47 | Redline",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 75.0,
                    "token": f"token-{index}",
                    "styleToken": f"style-{index}",
                }
                for index in range(1, 4)
            ],
        }
        db = Database(self.settings.db_path)
        try:
            db.initialize()
        finally:
            db.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _execute(
        self,
        *,
        request_id: str,
        quantity: int,
        approved_roi: float,
        market_service: FakeManualMarketService | None = None,
    ) -> dict:
        return execute_manual_profit_trade_request(
            self.settings,
            request_id=request_id,
            market_hash_name=MARKET_HASH_NAME,
            quantity=quantity,
            approved_expected_roi=approved_roi,
            approved_scan_id="PTSCAN-confirmed",
            approved_observed_at="2026-07-23T01:02:03+00:00",
            requested_at=datetime.now(timezone.utc).isoformat(),
            config=self.config,
            inventory_payload=self.inventory_payload,
            market_service=market_service or FakeManualMarketService(),
            c5_client=object(),
            new_action_guard=lambda: True,
        )

    def test_below_auto_min_roi_creates_multiple_trades_and_request_id_is_idempotent(self) -> None:
        def mark_steam_bought(
            _settings: Settings,
            trade_id: int,
            **_: object,
        ) -> dict:
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                db.update_profit_trade(
                    trade_id,
                    status="steam_bought",
                    step_key="steam_bought",
                    step_index=3,
                )
            finally:
                db.close()
            return {"ok": True}

        def mark_c5_listed(
            _settings: Settings,
            trade_id: int,
            **_: object,
        ) -> dict:
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                db.update_profit_trade(
                    trade_id,
                    status="c5_listed",
                    step_key="c5_listed",
                    step_index=5,
                )
            finally:
                db.close()
            return {"ok": True}

        with (
            patch.object(
                profit_trade_module,
                "_fetch_c5_recent_sale_risks",
                return_value={},
            ),
            patch.object(
                profit_trade_module,
                "execute_profit_trade_buy",
                side_effect=mark_steam_bought,
            ) as buy,
            patch.object(
                profit_trade_module,
                "execute_profit_trade_list_c5",
                side_effect=mark_c5_listed,
            ) as list_c5,
        ):
            first = self._execute(
                request_id="manual-request-idempotent",
                quantity=2,
                approved_roi=0.05,
            )
            second = self._execute(
                request_id="manual-request-idempotent",
                quantity=2,
                approved_roi=0.05,
            )

        self.assertTrue(first["ok"])
        self.assertEqual(2, len(first["createdTradeIds"]))
        self.assertEqual(2, len(first["tradeIds"]))
        self.assertEqual([], second["createdTradeIds"])
        self.assertEqual(first["tradeIds"], second["tradeIds"])
        self.assertEqual(2, buy.call_count)
        self.assertEqual(2, list_c5.call_count)

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            rows = db.list_profit_trades_for_manual_request(
                "manual-request-idempotent",
                limit=20,
            )
        finally:
            db.close()
        self.assertEqual(2, len(rows))
        notes = [json.loads(str(row["note"])) for row in rows]
        self.assertEqual([1, 2], [note["manualExecutionBatchIndex"] for note in notes])
        self.assertTrue(all(note["manualExecutionApproved"] for note in notes))
        self.assertTrue(
            all(note["manualExecutionRoiFloor"] == 0.05 for note in notes)
        )
        self.assertTrue(all(note["minRoiAtOpen"] == 0.05 for note in notes))
        self.assertTrue(
            all(
                note["minRoiAtOpenSource"] == "manual_execution_approved_snapshot"
                for note in notes
            )
        )
        self.assertTrue(
            all(note["manualExecutionAutomaticMinRoi"] == 0.07 for note in notes)
        )

    def test_current_roi_below_manually_approved_floor_is_rejected_without_trades(self) -> None:
        with patch.object(
            profit_trade_module,
            "_fetch_c5_recent_sale_risks",
            return_value={},
        ):
            with self.assertRaisesRegex(RuntimeError, "lower than the value confirmed"):
                self._execute(
                    request_id="manual-request-lower-roi",
                    quantity=1,
                    approved_roi=0.05,
                    market_service=FakeManualMarketService(c5_price=74.0),
                )

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_count = db.conn.execute(
                "SELECT COUNT(*) FROM profit_trades"
            ).fetchone()[0]
            reservation_count = db.conn.execute(
                "SELECT COUNT(*) FROM asset_reservations"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(0, trade_count)
        self.assertEqual(0, reservation_count)

    def test_partial_batch_reservation_failure_cleans_up_earlier_locked_trades(self) -> None:
        original_create = profit_trade_module._create_profit_trade_from_opportunity
        create_calls = 0

        def fail_second_create(*args: object, **kwargs: object) -> int | None:
            nonlocal create_calls
            create_calls += 1
            if create_calls == 2:
                return None
            return original_create(*args, **kwargs)

        with (
            patch.object(
                profit_trade_module,
                "_fetch_c5_recent_sale_risks",
                return_value={},
            ),
            patch.object(
                profit_trade_module,
                "_create_profit_trade_from_opportunity",
                side_effect=fail_second_create,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "became unavailable"):
                self._execute(
                    request_id="manual-request-partial-reservation",
                    quantity=2,
                    approved_roi=0.05,
                )

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            rows = db.list_profit_trades_for_manual_request(
                "manual-request-partial-reservation",
                limit=20,
            )
            active_reservations = db.list_asset_reservations(
                owner="profit_trade",
                statuses=["active", "consumed"],
                limit=20,
            )
        finally:
            db.close()
        self.assertTrue(rows)
        self.assertTrue(all(str(row["status"]) == "cancelled" for row in rows))
        self.assertEqual([], active_reservations)

    def test_long_buy_cancellation_race_is_imported_and_listed_in_same_manual_batch(self) -> None:
        """A confirmed old buy-order fill must win over a second direct buy."""

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            long_buy_order_id = db.create_profit_trade_long_buy_order(
                market_hash_name=MARKET_HASH_NAME,
                steam_account_id="account-a",
                steam_id="steam-a",
                create_request_id="long-buy-race-create",
                bid_price_cents=7_000,
                quantity=1,
                c5_price_batch=75.0,
                c5_expected_net_price=74.25,
                balance_discount=0.69,
                standard_roi=0.05,
                aggressive_roi=0.045,
                standard_safe_price_cents=7_070,
                aggressive_safe_price_cents=7_120,
                competitor_buy_price_cents=6_990,
                competitor_buy_status="raw",
                worst_case_roi=0.05,
                source_scan_id="long-buy-race-scan",
                wallet_before=1_000.0,
            )
            db.update_profit_trade_long_buy_order(
                long_buy_order_id,
                event_type="remote_created",
                state="active",
                buy_order_id="long-buy-race-remote",
            )
            recorded_fill = db.record_profit_trade_long_buy_fill(
                long_buy_order_id=long_buy_order_id,
                steam_account_id="account-a",
                purchase_id="long-buy-race-purchase",
                listing_id="long-buy-race-listing",
                market_hash_name=MARKET_HASH_NAME,
                paid_total_cents=6_950,
                asset_id="b-before-race",
                new_asset_id="b-after-race",
                purchased_at="2026-07-29T01:00:00+00:00",
                evidence={
                    "receipt": {
                        "purchaseId": "long-buy-race-purchase",
                        "paidTotal": 69.5,
                    }
                },
            )
        finally:
            db.close()
        fill_id = int(recorded_fill["id"])

        def direct_buy_hits_long_buy_fill(
            _settings: Settings,
            trade_id: int,
            **_: object,
        ) -> dict:
            self.assertTrue(
                profit_trade_module._cancel_locked_trade_before_steam_buy_by_id(
                    self.settings,
                    trade_id,
                    reason="test cancellation race: long buy filled",
                )
            )
            return {"ok": False, "longBuyFillIds": [fill_id]}

        listed_calls: list[int] = []

        def mark_long_buy_c5_listed(
            _settings: Settings,
            trade_id: int,
            **_: object,
        ) -> dict:
            listed_calls.append(trade_id)
            db_for_listing = Database(self.settings.db_path)
            try:
                db_for_listing.initialize()
                db_for_listing.update_profit_trade(
                    trade_id,
                    status="c5_listed",
                    step_key="c5_listed",
                    step_index=5,
                )
            finally:
                db_for_listing.close()
            return {"ok": True}

        with (
            patch.object(
                profit_trade_module,
                "_fetch_c5_recent_sale_risks",
                return_value={},
            ),
            patch.object(
                profit_trade_module,
                "execute_profit_trade_buy",
                side_effect=direct_buy_hits_long_buy_fill,
            ) as buy,
            patch.object(
                profit_trade_module,
                "execute_profit_trade_list_c5",
                side_effect=mark_long_buy_c5_listed,
            ),
        ):
            result = self._execute(
                request_id="manual-long-buy-cancellation-race",
                quantity=2,
                approved_roi=0.05,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(1, buy.call_count)
        self.assertEqual([], result["boughtTradeIds"])
        self.assertEqual(1, len(result["longBuyFillTradeIds"]))
        self.assertEqual(
            result["longBuyFillTradeIds"],
            result["longBuyListedTradeIds"],
        )
        self.assertEqual(result["longBuyListedTradeIds"], listed_calls)
        self.assertEqual([], result["longBuyManualTradeIds"])
        self.assertTrue(set(result["longBuyListedTradeIds"]).issubset(result["listedTradeIds"]))

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            direct_rows = db.list_profit_trades_for_manual_request(
                "manual-long-buy-cancellation-race",
                limit=20,
            )
            fill = db.get_profit_trade_long_buy_fill(fill_id)
            imported = db.get_profit_trade(result["longBuyFillTradeIds"][0])
        finally:
            db.close()
        self.assertEqual(2, len(direct_rows))
        self.assertTrue(all(str(row["status"]) == "cancelled" for row in direct_rows))
        self.assertEqual("processed", fill["state"])
        self.assertEqual(result["longBuyFillTradeIds"][0], int(fill["profit_trade_id"]))
        self.assertEqual("c5_listed", imported["status"])


if __name__ == "__main__":
    unittest.main()
