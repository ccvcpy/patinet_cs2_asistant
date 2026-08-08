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

from cs2_assistant.clients import SteamMarketError
from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import CatalogItem, MarketState, StrategyConfig
import cs2_assistant.services.profit_trade as profit_trade
from cs2_assistant.services.market import MarketService
from cs2_assistant.services.runtime_controller import (
    RUNTIME_PROFIT_TRADE,
    TASK_PROFIT_SELECTION_WATCH,
    UnifiedRuntimeController,
)


class _NoopLogger:
    def emit(self, **_: object) -> None:
        return None


class _FakeC5:
    def __init__(self, prices: dict[str, float | None]) -> None:
        self.prices = dict(prices)
        self.price_batch_calls: list[list[str]] = []
        self.quick_buy_calls: list[dict[str, object]] = []

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, object]]:
        self.price_batch_calls.append(list(market_hash_names))
        return {
            name: {"price": price, "count": 5, "itemId": f"c5-{index}"}
            for index, name in enumerate(market_hash_names)
            if self.prices.get(name) is not None
            for price in [self.prices[name]]
        }

    def quick_buy(self, **kwargs: object) -> None:  # pragma: no cover - must never be called
        self.quick_buy_calls.append(dict(kwargs))
        raise AssertionError("selection watch must not quick-buy")


