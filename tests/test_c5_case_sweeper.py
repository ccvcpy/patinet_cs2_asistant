from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cs2_assistant.accounts import AccountStore
from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import CatalogItem
from cs2_assistant.services.c5_case_sweeper import C5CaseSweeper


class FakeC5SweeperClient:
    def __init__(self, price: float = 1.10) -> None:
        self.price = price
        self.market_list_calls: list[dict[str, object]] = []
        self.batch_buy_calls: list[dict[str, object]] = []
        self.listings: list[dict[str, object]] = [
            {"productId": "product-1", "price": price, "delivery": 2},
        ]
        self.details: dict[str, dict[str, object]] = {}
        self.buyer_orders: list[dict[str, object]] = []
        self.steam_accounts: list[dict[str, object]] = [{
            "steamId": "76561197960265729",
            "nickname": "test-c5-account",
            "autoType": 2,
        }]

    def steam_info(self) -> dict[str, object]:
        return {"steamList": self.steam_accounts}

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, object]:
        return {market_hash_names[0]: {"price": self.price, "count": 50}}

    def market_products_list(self, **kwargs: object) -> dict[str, object]:
        self.market_list_calls.append(kwargs)
        page_num = int(kwargs.get("page_num") or 1)
        page_size = int(kwargs.get("page_size") or 10)
        start = (page_num - 1) * page_size
        end = start + page_size
        return {
            "list": self.listings[start:end],
            "pageNum": page_num,
            "pageSize": page_size,
            "hasMore": end < len(self.listings),
        }

    def batch_buy(self, **kwargs: object) -> dict[str, object]:
        self.batch_buy_calls.append(kwargs)
        success_list = []
        for number, row in enumerate(kwargs.get("product_list") or [], start=1):
            success_list.append({
                "outTradeNo": row["outTradeNo"],
                "productId": row["productId"],
                "actualPay": row["buyPrice"],
                "delivery": 2,
                "orderAssetId": f"asset-order-{len(self.batch_buy_calls)}-{number}",
                "orderId": f"trade-order-{len(self.batch_buy_calls)}-{number}",
            })
        return {"successNum": len(success_list), "failNum": 0, "successList": success_list, "failedList": []}

    def buyer_order_detail(self, order_id: str) -> dict[str, object]:
        return self.details.get(order_id, {"status": 1, "statusName": "pending"})

    def buyer_order_status(self, **kwargs: object) -> dict[str, object]:
        page_num = int(kwargs.get("page_num") or 1)
        page_size = int(kwargs.get("page_size") or 100)
        start = (page_num - 1) * page_size
        end = start + page_size
        return {
            "page": page_num,
            "limit": page_size,
            "pages": max(1, (len(self.buyer_orders) + page_size - 1) // page_size),
            "total": len(self.buyer_orders),
            "list": self.buyer_orders[start:end],
        }


class UncertainC5SweeperClient(FakeC5SweeperClient):
    def batch_buy(self, **kwargs: object) -> dict[str, object]:
        self.batch_buy_calls.append(kwargs)
        return {}


class C5CaseSweeperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.state_path = root / "sweeper.json"
        self.db_path = root / "assistant.db"
        db = Database(self.db_path)
        db.initialize()
        db.upsert_items([
            CatalogItem("Kilowatt Case", "千瓦武器箱", "c5-kilowatt"),
            CatalogItem("Revolution Case", "革命武器箱", "c5-revolution"),
            CatalogItem("Glove Case", "手套武器箱", "c5-glove"),
        ])
        db.close()
        self.client = FakeC5SweeperClient()
        self.account_store = AccountStore(root / "config")
        self.account_store.add_account(
            id="account-1",
            name="test-account",
            steam_id64="76561197960265729",
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=test",
        )
        self.settings = Settings(c5_api_key="test-key", db_path=self.db_path)
        self.service = C5CaseSweeper(
            self.settings,
            client=self.client,
            account_store=self.account_store,
            state_path=self.state_path,
            now=lambda: datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc),
            start_worker=False,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tempdir.cleanup()

    def _create(
        self,
        *,
        market_hash_name: str = "Kilowatt Case",
        display_name: str | None = "千瓦武器箱",
        max_price: float = 1.10,
        budget: float = 100.0,
        target: int = 10,
    ) -> str:
        dashboard = self.service.create_round(
            market_hash_name=market_hash_name,
            display_name=display_name,
            receiving_account_id="account-1",
            max_price=max_price,
            budget=budget,
            target_count=target,
            interval_seconds=60,
        )
        return str(dashboard["round"]["id"])

    def _start(self, **kwargs: object) -> str:
        round_id = self._create(**kwargs)
        self.service.start("开始扫货", round_id=round_id)
        return round_id

    def test_arbitrary_catalog_case_is_used_by_batch_buy(self) -> None:
        round_id = self._start(
            market_hash_name="Revolution Case",
            display_name="革命武器箱",
            max_price=1.20,
            budget=50.0,
        )
        dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual(1, len(self.client.batch_buy_calls))
        self.assertEqual("c5-revolution", self.client.market_list_calls[0]["item_id"])
        self.assertIn("partner=1", str(self.client.batch_buy_calls[0]["trade_url"]))
        self.assertEqual("革命武器箱", dashboard["round"]["displayName"])
        self.assertEqual("test-account", dashboard["orders"][0]["receivingAccountName"])

    def test_one_cycle_sends_exactly_one_batch_buy_request(self) -> None:
        round_id = self._start()
        dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual(1, len(self.client.batch_buy_calls))
        self.assertEqual(1, dashboard["counts"]["accepted"])
        self.assertEqual(1, dashboard["counts"]["pending"])
        self.assertEqual(60, dashboard["round"]["intervalSeconds"])

    def test_submission_interval_is_always_normalized_to_sixty_seconds(self) -> None:
        dashboard = self.service.create_round(
            market_hash_name="Kilowatt Case",
            display_name="千瓦武器箱",
            receiving_account_id="account-1",
            max_price=1.10,
            budget=100.0,
            target_count=10,
            interval_seconds=30,
        )

        self.assertEqual(60, dashboard["round"]["intervalSeconds"])

    def test_one_cycle_batches_all_eligible_products_in_one_request(self) -> None:
        self.client.listings = [
            {"productId": "p-low", "price": 1.00, "delivery": 2},
            {"productId": "p-mid", "price": 1.05, "delivery": 2},
            {"productId": "p-limit", "price": 1.10, "delivery": 2},
            {"productId": "p-high", "price": 1.11, "delivery": 2},
        ]
        round_id = self._start(max_price=1.10, budget=3.00, target=10)
        dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual(1, len(self.client.market_list_calls))
        self.assertEqual(1, len(self.client.batch_buy_calls))
        product_list = self.client.batch_buy_calls[0]["product_list"]
        self.assertEqual(["p-low", "p-mid"], [row["productId"] for row in product_list])
        self.assertEqual(2, dashboard["counts"]["accepted"])
        self.assertEqual(2, dashboard["counts"]["pending"])
        self.assertEqual(2.05, dashboard["money"]["committedAmount"])

    def test_batch_buy_mixed_result_keeps_success_and_releases_failed_amount(self) -> None:
        self.client.listings = [
            {"productId": "p-success", "price": 1.00, "delivery": 2},
            {"productId": "p-failed", "price": 1.05, "delivery": 2},
        ]

        def mixed_result(**kwargs: object) -> dict[str, object]:
            self.client.batch_buy_calls.append(kwargs)
            rows = list(kwargs["product_list"])
            return {
                "successNum": 1,
                "failNum": 1,
                "successList": [{
                    **rows[0],
                    "actualPay": 1.00,
                    "orderAssetId": "asset-order-success",
                    "orderId": "trade-order-success",
                }],
                "failedList": [{
                    **rows[1],
                    "errorCode": "PRICE_CHANGED",
                    "errorMsg": "价格已变化",
                }],
            }

        with patch.object(self.client, "batch_buy", side_effect=mixed_result):
            round_id = self._start(max_price=1.10, budget=2.05, target=2)
            dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual(1, len(self.client.batch_buy_calls))
        self.assertEqual(1, dashboard["counts"]["pending"])
        self.assertEqual(1, dashboard["counts"]["failed"])
        self.assertEqual(1.00, dashboard["money"]["committedAmount"])
        self.assertEqual(1.05, dashboard["money"]["remainingBudget"])

    def test_success_without_order_asset_id_pauses_and_holds_budget(self) -> None:
        def missing_order_asset_id(**kwargs: object) -> dict[str, object]:
            self.client.batch_buy_calls.append(kwargs)
            row = list(kwargs["product_list"])[0]
            return {
                "successNum": 1,
                "failNum": 0,
                "successList": [{**row, "actualPay": 1.10, "orderId": "trade-order-only"}],
                "failedList": [],
            }

        with patch.object(self.client, "batch_buy", side_effect=missing_order_asset_id):
            round_id = self._start(max_price=1.10, budget=2.20, target=2)
            dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual("paused", dashboard["round"]["status"])
        self.assertEqual("buy_uncertain", dashboard["round"]["stopReason"])
        self.assertEqual(1, dashboard["counts"]["pending"])
        self.assertEqual(1.10, dashboard["money"]["committedAmount"])
        self.assertEqual(1, len(self.client.batch_buy_calls))

    def test_market_search_failure_does_not_submit_purchase(self) -> None:
        with patch.object(self.client, "market_products_list", side_effect=RuntimeError("temporary error")):
            round_id = self._start()
            dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual([], self.client.batch_buy_calls)
        self.assertEqual("market_search_failed", dashboard["events"][0]["status"])

    def test_no_eligible_market_listing_does_not_submit_purchase(self) -> None:
        self.client.listings = [
            {"productId": "too-expensive", "price": 1.11, "delivery": 2},
            {"productId": "malformed", "delivery": 2},
        ]
        round_id = self._start(max_price=1.10)
        dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual([], self.client.batch_buy_calls)
        self.assertEqual("no_batch_candidates", dashboard["events"][0]["status"])

    def test_batch_selection_never_exceeds_remaining_target_count(self) -> None:
        self.client.listings = [
            {"productId": f"p-{index}", "price": 1.00, "delivery": 2}
            for index in range(5)
        ]
        round_id = self._start(max_price=1.10, budget=10.0, target=2)
        dashboard = self.service.run_cycle(round_id=round_id)

        product_list = self.client.batch_buy_calls[0]["product_list"]
        self.assertEqual(2, len(product_list))
        self.assertEqual(2, dashboard["counts"]["pending"])

    def test_market_pages_are_merged_before_one_batch_buy_request(self) -> None:
        self.client.listings = [
            {"productId": f"p-{index:03d}", "price": 1.00, "delivery": 2}
            for index in range(80)
        ]
        round_id = self._start(max_price=1.10, budget=100.0, target=100)
        dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual([1, 2], [row["page_num"] for row in self.client.market_list_calls])
        self.assertEqual([50, 50], [row["page_size"] for row in self.client.market_list_calls])
        self.assertEqual(1, len(self.client.batch_buy_calls))
        self.assertEqual(80, len(self.client.batch_buy_calls[0]["product_list"]))
        self.assertEqual(80, dashboard["counts"]["pending"])

    def test_repeated_start_cannot_bypass_interval(self) -> None:
        round_id = self._start()
        with self.assertRaisesRegex(ValueError, "正在运行"):
            self.service.start("开始扫货", round_id=round_id)
        self.assertEqual([], self.client.batch_buy_calls)

    def test_empty_batch_response_pauses_round(self) -> None:
        uncertain_client = UncertainC5SweeperClient()
        self.service.client = uncertain_client
        round_id = self._start()
        dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual("paused", dashboard["round"]["status"])
        self.assertEqual("buy_uncertain", dashboard["round"]["stopReason"])
        self.assertEqual(1, len(uncertain_client.batch_buy_calls))

    def test_batch_timeout_is_persisted_paused_and_reconciled_by_product_id(self) -> None:
        with patch.object(
            self.client,
            "batch_buy",
            side_effect=RuntimeError("504 Gateway Time-out"),
        ) as batch_buy:
            round_id = self._start()
            timed_out = self.service.run_cycle(round_id=round_id)

        self.assertEqual("paused", timed_out["round"]["status"])
        self.assertEqual("buy_uncertain", timed_out["round"]["stopReason"])
        submission = timed_out["round"]["submissions"][0]
        self.assertEqual("uncertain", submission["status"])
        self.assertEqual(1, len(submission["products"]))
        with self.assertRaisesRegex(ValueError, "购买结果不确定"):
            self.service.start("开始扫货", round_id=round_id)

        product = submission["products"][0]
        self.client.buyer_orders = [{
            "orderId": "remote-order-1",
            "productId": product["productId"],
            "price": product["buyPrice"],
            "status": 10,
            "statusName": "success",
            "receiveSteamId": "76561197960265729",
            "createTime": 1783831070,
        }]
        reconciled = self.service.run_cycle(allow_buy=False, round_id=round_id)

        self.assertEqual("reconciled", reconciled["round"]["submissions"][0]["status"])
        self.assertEqual(1, reconciled["counts"]["delivered"])
        self.assertEqual("remote-order-1", reconciled["orders"][0]["orderAssetId"])
        self.assertEqual("running", reconciled["round"]["status"])
        self.assertEqual(1, batch_buy.call_count)

    def test_partial_reconcile_keeps_paused_until_manual_confirm(self) -> None:
        self.client.listings = [
            {"productId": "product-1", "price": 1.00, "delivery": 2},
            {"productId": "product-absent", "price": 1.10, "delivery": 2},
        ]
        with patch.object(
            self.client,
            "batch_buy",
            side_effect=RuntimeError("504 Gateway Time-out"),
        ):
            round_id = self._start(max_price=1.10, budget=10.0, target=5)
            timed_out = self.service.run_cycle(round_id=round_id)

        self.assertEqual("paused", timed_out["round"]["status"])
        self.assertEqual(2, len(timed_out["round"]["submissions"][0]["products"]))

        self.client.buyer_orders = [{
            "orderId": "remote-order-1",
            "productId": "product-1",
            "price": 1.00,
            "status": 10,
            "statusName": "success",
            "receiveSteamId": "76561197960265729",
            "createTime": 1783831070,
        }]
        partial = self.service.run_cycle(allow_buy=False, round_id=round_id)
        self.assertEqual("paused", partial["round"]["status"])
        self.assertEqual("uncertain", partial["round"]["submissions"][0]["status"])
        self.assertEqual(["product-absent"], partial["round"]["submissions"][0]["unresolvedProductIds"])
        self.assertEqual(1, partial["round"]["submissions"][0]["matchedCount"])

        resumed = self.service.confirm_unresolved_not_bought(round_id=round_id)
        self.assertEqual("running", resumed["round"]["status"])
        self.assertIsNone(resumed["round"]["stopReason"])
        submission = resumed["round"]["submissions"][0]
        self.assertEqual("reconciled", submission["status"])
        self.assertEqual([], submission["unresolvedProductIds"])
        self.assertEqual(1, len(submission["notBoughtProducts"]))
        self.assertEqual("product-absent", submission["notBoughtProducts"][0]["productId"])
        self.assertEqual(
            1,
            len([event for event in resumed["events"] if event["status"] == "manual_not_bought"]),
        )

    def test_manual_confirm_rejected_when_round_not_uncertain(self) -> None:
        round_id = self._start()
        with self.assertRaisesRegex(ValueError, "购买结果不确定"):
            self.service.confirm_unresolved_not_bought(round_id=round_id)

    def test_absent_product_auto_confirms_not_bought_and_resumes(self) -> None:
        self.client.listings = [
            {"productId": "product-1", "price": 1.00, "delivery": 2},
            {"productId": "product-absent", "price": 1.10, "delivery": 2},
        ]
        with patch.object(
            self.client,
            "batch_buy",
            side_effect=RuntimeError("504 Gateway Time-out"),
        ):
            round_id = self._start(max_price=1.10, budget=10.0, target=5)
            self.service.run_cycle(round_id=round_id)

        self.client.buyer_orders = [{
            "orderId": "remote-order-1",
            "productId": "product-1",
            "price": 1.00,
            "status": 10,
            "statusName": "success",
            "receiveSteamId": "76561197960265729",
            "createTime": 1783831070,
        }]

        first = self.service.run_cycle(allow_buy=False, round_id=round_id)
        self.assertEqual("paused", first["round"]["status"])
        self.assertEqual(["product-absent"], first["round"]["submissions"][0]["unresolvedProductIds"])

        second = self.service.run_cycle(allow_buy=False, round_id=round_id)
        self.assertEqual("paused", second["round"]["status"])
        self.assertEqual(["product-absent"], second["round"]["submissions"][0]["unresolvedProductIds"])

        third = self.service.run_cycle(allow_buy=False, round_id=round_id)
        self.assertEqual("running", third["round"]["status"])
        submission = third["round"]["submissions"][0]
        self.assertEqual("reconciled", submission["status"])
        self.assertEqual([], submission["unresolvedProductIds"])
        self.assertEqual(1, len(submission["notBoughtProducts"]))
        self.assertEqual("product-absent", submission["notBoughtProducts"][0]["productId"])
        self.assertTrue(submission["notBoughtProducts"][0].get("autoConfirmed"))
        self.assertEqual(
            1,
            len([event for event in third["events"] if event["status"] == "auto_not_bought"]),
        )

    def test_price_above_limit_waits_without_buying_or_spamming_events(self) -> None:
        self.client.price = 1.11
        round_id = self._start(max_price=1.10)
        first = self.service.run_cycle(round_id=round_id)
        second = self.service.run_cycle(round_id=round_id)

        self.assertEqual([], self.client.batch_buy_calls)
        self.assertEqual("price_too_high", first["events"][0]["status"])
        self.assertEqual(1, len([row for row in second["events"] if row["status"] == "price_too_high"]))

    def test_accepted_is_pending_until_later_audit_confirms_delivery(self) -> None:
        round_id = self._start()
        first = self.service.run_cycle(round_id=round_id)
        self.assertEqual(0, first["counts"]["delivered"])
        self.assertEqual(1, first["counts"]["pending"])

        self.client.price = 1.20
        self.client.details["asset-order-1-1"] = {"status": 10, "statusName": "success"}
        second = self.service.run_cycle(round_id=round_id)

        self.assertEqual(1, second["counts"]["delivered"])
        self.assertEqual(0, second["counts"]["pending"])
        self.assertEqual(1, len(self.client.batch_buy_calls))

    def test_failed_delivery_releases_budget_for_next_cycle(self) -> None:
        round_id = self._start(budget=1.10, target=2)
        self.service.run_cycle(round_id=round_id)
        self.client.details["asset-order-1-1"] = {
            "status": 11,
            "statusName": "failed",
            "failedCode": "ORDER_DELIVER_FAILED",
        }
        dashboard = self.service.run_cycle(round_id=round_id)

        self.assertEqual(1, dashboard["counts"]["failed"])
        self.assertEqual(1, dashboard["counts"]["pending"])
        self.assertEqual(1.10, dashboard["money"]["committedAmount"])
        self.assertEqual(2, len(self.client.batch_buy_calls))

    def test_budget_is_independent_and_stops_next_purchase(self) -> None:
        round_id = self._start(max_price=1.20, budget=1.50, target=10)
        first = self.service.run_cycle(round_id=round_id)
        self.assertEqual(1.50, first["money"]["budget"])
        self.assertEqual(0.40, first["money"]["remainingBudget"])

        self.client.details["asset-order-1-1"] = {"status": 10, "statusName": "success"}
        completed = self.service.run_cycle(round_id=round_id)

        self.assertEqual("completed", completed["round"]["status"])
        self.assertEqual("budget_limit", completed["round"]["stopReason"])
        self.assertEqual(1, len(self.client.batch_buy_calls))

    def test_pending_target_stops_new_buys_but_keeps_auditing(self) -> None:
        round_id = self._start(target=1)
        self.service.run_cycle(round_id=round_id)
        second = self.service.run_cycle(round_id=round_id)
        self.assertEqual(1, len(self.client.batch_buy_calls))
        self.assertEqual("running", second["round"]["status"])
        self.assertEqual("waiting_delivery", second["events"][0]["status"])

        self.client.details["asset-order-1-1"] = {"status": 10, "statusName": "success"}
        final = self.service.run_cycle(round_id=round_id)
        self.assertEqual("completed", final["round"]["status"])
        self.assertEqual("target_reached", final["round"]["stopReason"])

    def test_new_round_requires_previous_round_to_finish_or_stop(self) -> None:
        first_id = self._create(budget=30.0)
        with self.assertRaisesRegex(ValueError, "尚未结束"):
            self._create(market_hash_name="Glove Case", display_name="手套武器箱")

        self.service.stop(round_id=first_id)
        second_id = self._create(
            market_hash_name="Glove Case",
            display_name="手套武器箱",
            max_price=12.0,
            budget=500.0,
            target=50,
        )
        dashboard = self.service.dashboard(second_id)

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(2, dashboard["round"]["roundNumber"])
        self.assertEqual(0, dashboard["counts"]["accepted"])
        self.assertEqual(2, len(dashboard["rounds"]))
        self.assertEqual("stopped", dashboard["rounds"][1]["status"])

    def test_update_paused_round_cannot_reduce_budget_below_commitment(self) -> None:
        round_id = self._start(budget=10.0)
        self.service.run_cycle(round_id=round_id)
        self.service.pause(round_id=round_id)
        with self.assertRaisesRegex(ValueError, "已占用金额"):
            self.service.update_round(
                round_id,
                market_hash_name="Kilowatt Case",
                display_name="千瓦武器箱",
                receiving_account_id="account-1",
                max_price=1.10,
                budget=0.50,
                target_count=10,
            )

    def test_paused_refresh_never_buys(self) -> None:
        round_id = self._create()
        dashboard = self.service.run_cycle(allow_buy=False, round_id=round_id)
        self.assertEqual([], self.client.batch_buy_calls)
        self.assertEqual(1.10, dashboard["round"]["lastPrice"])

    def test_restart_turns_running_round_into_paused(self) -> None:
        round_id = self._start()
        self.service.close()
        restarted = C5CaseSweeper(
            self.settings,
            client=self.client,
            account_store=self.account_store,
            state_path=self.state_path,
            start_worker=False,
        )
        try:
            dashboard = restarted.dashboard(round_id)
            self.assertEqual("paused", dashboard["round"]["status"])
            self.assertFalse(dashboard["realExecutionRunning"])
        finally:
            restarted.close()

    def test_v2_task_is_migrated_to_first_independent_round(self) -> None:
        self.service.close()
        self.state_path.write_text(json.dumps({
            "version": 2,
            "task": {
                "marketHashName": "Kilowatt Case",
                "displayName": "千瓦武器箱",
                "maxPrice": 1.14,
                "targetCount": 100,
                "intervalSeconds": 60,
                "status": "paused",
                "orders": [],
                "events": [],
            },
        }, ensure_ascii=False), encoding="utf-8")
        migrated = C5CaseSweeper(
            self.settings,
            client=self.client,
            account_store=self.account_store,
            state_path=self.state_path,
            start_worker=False,
        )
        self.service = migrated
        dashboard = migrated.dashboard()
        self.assertEqual(1, dashboard["round"]["roundNumber"])
        self.assertEqual(114.0, dashboard["money"]["budget"])

    def test_catalog_search_supports_chinese_english_and_custom_name(self) -> None:
        chinese = self.service.search_items("革命")
        english = self.service.search_items("Glove")
        custom = self.service.search_items("Custom Event Case")

        self.assertEqual("Revolution Case", chinese[0]["marketHashName"])
        self.assertEqual("手套武器箱", english[0]["displayName"])
        self.assertTrue(custom[0]["custom"])

    def test_non_case_item_is_rejected_even_if_api_is_called_directly(self) -> None:
        with self.assertRaisesRegex(ValueError, "只支持武器箱"):
            self.service.create_round(
                market_hash_name="AK-47 | Neon Revolution (Field-Tested)",
                display_name="AK-47 | 霓虹革命（久经沙场）",
                receiving_account_id="account-1",
                max_price=100.0,
                budget=500.0,
                target_count=1,
            )

    def test_catalog_search_hides_skins_capsules_packages_and_case_keys(self) -> None:
        db = Database(self.db_path)
        db.upsert_items([
            CatalogItem("Kilowatt Case Key", "千瓦武器箱钥匙"),
            CatalogItem("Paris 2023 Legends Sticker Capsule", "巴黎胶囊"),
            CatalogItem("Anubis Collection Package", "阿努比斯收藏包"),
            CatalogItem("AK-47 | Neon Revolution (Field-Tested)", "AK-47 | 霓虹革命（久经沙场）"),
        ])
        db.close()

        values = self.service.search_items("")
        self.assertTrue(values)
        self.assertTrue(all(row["marketHashName"].endswith(" Case") for row in values))

    def test_catalog_search_prioritizes_exact_and_prefix_matches(self) -> None:
        results = self.service.search_items("Kilowatt Case")
        self.assertEqual("Kilowatt Case", results[0]["marketHashName"])

    def test_catalog_search_page_exposes_continuation_without_duplicates(self) -> None:
        first = self.service.search_items_page("", limit=1, offset=0)
        second = self.service.search_items_page("", limit=1, offset=1)

        self.assertTrue(first["pagination"]["hasMore"])
        self.assertEqual(1, first["pagination"]["nextOffset"])
        self.assertNotEqual(
            first["items"][0]["marketHashName"],
            second["items"][0]["marketHashName"],
        )

    def test_receiving_accounts_expose_safe_identity_and_c5_binding(self) -> None:
        accounts = self.service.receiving_accounts(refresh=True)
        self.assertEqual(1, len(accounts))
        self.assertEqual("test-account", accounts[0]["name"])
        self.assertTrue(accounts[0]["c5Bound"])
        self.assertTrue(accounts[0]["tradeUrlMatches"])
        self.assertTrue(accounts[0]["available"])
        self.assertNotIn("tradeUrl", accounts[0])

    def test_start_rejects_account_no_longer_bound_on_c5(self) -> None:
        round_id = self._create()
        self.client.steam_accounts = []
        with self.assertRaisesRegex(ValueError, "没有绑定在 C5"):
            self.service.start("开始扫货", round_id=round_id)

    def test_state_file_does_not_store_api_key(self) -> None:
        self._start()
        raw = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("test-key", raw)
        json.loads(raw)

    def test_save_locked_recovers_after_transient_replace_failure(self) -> None:
        self._create()
        original_replace = Path.replace
        calls = {"count": 0}

        def flaky_replace(path: Path, target: Path) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise PermissionError(13, "sharing violation", str(target))
            return original_replace(path, target)

        with patch.object(Path, "replace", flaky_replace):
            self.service._save_locked()

        self.assertEqual(2, calls["count"])
        self.assertTrue(self.state_path.exists())
        json.loads(self.state_path.read_text(encoding="utf-8"))
        leftovers = list(self.state_path.parent.glob(f"{self.state_path.name}.*.tmp"))
        self.assertEqual([], leftovers)

    def test_worker_survives_cycle_exception_and_keeps_running(self) -> None:
        round_id = self._start()
        with patch.object(
            self.service,
            "run_cycle",
            side_effect=RuntimeError("temporary failure"),
        ):
            self.service._run_due_round(round_id)

        dashboard = self.service.dashboard(round_id)
        self.assertEqual("running", dashboard["round"]["status"])
        self.assertIsNotNone(dashboard["round"]["nextRunAt"])
        self.assertIn("temporary failure", dashboard["workerError"] or "")

    def test_worker_records_persist_failure_and_keeps_round_running(self) -> None:
        round_id = self._start()
        with patch.object(
            self.service,
            "run_cycle",
            return_value=self.service.dashboard(round_id),
        ), patch.object(
            self.service,
            "_save_locked",
            side_effect=PermissionError(13, "locked"),
        ):
            self.service._run_due_round(round_id)

        dashboard = self.service.dashboard(round_id)
        self.assertEqual("running", dashboard["round"]["status"])
        self.assertIn("persist state failed", dashboard["workerError"] or "")


if __name__ == "__main__":
    unittest.main()
