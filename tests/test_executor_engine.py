from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import (
    OP_REBUY_C5,
    OP_SELL_STEAM,
    OP_TRANSFER_BUY,
    OP_TRANSFER_SELL,
    POOL_STATUS_HOLDING,
    POOL_STATUS_LISTED,
    POOL_STATUS_LISTING_PENDING,
    POOL_STATUS_PENDING_REBUY,
    POOL_STATUS_TRANSFER_HOLDING,
    POOL_STATUS_TRANSFER_SOLD,
    STRATEGY_GUADAO,
    STRATEGY_TRANSFER,
    StrategyCandidate,
    StrategyConfig,
)
from cs2_assistant.services.executor_engine import ExecutionEngine, ListingDecision


class FakeSteamClient:
    def __init__(self) -> None:
        self.steam_id64 = "76561198000000000"
        self.buy_calls: list[dict[str, object]] = []
        self.active_listing_ids: set[str] = set()
        self.sell_calls: list[dict[str, object]] = []
        self.confirm_calls = 0
        self.confirm_should_fail = False
        self.confirm_result = 1

    def get_item_nameid(self, *, app_id: int, market_hash_name: str) -> str:
        return "123456"

    def item_orders_histogram(self, *, item_nameid: str, country: str, language: str, currency: int) -> dict:
        return {"success": 1, "lowest_sell_order": 2500}

    def search_listings(self, *, app_id: int, market_hash_name: str, start: int = 0, count: int = 10) -> dict:
        return {
            "success": 1,
            "listinginfo": {
                "listing-low": {
                    "listingid": "listing-low",
                    "converted_price": 2200,
                    "converted_fee": 300,
                    "converted_total": 2500,
                },
                "listing-high": {
                    "listingid": "listing-high",
                    "converted_price": 2300,
                    "converted_fee": 300,
                    "converted_total": 2600,
                },
            },
        }

    def buy_listing(self, **kwargs: object) -> dict:
        self.buy_calls.append(dict(kwargs))
        return {"wallet_info": {"success": 1}}

    def sell_item(self, **kwargs: object) -> dict:
        self.sell_calls.append(dict(kwargs))
        return {
            "listingid": "listing-1",
        }

    def confirm_all(self) -> int:
        self.confirm_calls += 1
        if self.confirm_should_fail:
            raise RuntimeError("confirm boom")
        return self.confirm_result

    def list_active_listings(self) -> list[object]:
        class Listing:
            def __init__(self, listing_id: str) -> None:
                self.listing_id = listing_id

        return [Listing(listing_id) for listing_id in sorted(self.active_listing_ids)]


class FakeC5Client:
    def __init__(self) -> None:
        self.sale_create_calls: list[dict[str, object]] = []

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        return {
            market_hash_names[0]: {
                "price": 20.0,
                "count": 1,
            }
        }

    def sale_create(self, *, app_id: int, items: list[dict]) -> dict:
        self.sale_create_calls.append({"app_id": app_id, "items": items})
        return {
            "shopOn": True,
            "succeed": 1,
            "failed": 0,
            "successList": [
                {
                    "assetId": items[0]["assetId"],
                    "productId": "product-1",
                }
            ],
        }

    def sale_search(self, *, app_id: int, steam_id: str | None = None, delivery: int | None = None, page: int = 1, limit: int = 20) -> dict:
        return {"total": 0, "page": page, "limit": limit, "list": []}


class FakeServerChan:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def send(self, title: str, body: str) -> None:
        self.messages.append({"title": title, "body": body})


def build_candidate() -> StrategyCandidate:
    return StrategyCandidate(
        name="Revolution Case",
        market_hash_name="Revolution Case",
        inventory_count=1,
        tradable_count=1,
        rebuy_price=20.0,
        rebuy_price_source="c5_batch",
        steam_sell_price=25.0,
        steam_price_source="steam_market",
        steam_after_tax_price=21.73,
        listing_ratio=0.92,
        transfer_real_ratio=0.07,
        recommended_strategies=[STRATEGY_TRANSFER],
        steam_accounts=["main-steam"],
    )


