from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.clients.c5game import C5GameError
from cs2_assistant.clients.steam_market import SteamMarketError
from cs2_assistant.config import Settings
from cs2_assistant.services.c5_research_scan import (
    create_c5_research_scan,
    get_c5_research_scan,
    initialize_c5_research_schema,
    list_c5_research_results,
    run_c5_research_scan_chunk,
    set_c5_research_scan_action,
)


def _catalog_item(name: str, index: int) -> dict[str, object]:
    return {
        "market_hash_name": name,
        "name_cn": f"研究饰品 {index}",
        "itemClassId": "skins_not_grouped",
        "rarityId": "rarity_mythical_weapon",
        "wearId": "SFUI_InvTooltip_Wear_Amount_2",
        "stattrak": False,
        "souvenir": False,
    }


class _FakeC5:
    def __init__(
        self,
        prices: dict[str, float | None],
        events: list[tuple[str, object]],
        failure: Exception | None = None,
    ) -> None:
        self.prices = dict(prices)
        self.events = events
        self.failure = failure
        self.price_batch_calls: list[list[str]] = []
        self.forbidden_calls: list[str] = []

    def price_batch(
        self,
        market_hash_names: list[str],
        app_id: int = 730,
    ) -> dict[str, dict[str, object]]:
        names = list(market_hash_names)
        self.price_batch_calls.append(names)
        self.events.append(("c5.price_batch", names))
        if self.failure is not None:
            raise self.failure
        return {
            name: {
                "price": self.prices[name],
                "count": 8,
                "itemId": f"c5-{index}",
            }
            for index, name in enumerate(names)
            if self.prices.get(name) is not None
        }

    def quick_buy(self, **_: object) -> None:  # pragma: no cover - forbidden boundary
        self.forbidden_calls.append("quick_buy")
        raise AssertionError("research scan must not quick-buy")

    def sale_create(self, **_: object) -> None:  # pragma: no cover - forbidden boundary
        self.forbidden_calls.append("sale_create")
        raise AssertionError("research scan must not create a C5 sale")

    def purchase_create(self, **_: object) -> None:  # pragma: no cover - forbidden boundary
        self.forbidden_calls.append("purchase_create")
        raise AssertionError("research scan must not create a C5 purchase")