class _FakeSteam:
    account_id = "selection-account"
    steam_id64 = "selection-steam"

    def __init__(
        self,
        *,
        prices: dict[str, int] | None = None,
        buy_prices: dict[str, int | None] | None = None,
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.prices = dict(prices or {})
        self.buy_prices = dict(buy_prices or {})
        self.failures = dict(failures or {})
        self.order_book_calls: list[dict[str, object]] = []
        self.forbidden_calls: list[str] = []

    def order_book(self, *, app_id: int, market_hash_name: str, **kwargs: object) -> dict[str, object]:
        self.order_book_calls.append(
            {"app_id": app_id, "market_hash_name": market_hash_name, **dict(kwargs)}
        )
        failure = self.failures.get(market_hash_name)
        if failure is not None:
            raise failure
        sell = self.prices.get(market_hash_name, 10000)
        buy = self.buy_prices.get(
            market_hash_name,
            max(1, int(round(sell * 0.95))),
        )
        return {
            "success": True,
            "data": {
                "eCurrency": 23,
                "cSellOrders": 4,
                "cBuyOrders": 3,
                "rgCompactSellOrders": [sell, 2, sell + 100, 2],
                "rgCompactBuyOrders": [] if buy is None else [buy, 3],
            },
        }

    def search_listings(self, **_: object) -> None:  # pragma: no cover - must never be called
        self.forbidden_calls.append("search_listings")
        raise AssertionError("selection watch must not call search_listings")

    def buy_listing(self, **_: object) -> None:  # pragma: no cover - must never be called
        self.forbidden_calls.append("buy_listing")
        raise AssertionError("selection watch must not buy_listing")

    def create_buy_order(self, **_: object) -> None:  # pragma: no cover - must never be called
        self.forbidden_calls.append("create_buy_order")
        raise AssertionError("selection watch must not create_buy_order")

    def sell_item(self, **_: object) -> None:  # pragma: no cover - must never be called
        self.forbidden_calls.append("sell_item")
        raise AssertionError("selection watch must not sell_item")


class _CrossedProbeSteam(_FakeSteam):
    def __init__(
        self,
        *,
        listing_payload: dict[str, object] | None = None,
        listing_error: Exception | None = None,
    ) -> None:
        super().__init__(prices={"probe": 1602}, buy_prices={"probe": 1663})
        self.listing_payload = listing_payload or {"listinginfo": {}}
        self.listing_error = listing_error
        self.search_listings_calls: list[dict[str, object]] = []

    def search_listings(self, **kwargs: object) -> dict[str, object]:
        self.search_listings_calls.append(dict(kwargs))
        if self.listing_error is not None:
            raise self.listing_error
        return dict(self.listing_payload)


class ProfitTradeSelectionWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.temp_dir.name) / "assistant.db")
        self.config = StrategyConfig(
            profit_trade_balance_discount=0.69,
            profit_trade_c5_current_sale_net_factor=0.99,
            # Selection research is deliberately allowed beneath this ordinary
            # execution threshold; keeping it high makes that distinction testable.
            profit_trade_min_item_value=50.0,
        )
        self._seed_catalog(
            "AK-47 | Research One (Field-Tested)",
            "研究一号",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_catalog(self, market_hash_name: str, name_cn: str | None = None) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.upsert_items(
                [
                    CatalogItem(
                        market_hash_name=market_hash_name,
                        name_cn=name_cn or market_hash_name,
                    )
                ]
            )
        finally:
            db.close()

    def _service(
        self,
        *,
        prices: dict[str, float | None],
        steam_prices: dict[str, int] | None = None,
        steam_buy_prices: dict[str, int | None] | None = None,
        failures: dict[str, Exception] | None = None,
    ) -> tuple[MarketService, _FakeC5, _FakeSteam]:
        c5 = _FakeC5(prices)
        steam = _FakeSteam(
            prices=steam_prices,
            buy_prices=steam_buy_prices,
            failures=failures,
        )
        return MarketService(c5_client=c5, steam_market_client=steam, include_c5_purchase_prices=False), c5, steam

    def _add(self, market_hash_name: str) -> dict[str, object]:
        return profit_trade.update_profit_trade_selection_watch(
            self.settings,
            action="add",
            market_hash_name=market_hash_name,
        )

    def test_crossed_listing_probe_only_queries_once_and_matches_buyer_total(self) -> None:
        client = _CrossedProbeSteam(
            listing_payload={
                "listinginfo": {
                    "123456": {
                        "listingid": "123456",
                        "market_hash_name": "probe",
                        "converted_currencyid": 23,
                        "converted_price": 1393,
                        "converted_fee": 209,
                        "converted_total": 1602,
                    }
                }
            }
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            result = profit_trade._probe_crossed_orderbook_listing(
                settings=self.settings,
                config=self.config,
                db=db,
                client=client,
                market_hash_name="probe",
                snapshot={
                    "currencyId": 23,
                    "currencyValid": True,
                    "sellerFloorPrice": 16.02,
                    "buyerMaxPrice": 16.63,
                    "crossed": True,
                },
            )
        finally:
            db.close()

        self.assertEqual(1, len(client.search_listings_calls))
        self.assertEqual("matched", result["status"])
        self.assertEqual("123456", result["listingId"])
        self.assertEqual(16.02, result["listingTotal"])
        self.assertTrue(result["priceMatchesFloor"])
        self.assertFalse(result["purchaseAttempted"])

    def test_crossed_listing_probe_saves_mismatch_and_does_not_hide_listing(self) -> None:
        client = _CrossedProbeSteam(
            listing_payload={
                "listinginfo": {
                    "different-floor": {
                        "listingid": "different-floor",
                        "market_hash_name": "probe",
                        "converted_currencyid": 23,
                        "converted_price": 1500,
                        "converted_fee": 225,
                        "converted_total": 1725,
                    }
                }
            }
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            result = profit_trade._probe_crossed_orderbook_listing(
                settings=self.settings,
                config=self.config,
                db=db,
                client=client,
                market_hash_name="probe",
                snapshot={
                    "currencyId": 23,
                    "currencyValid": True,
                    "sellerFloorPrice": 16.02,
                    "buyerMaxPrice": 16.63,
                    "crossed": True,
                },
            )
        finally:
            db.close()

        self.assertEqual("floor_mismatch", result["status"])
        self.assertEqual("different-floor", result["listingId"])
        self.assertEqual(17.25, result["listingTotal"])
        self.assertFalse(result["priceMatchesFloor"])

    def test_non_crossed_snapshot_never_queries_listings(self) -> None:
        client = _CrossedProbeSteam()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            result = profit_trade._probe_crossed_orderbook_listing(
                settings=self.settings,
                config=self.config,
                db=db,
                client=client,
                market_hash_name="probe",
                snapshot={
                    "currencyId": 23,
                    "currencyValid": True,
                    "sellerFloorPrice": 16.02,
                    "buyerMaxPrice": 15.00,
                    "crossed": False,
                },
            )
        finally:
            db.close()

        self.assertIsNone(result)
        self.assertEqual([], client.search_listings_calls)

    def test_crossed_listing_probe_429_opens_shared_circuit_without_retry(self) -> None:
        client = _CrossedProbeSteam(
            listing_error=SteamMarketError(
                "Steam listings rate limited",
                status_code=429,
                retry_after="120",
            )
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            result = profit_trade._probe_crossed_orderbook_listing(
                settings=self.settings,
                config=self.config,
                db=db,
                client=client,
                market_hash_name="probe",
                snapshot={
                    "currencyId": 23,
                    "currencyValid": True,
                    "sellerFloorPrice": 16.02,
                    "buyerMaxPrice": 16.63,
                    "crossed": True,
                },
            )
            circuit = profit_trade._get_profit_trade_listings_circuit(db)
        finally:
            db.close()

        self.assertEqual(1, len(client.search_listings_calls))
        self.assertEqual("rate_limited", result["status"])
        self.assertEqual("open", circuit["status"])

    def test_crossed_listing_probe_respects_existing_shared_circuit(self) -> None:
        client = _CrossedProbeSteam()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.set_profit_trade_runtime_state(
                profit_trade.PROFIT_TRADE_LISTINGS_CIRCUIT_KEY,
                {
                    "status": "open",
                    "first429At": "2026-07-23T00:00:00+00:00",
                    "cooldownUntil": "2099-07-23T00:10:00+00:00",
                },
            )
            result = profit_trade._probe_crossed_orderbook_listing(
                settings=self.settings,
                config=self.config,
                db=db,
                client=client,
                market_hash_name="probe",
                snapshot={
                    "currencyId": 23,
                    "currencyValid": True,
                    "sellerFloorPrice": 16.02,
                    "buyerMaxPrice": 16.63,
                    "crossed": True,
                },
            )
        finally:
            db.close()

        self.assertEqual("circuit_open", result["status"])
        self.assertEqual([], client.search_listings_calls)

    def test_crossed_selection_scan_never_searches_listings(self) -> None:
        name = "probe"
        self._seed_catalog(name, "交叉盘口测试")
        self._add(name)
        c5 = _FakeC5({name: 20.0})
        steam = _CrossedProbeSteam(
            listing_payload={
                "listinginfo": {
                    "probe-listing": {
                        "listingid": "probe-listing",
                        "market_hash_name": name,
                        "converted_currencyid": 23,
                        "converted_price": 1393,
                        "converted_fee": 209,
                        "converted_total": 1602,
                    }
                }
            }
        )
        service = MarketService(
            c5_client=c5,
            steam_market_client=steam,
            include_c5_purchase_prices=False,
        )
        with patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()):
            report = profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
                market_service=service,
                force=True,
            )

        self.assertEqual(1, report["observedCount"])
        self.assertEqual([], steam.search_listings_calls)
        current = profit_trade.build_profit_trade_selection_watch_payload(self.settings)["items"][0]
        history = profit_trade.build_profit_trade_selection_history_payload(
            self.settings,
            name,
        )["items"][0]
        for row in (current, history):
            self.assertTrue(row["steamOrderbook"]["crossed"])
            self.assertIsNone(row["crossedListingProbe"])

    def test_catalog_whitelist_lifecycle_and_research_only_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "local catalog"):
            self._add("arbitrary unimported market name")

        created = self._add("AK-47 | Research One (Field-Tested)")
        self.assertEqual("added", created["action"])
        self.assertTrue(created["researchOnly"])
        self.assertFalse(created["canExecute"])
        self.assertEqual("研究一号", created["item"]["name"])

        removed = profit_trade.update_profit_trade_selection_watch(
            self.settings,
            action="remove",
            market_hash_name="AK-47 | Research One (Field-Tested)",
        )
        self.assertEqual("removed", removed["action"])
        reentered = profit_trade.update_profit_trade_selection_watch(
            self.settings,
            action="reactivate",
            market_hash_name="AK-47 | Research One (Field-Tested)",
        )
        self.assertEqual("reactivated", reentered["action"])

        history = profit_trade.build_profit_trade_selection_history_payload(
            self.settings,
            "AK-47 | Research One (Field-Tested)",
        )
        self.assertEqual(
            ["reentered", "removed", "added"],
            [item["eventType"] for item in history["items"]],
        )

    def test_refresh_saves_negative_and_unavailable_without_trade_side_effects(self) -> None:
        low_name = "PP-Bizon | Low Research (Field-Tested)"
        unavailable_name = "P90 | No C5 Research (Field-Tested)"
        self._seed_catalog(low_name, "低价负收益")
        self._seed_catalog(unavailable_name, "暂无价格")
        for name in (
            "AK-47 | Research One (Field-Tested)",
            low_name,
            unavailable_name,
        ):
            self._add(name)
        service, c5, steam = self._service(
            prices={
                "AK-47 | Research One (Field-Tested)": 90.0,
                low_name: 0.50,
                unavailable_name: None,
            },
            steam_prices={
                "AK-47 | Research One (Field-Tested)": 10000,
                low_name: 100,
                unavailable_name: 100,
            },
        )
        with patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()):
            report = profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
                market_service=service,
            )
        self.assertEqual(3, report["observedCount"])
        self.assertEqual(3, len(steam.order_book_calls))
        self.assertEqual([], steam.forbidden_calls)
        self.assertEqual([], c5.quick_buy_calls)

        payload = profit_trade.build_profit_trade_selection_watch_payload(self.settings)
        by_name = {item["marketHashName"]: item for item in payload["items"]}
        self.assertLess(by_name[low_name]["expectedRoi"], 0.0)
        self.assertEqual("observed", by_name[low_name]["selectionStatus"])
        self.assertTrue(by_name[low_name]["active"])
        self.assertEqual("price_unavailable", by_name[unavailable_name]["selectionStatus"])
        self.assertTrue(by_name[unavailable_name]["active"])
        self.assertEqual(1.0, by_name[unavailable_name]["steamBuyPrice"])
        self.assertIsNone(by_name[unavailable_name]["c5ListingPrice"])
        self.assertIsNone(by_name[unavailable_name]["expectedRoi"])
        self.assertEqual(0, by_name[low_name]["inventoryCount"])
        self.assertEqual("selection_only", by_name[low_name]["executionStatus"])

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertEqual(0, db.conn.execute("SELECT COUNT(*) FROM profit_trades").fetchone()[0])
            self.assertEqual(0, db.conn.execute("SELECT COUNT(*) FROM asset_reservations").fetchone()[0])
            self.assertEqual(0, db.conn.execute("SELECT COUNT(*) FROM pool_operations").fetchone()[0])
        finally:
            db.close()

    def test_reuses_shared_market_state_without_a_second_remote_read(self) -> None:
        name = "AK-47 | Research One (Field-Tested)"
        self._add(name)
        shared = MarketState(
            market_hash_name=name,
            name_cn="研究一号",
            c5_sell_price=90.0,
            c5_price_source="c5_batch",
            steam_sell_price=100.0,
            steam_price_source="steam_orderbook",
            raw_json={
                "steam_orderbook_snapshot": {
                    "observedAt": "2026-07-23T00:00:00+00:00",
                    "currencyId": 23,
                    "currencyValid": True,
                    "sellerFloorPrice": 100.0,
                    "sellerFloorCount": 2,
                    "buyerMaxPrice": 95.0,
                    "buyerMaxCount": 3,
                    "spreadAmount": 5.0,
                    "spreadPct": 0.05,
                    "crossed": False,
                    "sellLevels": [{"price": 100.0, "count": 2}],
                    "buyLevels": [{"price": 95.0, "count": 3}],
                }
            },
        )
        service, c5, steam = self._service(prices={name: 90.0})
        with patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()):
            result = profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
                market_service=service,
                shared_state_map={name: shared},
            )
        self.assertEqual(1, result["reusedSnapshotCount"])
        self.assertEqual([], c5.price_batch_calls)
        self.assertEqual([], steam.order_book_calls)
        item = profit_trade.build_profit_trade_selection_watch_payload(self.settings)["items"][0]
        self.assertEqual(100.0, item["steamBuyPrice"])
        self.assertEqual(95.0, item["steamOrderbook"]["buyerMaxPrice"])
        self.assertEqual("valid", item["buyOrderReferenceStatus"])

    def test_forced_profit_cycle_refresh_ignores_future_selection_due_time(self) -> None:
        name = "AK-47 | Research One (Field-Tested)"
        self._add(name)
        first_service, _, first_steam = self._service(prices={name: 90.0})
        with patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()):
            first = profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
                market_service=first_service,
            )
        self.assertEqual(1, first["observedCount"])
        self.assertEqual(1, len(first_steam.order_book_calls))

        not_due_service, _, not_due_steam = self._service(prices={name: 90.0})
        with patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()):
            not_due = profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
                market_service=not_due_service,
            )
        self.assertEqual(0, not_due["dueCount"])
        self.assertEqual([], not_due_steam.order_book_calls)

        forced_service, _, forced_steam = self._service(prices={name: 90.0})
        with patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()):
            forced = profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
                market_service=forced_service,
                force=True,
            )
        self.assertEqual(1, forced["dueCount"])
        self.assertEqual(1, forced["observedCount"])
        self.assertEqual(1, len(forced_steam.order_book_calls))

    def test_429_stops_current_batch_and_defers_unstarted_items(self) -> None:
        names = [
            "AK-47 | Research One (Field-Tested)",
            "AUG | Research Two (Field-Tested)",
            "FAMAS | Research Three (Field-Tested)",
        ]
        self._seed_catalog(names[1], "研究二号")
        self._seed_catalog(names[2], "研究三号")
        for name in names:
            self._add(name)
        # The DB orders alphabetically: AK (success), AUG (429), FAMAS (must not call).
        service, _, steam = self._service(
            prices={name: 90.0 for name in names},
            failures={
                names[1]: SteamMarketError(
                    "Steam orderbook rate limited",
                    status_code=429,
                )
            },
        )
        with patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()):
            result = profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
                market_service=service,
            )
        self.assertTrue(result["rateLimited"])
        self.assertEqual(1, result["deferredCount"])
        self.assertEqual([names[0], names[1]], [call["market_hash_name"] for call in steam.order_book_calls])
        items = profit_trade.build_profit_trade_selection_watch_payload(self.settings)["items"]
        status_by_name = {item["marketHashName"]: item["selectionStatus"] for item in items}
        self.assertEqual("scan_failed", status_by_name[names[1]])
        self.assertEqual("scan_deferred", status_by_name[names[2]])

    def test_all_due_items_share_profit_cycle_interval_and_full_history_aggregate(self) -> None:
        names = [f"Glock-18 | Research {index} (Field-Tested)" for index in range(11)]
        for name in names:
            self._seed_catalog(name, name)
            self._add(name)
        service, _, steam = self._service(prices={name: 2.0 for name in names})
        with patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()):
            report = profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
                market_service=service,
            )
        self.assertEqual(11, report["dueCount"])
        self.assertEqual(11, report["observedCount"])
        self.assertEqual(11, len(steam.order_book_calls))
        watch_payload = profit_trade.build_profit_trade_selection_watch_payload(self.settings)
        self.assertEqual(
            profit_trade.PROFIT_TRADE_CYCLE_INTERVAL_SECONDS,
            watch_payload["scanIntervalSeconds"],
        )
        self.assertIsNone(watch_payload["maxItemsPerCycle"])
        self.assertTrue(watch_payload["scansAllActiveItems"])

        controller = UnifiedRuntimeController(self.settings)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            controller._seed_tasks(db)
            task = db.get_scheduled_task(TASK_PROFIT_SELECTION_WATCH)
            self.assertIsNotNone(task)
            self.assertEqual(RUNTIME_PROFIT_TRADE, task["source"])
            self.assertEqual(TASK_PROFIT_SELECTION_WATCH, task["task_type"])
            self.assertEqual(3, task["priority"])

            # Pagination must not change history max/min/average.  Insert three
            # observations directly so the API returns one row but aggregates all.
            name = names[0]
            # Remove the initial refresh observation for this one assertion so
            # the three deliberately chosen values are the complete history.
            db.conn.execute(
                "DELETE FROM profit_trade_selection_observations WHERE market_hash_name = ?",
                (name,),
            )
            db.conn.commit()
            for index, (roi, roi_basis) in enumerate(
                ((0.10, 0.68), (0.20, 0.69), (0.30, 0.73))
            ):
                db.record_profit_trade_selection_watch_scan(
                    [
                        {
                            "market_hash_name": name,
                            "name_cn": name,
                            "status": "observed",
                            "event_type": "observed",
                            "steam_buy_price": 10.0,
                            "steam_price_source": "steam_orderbook",
                            "c5_listing_price": 10.0,
                            "c5_price_source": "c5_batch",
                            "c5_expected_net_price": 10.0,
                            "balance_discount": roi_basis,
                            "expected_profit": roi * 10,
                            "expected_roi": roi,
                            "buy_order_reference_roi": roi + 0.01,
                            "buy_order_reference_profit": roi * 10 + 0.1,
                            "buy_order_reference_status": "valid",
                            "raw": {"steamOrderbook": {"currencyId": 23}},
                        }
                    ],
                    scan_id=f"history-{index}",
                    observed_at=f"2026-07-23T00:0{index}:00+00:00",
                )
        finally:
            db.close()
        history = profit_trade.build_profit_trade_selection_history_payload(
            self.settings,
            names[0],
            page_size=1,
        )
        self.assertEqual(1, len(history["items"]))
        self.assertAlmostEqual(0.30, history["summary"]["maxExpectedRoi"])
        self.assertAlmostEqual(0.10, history["summary"]["minExpectedRoi"])
        self.assertAlmostEqual(0.20, history["summary"]["avgExpectedRoi"])
        # The selection drawer intentionally reuses the inventory history
        # component, which reads this exact compatibility contract rather than
        # the selection-specific summary names above.
        self.assertAlmostEqual(0.30, history["stats"]["highestRoi"])
        self.assertAlmostEqual(0.20, history["stats"]["averageRoi"])
        self.assertIsNone(history["stats"]["roiBasis"])
        self.assertAlmostEqual(0.68, history["stats"]["roiBasisMin"])
        self.assertAlmostEqual(0.73, history["stats"]["roiBasisMax"])
        self.assertEqual(3, history["stats"]["validObservationCount"])
        self.assertAlmostEqual(1.0, history["summary"]["positiveRoiShare"])
        self.assertGreater(history["summary"]["durationSeconds"], 0)
        self.assertEqual(
            ["high", "good", "low", "negative"],
            [bucket["key"] for bucket in history["summary"]["roiDurationBuckets"]],
        )
        self.assertEqual(3, history["trend"]["totalValidPoints"])
        self.assertFalse(history["trend"]["sampled"])
        self.assertEqual(
            [0.10, 0.20, 0.30],
            [point["expectedRoi"] for point in history["trend"]["points"]],
        )
        for actual, expected in zip(
            [point["buyOrderReferenceRoi"] for point in history["trend"]["points"]],
            [0.11, 0.21, 0.31],
            strict=True,
        ):
            self.assertAlmostEqual(expected, actual)

    def test_no_relogin_selection_service_is_requested_by_the_runtime_path(self) -> None:
        """The independent selection constructor must explicitly disable relogin."""

        name = "AK-47 | Research One (Field-Tested)"
        self._add(name)
        service, _, _ = self._service(prices={name: 90.0})
        calls: list[dict[str, object]] = []

        def build_safe_service(*args: object, **kwargs: object) -> MarketService:
            calls.append(dict(kwargs))
            return service

        with (
            patch.object(profit_trade, "_build_profit_trade_market_service", side_effect=build_safe_service),
            patch.object(profit_trade, "get_profit_trade_event_logger", return_value=_NoopLogger()),
        ):
            profit_trade.refresh_profit_trade_selection_watch(
                self.settings,
                config=self.config,
            )
        self.assertEqual(1, len(calls))
        self.assertIs(False, calls[0]["allow_relogin"])

    def test_manual_selection_refresh_queues_research_task_without_executor_enable(self) -> None:
        self._add("AK-47 | Research One (Field-Tested)")
        controller = UnifiedRuntimeController(self.settings)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            controller._seed_tasks(db)
        finally:
            db.close()

        result = controller.profit_selection_watch_now()

        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertTrue(result["researchOnly"])
        self.assertFalse(result["canExecute"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            task = db.get_scheduled_task(TASK_PROFIT_SELECTION_WATCH)
            self.assertIsNotNone(task)
            self.assertEqual("pending", task["status"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