def build_guadao_candidate() -> StrategyCandidate:
    return StrategyCandidate(
        name="Revolution Case",
        market_hash_name="Revolution Case",
        inventory_count=1,
        tradable_count=1,
        rebuy_price=20.0,
        rebuy_price_source="c5_batch",
        steam_sell_price=25.0,
        steam_price_source="steam_market",
        steam_after_tax_price=21.73,
        listing_ratio=0.92,
        transfer_real_ratio=0.07,
        recommended_strategies=[STRATEGY_GUADAO],
        steam_accounts=["main-steam"],
    )


class ExecutorEngineTransferTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "assistant.db"
        self.db = Database(self.db_path)
        self.db.initialize()

        self.engine = object.__new__(ExecutionEngine)
        self.engine.settings = Settings(db_path=self.db_path, app_id=730)
        self.engine.config = StrategyConfig(
            execution_enabled=True,
            dry_run=False,
            max_buy_per_cycle=3,
            max_list_per_cycle=3,
            transfer_min_real_ratio=0.05,
        )
        self.engine.db = self.db
        self.engine.c5_client = FakeC5Client()
        self.engine.steam_client = FakeSteamClient()
        self.engine.serverchan = None
        self.engine._last_inventory_payload = {}
        self.engine._inventory_items_by_asset_id = {}
        self.engine._pending_confirmation_count = 0

        self.db.upsert_pool_item("Revolution Case", 1, status=POOL_STATUS_HOLDING)
        old_asset = {
            "assetId": "asset-old",
            "marketHashName": "Revolution Case",
            "steamId": self.engine.steam_client.steam_id64,
            "ifTradable": True,
            "tradableTime": None,
            "token": "token-old",
            "styleToken": "style-old",
            "price": 20.0,
        }
        self.db.upsert_inventory_assets([old_asset])
        self.engine._inventory_items_by_asset_id = {"asset-old": old_asset}

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_transfer_sells_existing_base_asset_instead_of_new_buy(self) -> None:
        candidate = build_candidate()

        self.assertTrue(self.engine._execute_transfer_buy(candidate))
        self.assertEqual(1, len(self.engine.steam_client.buy_calls))
        self.assertEqual("listing-low", self.engine.steam_client.buy_calls[0]["listing_id"])

        buy_ops = self.db.list_pool_operations_by_type(OP_TRANSFER_BUY, limit=10)
        self.assertEqual(1, len(buy_ops))
        self.assertEqual("pending", buy_ops[0]["status"])
        self.assertIn('"sellAssetId": "asset-old"', buy_ops[0]["note"])

        listed = self.engine._execute_transfer_sells()
        self.assertEqual(1, listed)
        self.assertEqual(1, len(self.engine.c5_client.sale_create_calls))

        sale_call = self.engine.c5_client.sale_create_calls[0]
        sale_item = sale_call["items"][0]
        self.assertEqual("asset-old", sale_item["assetId"])
        self.assertEqual("token-old", sale_item["token"])
        self.assertEqual("style-old", sale_item["styleToken"])

        buy_ops = self.db.list_pool_operations_by_type(OP_TRANSFER_BUY, limit=10)
        sell_ops = self.db.list_pool_operations_by_type(OP_TRANSFER_SELL, limit=10)
        self.assertEqual("listed", buy_ops[0]["status"])
        self.assertEqual(1, len(sell_ops))
        self.assertEqual("listed", sell_ops[0]["status"])
        self.assertEqual("asset-old", sell_ops[0]["asset_id"])

    def test_transfer_returns_to_holding_only_after_replacement_is_tradable(self) -> None:
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_TRANSFER,
            operation_type=OP_TRANSFER_BUY,
            expected_price=25.0,
            asset_id="asset-new",
            note='{"boughtAssetId":"asset-new"}',
        )
        op = self.db.list_pool_operations_by_type(OP_TRANSFER_BUY, limit=10)[0]
        self.db.update_pool_operation(op["id"], status="sold")
        self.db.upsert_pool_item("Revolution Case", 1, status=POOL_STATUS_TRANSFER_SOLD)

        self.engine._inventory_items_by_asset_id["asset-new"] = {
            "assetId": "asset-new",
            "marketHashName": "Revolution Case",
            "steamId": self.engine.steam_client.steam_id64,
            "ifTradable": False,
            "tradableTime": None,
        }
        self.engine._refresh_transfer_holdings()
        row = self.db.list_pool_items(status=POOL_STATUS_TRANSFER_HOLDING)[0]
        self.assertEqual("Revolution Case", row["market_hash_name"])

        self.engine._inventory_items_by_asset_id["asset-new"]["ifTradable"] = True
        self.engine._refresh_transfer_holdings()
        row = self.db.list_pool_items(status=POOL_STATUS_HOLDING)[0]
        self.assertEqual("Revolution Case", row["market_hash_name"])

    def test_guadao_listing_does_not_pre_rebuy_even_if_config_enabled(self) -> None:
        self.engine.config.dry_run = True
        self.engine.config.force_refresh_before_execution = False
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type(
            "Report",
            (),
            {"guadao_candidates": [build_guadao_candidate()]},
        )()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        rebuy_ops = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual([], rebuy_ops)

    def test_guadao_refresh_listing_marks_pending_rebuy_and_creates_rebuy_op(self) -> None:
        self.engine.config.listing_check_interval_minutes = 0
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        sold = self.engine._refresh_listings()

        self.assertEqual(1, sold)
        row = self.db.list_pool_items(status=POOL_STATUS_PENDING_REBUY)[0]
        self.assertEqual("Revolution Case", row["market_hash_name"])
        rebuy_ops = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual(1, len(rebuy_ops))
        self.assertEqual("pending", rebuy_ops[0]["status"])

    def test_guadao_does_not_continue_listing_when_cycle_is_open(self) -> None:
        self.engine.config.dry_run = True
        self.engine.config.force_refresh_before_execution = False
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_PENDING_REBUY})

        self.assertEqual(0, listed)

    def test_listing_pending_confirmation_is_recorded_when_credentials_missing(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = None
        self.engine.settings.steam_device_id = None
        self.engine.serverchan = FakeServerChan()
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertIn('"needsConfirmation": true', sell_op["note"])
        self.assertIn('"confirmationStatus": "manual_required"', sell_op["note"])
        self.assertEqual(1, self.engine._pending_confirmation_count)
        self.assertEqual(1, len(self.engine.serverchan.messages))

    def test_listing_confirm_failure_is_not_silent(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine.steam_client.confirm_should_fail = True
        self.engine.serverchan = FakeServerChan()
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertIn('"confirmationStatus": "failed"', sell_op["note"])
        self.assertIn('confirm boom', sell_op["note"])
        self.assertEqual(1, self.engine._pending_confirmation_count)
        self.assertEqual(1, len(self.engine.serverchan.messages))

    def test_listing_auto_confirm_marks_listed(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTED)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertIn('"confirmationStatus": "confirmed"', sell_op["note"])
        self.assertEqual(1, self.engine.steam_client.confirm_calls)
        self.assertEqual(0, self.engine._pending_confirmation_count)

    def test_listing_without_found_confirmation_is_marked_pending(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine.steam_client.confirm_result = 0
        self.engine.serverchan = FakeServerChan()
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertIn('"confirmationStatus": "not_found"', sell_op["note"])
        self.assertEqual(1, self.engine._pending_confirmation_count)
        self.assertEqual(1, len(self.engine.serverchan.messages))

    def test_pending_confirmation_recovers_when_listing_appears_active(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","needsConfirmation":true,"confirmationStatus":"not_found"}',
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
        self.engine.steam_client.active_listing_ids = {"listing-1"}

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(1, updated)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTED)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertIn('"confirmationStatus": "confirmed_late"', sell_op["note"])

    @patch("cs2_assistant.services.executor_engine.scan_strategies")
    @patch("cs2_assistant.services.executor_engine.time.sleep")
    def test_run_once_waits_for_guadao_cycle_to_close(self, sleep_mock: object, scan_mock: object) -> None:
        self.engine.config.listing_check_interval_minutes = 2
        self.engine._sync_assets = lambda: None  # type: ignore[method-assign]
        self.engine._refresh_transfer_holdings = lambda: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_buys = lambda report, status_map: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_sells = lambda: 0  # type: ignore[method-assign]
        self.engine._refresh_transfer_sales = lambda: 0  # type: ignore[method-assign]

        report = type(
            "Report",
            (),
            {
                "guadao_candidates": [build_guadao_candidate()],
                "transfer_candidates": [],
            },
        )()
        scan_mock.return_value = report

        state = {"advance_calls": 0}

        def fake_execute(report_obj: object, status_map: dict[str, str]) -> int:
            self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
            return 1

        def fake_advance() -> tuple[int, int]:
            state["advance_calls"] += 1
            if state["advance_calls"] >= 2:
                self.db.set_pool_status("Revolution Case", POOL_STATUS_HOLDING)
                return (1, 1)
            return (0, 0)

        self.engine._execute_guadao_listings = fake_execute  # type: ignore[method-assign]
        self.engine._backfill_listing_ids = lambda: 0  # type: ignore[method-assign]
        self.engine._advance_guadao_cycle = fake_advance  # type: ignore[method-assign]

        self.engine.run_once(wait_for_cycle=True)

        self.assertEqual(1, sleep_mock.call_count)
        self.assertEqual(((120.0,),), tuple(call.args for call in sleep_mock.call_args_list))
        pool_row = self.db.list_pool_items(status=POOL_STATUS_HOLDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])

    @patch("cs2_assistant.services.executor_engine.scan_strategies")
    def test_run_once_reports_when_pool_item_missing_from_real_inventory(self, scan_mock: object) -> None:
        self.engine._sync_assets = lambda: None  # type: ignore[method-assign]
        self.engine._refresh_transfer_holdings = lambda: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_buys = lambda report, status_map: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_sells = lambda: 0  # type: ignore[method-assign]
        self.engine._refresh_transfer_sales = lambda: 0  # type: ignore[method-assign]
        self.engine._last_inventory_payload = {"list": []}
        self.db.upsert_pool_item("Fracture Case", 1, status=POOL_STATUS_HOLDING)

        report = type(
            "Report",
            (),
            {
                "all_evaluated": [],
                "guadao_candidates": [],
                "transfer_candidates": [],
                "guadao_count": 0,
                "transfer_count": 0,
                "missing_price_count": 0,
                "total_pool_types": 2,
            },
        )()
        scan_mock.return_value = report

        with patch("builtins.print") as print_mock:
            self.engine.run_once(wait_for_cycle=False)

        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("[扫描] 底仓池 2 个品种 | 进入评估 0 个 | 缺价 0 个 | 挂刀候选 0 个 | 导余额候选 0 个", printed)
        self.assertIn("[结果] 本轮只完成了扫描/状态检查，没有实际上架、买入或卖出。", printed)
        self.assertIn("当前真实库存里不存在", printed)
        self.assertIn("Fracture Case、Revolution Case", printed)

    @patch("cs2_assistant.services.executor_engine.scan_strategies")
    def test_run_once_reports_open_guadao_cycle_without_actions(self, scan_mock: object) -> None:
        self.engine._sync_assets = lambda: None  # type: ignore[method-assign]
        self.engine._refresh_transfer_holdings = lambda: 0  # type: ignore[method-assign]
        self.engine._advance_guadao_cycle = lambda: (0, 0)  # type: ignore[method-assign]
        self.engine._execute_transfer_buys = lambda report, status_map: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_sells = lambda: 0  # type: ignore[method-assign]
        self.engine._refresh_transfer_sales = lambda: 0  # type: ignore[method-assign]
        self.db.set_pool_status("Revolution Case", POOL_STATUS_PENDING_REBUY)
        self.engine._last_inventory_payload = {
            "list": [
                {
                    "assetId": "asset-old",
                    "marketHashName": "Revolution Case",
                }
            ]
        }

        report = type(
            "Report",
            (),
            {
                "all_evaluated": [build_guadao_candidate()],
                "guadao_candidates": [build_guadao_candidate()],
                "transfer_candidates": [],
                "guadao_count": 1,
                "transfer_count": 0,
                "missing_price_count": 0,
                "total_pool_types": 1,
            },
        )()
        scan_mock.return_value = report

        with patch("builtins.print") as print_mock:
            self.engine.run_once(wait_for_cycle=False)

        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("[等待] 检测到上一轮挂刀循环未闭环，本轮先等待卖出/补仓完成。", printed)
        self.assertIn("[结果] 本轮只完成了扫描/状态检查，没有实际上架、买入或卖出。", printed)
        self.assertIn("上一轮挂刀循环未闭环（pending_rebuy=1）", printed)


if __name__ == "__main__":
    unittest.main()