class _FakeSteam:
    def __init__(
        self,
        prices_minor: dict[str, int],
        events: list[tuple[str, object]],
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.prices_minor = dict(prices_minor)
        self.events = events
        self.failures = dict(failures or {})
        self.order_book_calls: list[str] = []
        self.forbidden_calls: list[str] = []

    def order_book(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        **_: object,
    ) -> dict[str, object]:
        self.order_book_calls.append(market_hash_name)
        self.events.append(("steam.order_book", market_hash_name))
        failure = self.failures.get(market_hash_name)
        if failure is not None:
            raise failure
        sell = int(self.prices_minor[market_hash_name])
        return {
            "success": True,
            "data": {
                "eCurrency": 23,
                "cSellOrders": 4,
                "cBuyOrders": 3,
                "rgCompactSellOrders": [sell, 2, sell + 100, 2],
                "rgCompactBuyOrders": [max(1, sell - 100), 3],
            },
        }

    def search_listings(self, **_: object) -> None:  # pragma: no cover
        self.forbidden_calls.append("search_listings")
        raise AssertionError("research scan must not search listings")

    def buy_listing(self, **_: object) -> None:  # pragma: no cover
        self.forbidden_calls.append("buy_listing")
        raise AssertionError("research scan must not buy a listing")

    def create_buy_order(self, **_: object) -> None:  # pragma: no cover
        self.forbidden_calls.append("create_buy_order")
        raise AssertionError("research scan must not create a buy order")

    def sell_item(self, **_: object) -> None:  # pragma: no cover
        self.forbidden_calls.append("sell_item")
        raise AssertionError("research scan must not list an item")

    def remove_listing(self, **_: object) -> None:  # pragma: no cover
        self.forbidden_calls.append("remove_listing")
        raise AssertionError("research scan must not remove a listing")


class _FakeMarketService:
    def __init__(self, c5: _FakeC5, steam: _FakeSteam) -> None:
        self.c5_client = c5
        self.steam_market_clients = [steam]


class C5ResearchScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings = Settings(
            db_path=Path(self.temp_dir.name) / "assistant.db",
        )

    def _create(
        self,
        names: list[str],
        filters: dict[str, object] | None = None,
    ) -> dict[str, object]:
        items = [_catalog_item(name, index) for index, name in enumerate(names)]
        with patch(
            "cs2_assistant.services.c5_research_scan._filter_catalog_items",
            return_value=items,
        ) as catalog_filter:
            created = create_c5_research_scan(self.settings, filters or {})
        catalog_filter.assert_called_once_with(self.settings, filters or {})
        return created

    def test_create_has_202_queue_semantics_and_persists_only_research_tables(self) -> None:
        initialize_c5_research_schema(self.settings)
        created = self._create(["alpha", "beta"], {"rarityIds": ["rare"]})

        self.assertTrue(str(created["requestId"]).startswith("C5RS-"))
        self.assertEqual(202, created["httpStatus"])
        self.assertEqual("queued", created["status"])
        self.assertTrue(created["queued"])
        self.assertTrue(created["accepted"])
        self.assertTrue(created["researchOnly"])
        self.assertFalse(created["canExecute"])
        self.assertEqual(2, created["matchedCount"])

        persisted = get_c5_research_scan(self.settings, str(created["requestId"]))
        self.assertEqual(created["requestId"], persisted["requestId"])
        self.assertEqual({"rarityIds": ["rare"]}, persisted["filters"])

        with closing(sqlite3.connect(self.settings.db_path)) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("c5_research_scan_jobs", tables)
        self.assertIn("c5_research_scan_results", tables)
        self.assertTrue(
            {
                "profit_trades",
                "inventory_pool",
                "inventory_assets",
                "pool_operations",
            }.isdisjoint(tables)
        )

    def test_c5_price_filter_runs_before_steam_and_never_calls_trading_methods(self) -> None:
        names = ["inside", "too-expensive", "missing-c5"]
        created = self._create(
            names,
            {"priceMin": 10, "priceMax": 20},
        )
        events: list[tuple[str, object]] = []
        c5 = _FakeC5(
            {"inside": 15.0, "too-expensive": 25.0, "missing-c5": None},
            events,
        )
        steam = _FakeSteam({"inside": 1000}, events)

        # Sentinels prove this service never updates business transaction rows.
        with closing(sqlite3.connect(self.settings.db_path)) as conn:
            for table in (
                "profit_trades",
                "inventory_pool",
                "inventory_assets",
                "pool_operations",
            ):
                conn.execute(f"CREATE TABLE {table} (marker TEXT NOT NULL)")
                conn.execute(f"INSERT INTO {table} (marker) VALUES ('untouched')")
            conn.commit()

        result = run_c5_research_scan_chunk(
            self.settings,
            str(created["requestId"]),
            chunk_size=50,
            market_service=_FakeMarketService(c5, steam),
        )

        self.assertEqual("completed_with_errors", result["status"])
        self.assertTrue(result["terminal"])
        self.assertEqual(["inside"], steam.order_book_calls)
        self.assertEqual("c5.price_batch", events[0][0])
        self.assertEqual("steam.order_book", events[1][0])
        self.assertEqual([], c5.forbidden_calls)
        self.assertEqual([], steam.forbidden_calls)
        self.assertTrue(result["researchOnly"])
        self.assertFalse(result["canExecute"])

        payload = list_c5_research_results(
            self.settings,
            str(created["requestId"]),
            page=1,
            page_size=20,
            sort="catalog",
        )
        by_name = {item["marketHashName"]: item for item in payload["items"]}
        self.assertEqual("observed", by_name["inside"]["status"])
        self.assertEqual("c5_filtered_out", by_name["too-expensive"]["status"])
        self.assertEqual("c5_price_unavailable", by_name["missing-c5"]["status"])
        self.assertAlmostEqual(0.795, by_name["inside"]["expectedRoi"])
        self.assertAlmostEqual(7.95, by_name["inside"]["expectedProfit"])

        with closing(sqlite3.connect(self.settings.db_path)) as conn:
            for table in (
                "profit_trades",
                "inventory_pool",
                "inventory_assets",
                "pool_operations",
            ):
                marker = conn.execute(f"SELECT marker FROM {table}").fetchone()[0]
                self.assertEqual("untouched", marker)

    def test_steam_429_keeps_cursor_and_resume_retries_without_repeating_completed_rows(self) -> None:
        names = ["first", "rate-limited", "last"]
        created = self._create(names)
        events: list[tuple[str, object]] = []
        c5 = _FakeC5({name: 10.0 for name in names}, events)
        steam = _FakeSteam(
            {name: 1000 for name in names},
            events,
            failures={
                "rate-limited": SteamMarketError(
                    "Steam orderbook rate limited",
                    status_code=429,
                    retry_after="120",
                )
            },
        )
        service = _FakeMarketService(c5, steam)

        retry = run_c5_research_scan_chunk(
            self.settings,
            str(created["requestId"]),
            market_service=service,
        )
        self.assertEqual("retry", retry["status"])
        self.assertEqual(1, retry["cursor"])
        self.assertEqual(1, retry["processedCount"])
        self.assertIsNotNone(retry["nextAttemptAt"])
        self.assertEqual(["first", "rate-limited"], steam.order_book_calls)

        resumed = set_c5_research_scan_action(
            self.settings,
            str(created["requestId"]),
            "resume",
        )
        self.assertEqual("queued", resumed["status"])
        steam.failures.clear()
        completed = run_c5_research_scan_chunk(
            self.settings,
            str(created["requestId"]),
            market_service=service,
        )
        self.assertEqual("completed", completed["status"])
        self.assertEqual(3, completed["observedCount"])
        self.assertEqual(1, steam.order_book_calls.count("first"))

    def test_wrapped_c5_429_saves_cursor_without_calling_steam(self) -> None:
        created = self._create(["first", "second"])
        events: list[tuple[str, object]] = []
        cause = SteamMarketError(
            "upstream C5 HTTP response",
            status_code=429,
            retry_after="45",
        )
        wrapped = C5GameError("C5 request failed")
        wrapped.__cause__ = cause
        c5 = _FakeC5(
            {"first": 10.0, "second": 10.0},
            events,
            failure=wrapped,
        )
        steam = _FakeSteam({"first": 1000, "second": 1000}, events)

        retry = run_c5_research_scan_chunk(
            self.settings,
            str(created["requestId"]),
            market_service=_FakeMarketService(c5, steam),
        )

        self.assertEqual("retry", retry["status"])
        self.assertTrue(retry["safeToRetry"])
        self.assertEqual(0, retry["cursor"])
        self.assertEqual(0, retry["processedCount"])
        self.assertIsNotNone(retry["nextAttemptAt"])
        self.assertEqual([], steam.order_book_calls)
        persisted = get_c5_research_scan(self.settings, str(created["requestId"]))
        self.assertTrue(persisted["safeToRetry"])
        self.assertEqual(0, persisted["cursor"])

    def test_pause_resume_and_cancel_are_persistent_and_prevent_remote_calls(self) -> None:
        created = self._create(["alpha"])
        events: list[tuple[str, object]] = []
        c5 = _FakeC5({"alpha": 10.0}, events)
        steam = _FakeSteam({"alpha": 1000}, events)
        service = _FakeMarketService(c5, steam)
        request_id = str(created["requestId"])

        paused = set_c5_research_scan_action(self.settings, request_id, "pause")
        self.assertEqual("paused", paused["status"])
        still_paused = run_c5_research_scan_chunk(
            self.settings,
            request_id,
            market_service=service,
        )
        self.assertEqual("paused", still_paused["status"])
        self.assertEqual([], events)

        resumed = set_c5_research_scan_action(self.settings, request_id, "resume")
        self.assertEqual("queued", resumed["status"])
        cancelled = set_c5_research_scan_action(self.settings, request_id, "cancel")
        self.assertEqual("cancelled", cancelled["status"])
        still_cancelled = run_c5_research_scan_chunk(
            self.settings,
            request_id,
            market_service=service,
        )
        self.assertEqual("cancelled", still_cancelled["status"])
        self.assertEqual([], events)

    def test_result_pagination_and_roi_sort_are_server_side(self) -> None:
        names = ["low-roi", "high-roi", "middle-roi"]
        created = self._create(names)
        events: list[tuple[str, object]] = []
        c5 = _FakeC5({name: 10.0 for name in names}, events)
        steam = _FakeSteam(
            {"low-roi": 2000, "high-roi": 1000, "middle-roi": 1500},
            events,
        )
        run_c5_research_scan_chunk(
            self.settings,
            str(created["requestId"]),
            market_service=_FakeMarketService(c5, steam),
        )

        first_page = list_c5_research_results(
            self.settings,
            str(created["requestId"]),
            page=1,
            page_size=2,
            sort="roi_desc",
        )
        second_page = list_c5_research_results(
            self.settings,
            str(created["requestId"]),
            page=2,
            page_size=2,
            sort="roi_desc",
        )
        self.assertEqual(3, first_page["total"])
        self.assertEqual(2, first_page["pageSize"])
        self.assertEqual(["high-roi", "middle-roi"], [
            item["marketHashName"] for item in first_page["items"]
        ])
        self.assertEqual(["low-roi"], [
            item["marketHashName"] for item in second_page["items"]
        ])

    def test_default_market_service_is_built_with_relogin_disabled(self) -> None:
        created = self._create(["alpha"])
        events: list[tuple[str, object]] = []
        service = _FakeMarketService(
            _FakeC5({"alpha": 10.0}, events),
            _FakeSteam({"alpha": 1000}, events),
        )
        with patch(
            "cs2_assistant.services.profit_trade._build_profit_trade_market_service",
            return_value=service,
        ) as builder:
            run_c5_research_scan_chunk(
                self.settings,
                str(created["requestId"]),
            )
        self.assertFalse(builder.call_args.kwargs["allow_relogin"])


if __name__ == "__main__":
    unittest.main()
