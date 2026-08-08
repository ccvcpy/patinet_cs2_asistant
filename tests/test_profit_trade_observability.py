from __future__ import annotations

import json
import math
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database, PROFIT_TRADE_OBSERVABILITY_TABLES
from cs2_assistant.models import MarketState, StrategyConfig
import cs2_assistant.services.profit_trade as profit_trade_module
from cs2_assistant.services.profit_trade import (
    STEAM_BUY_LISTING_RETRY_ATTEMPTS,
    build_profit_trade_interruption_timeline_payload,
    build_profit_trade_interruptions_payload,
    build_profit_trade_roi_history_payload,
    build_profit_trade_roi_watch_payload,
    scan_profit_trade_opportunities,
    set_profit_trade_interruption_acknowledged,
)


MARKET_HASH_NAME = "USP-S | Tropical Breeze (Factory New)"


def profit_config(**overrides: object) -> StrategyConfig:
    values: dict[str, object] = {
        "profit_trade_enabled": True,
        "profit_trade_min_roi": 0.08,
        "profit_trade_min_item_value": 5.0,
        "profit_trade_require_c5_recent_sales": False,
        "profit_trade_require_c5_market_depth": False,
        "profit_trade_manual_review_roi": 0.20,
        "profit_trade_sticker_slab_status": "active",
        "profit_trade_sticker_status": "active",
        "profit_trade_balance_discount": 0.69,
    }
    values.update(overrides)
    return StrategyConfig(**values)


class FixedMarketService:
    def __init__(self, *, steam_price: float = 100.0, c5_price: float = 75.0) -> None:
        self.steam_price = steam_price
        self.c5_price = c5_price

    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        return [
            MarketState(
                market_hash_name=str(item["market_hash_name"]),
                name_cn=str(item.get("name_cn") or item["market_hash_name"]),
                steam_sell_price=self.steam_price,
                steam_price_source="steam_orderbook",
                c5_sell_price=self.c5_price,
                c5_price_source="c5_batch",
            )
            for item in items
        ]


class OrderbookSnapshotMarketService(FixedMarketService):
    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        states = super().refresh_items(items)
        for state in states:
            state.raw_json["steam_orderbook_snapshot"] = {
                "observedAt": "2026-07-22T12:00:00+00:00",
                "currencyId": 23,
                "currencyValid": True,
                "sellerFloorPrice": 100.0,
                "sellerFloorCount": 1,
                "buyerMaxPrice": 101.0,
                "buyerMaxCount": 3,
                "spreadAmount": -1.0,
                "spreadPct": -0.01,
                "crossed": True,
                "sellOrderCountTotal": 12,
                "buyOrderCountTotal": 8,
                "sellLevels": [{"price": 100.0, "count": 1}],
                "buyLevels": [{"price": 101.0, "count": 3}],
            }
        return states


class ListingProbeClient:
    account_id = "probe-account"
    steam_id64 = "probe-steam"

    def __init__(self) -> None:
        self.search_calls: list[dict[str, object]] = []

    def search_listings(self, **kwargs: object) -> dict[str, object]:
        self.search_calls.append(dict(kwargs))
        return {
            "listinginfo": {
                "inventory-probe-listing": {
                    "listingid": "inventory-probe-listing",
                    "market_hash_name": MARKET_HASH_NAME,
                    "converted_currencyid": 23,
                    "converted_price": 8696,
                    "converted_fee": 1304,
                    "converted_total": 10000,
                }
            }
        }


class InventoryProbeMarketService(OrderbookSnapshotMarketService):
    def __init__(self, *, steam_price: float = 100.0, c5_price: float = 75.0) -> None:
        super().__init__(steam_price=steam_price, c5_price=c5_price)
        self.probe_client = ListingProbeClient()
        self.steam_market_clients = [self.probe_client]


class ReferenceSnapshotMarketService(FixedMarketService):
    """Fixture-only market service: snapshots are already-fetched data."""

    def __init__(self, snapshots: dict[str, dict]) -> None:
        super().__init__(steam_price=100.0, c5_price=75.0)
        self.snapshots = snapshots

    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        states = super().refresh_items(items)
        for state in states:
            state.raw_json["steam_orderbook_snapshot"] = dict(
                self.snapshots[str(state.market_hash_name)]
            )
        return states


class FailingMarketService:
    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        raise RuntimeError("global market refresh failed")


class StableWallC5Client:
    def __init__(self, *, price: float = 75.0) -> None:
        self.price = float(price)

    def sale_search(self, **_: object) -> dict:
        return {"list": [], "total": 0}

    def price_statistics_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        return {
            name: {
                "marketHashName": name,
                "currentSellPrice": self.price,
                "onSaleCount": 3,
                "purchaseMaxPrice": self.price * 0.9,
                "purchaseCount": 10,
            }
            for name in market_hash_names
        }

class LowDepthC5Client:
    def price_statistics_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        return {
            name: {
                "marketHashName": name,
                "currentSellPrice": 75.0,
                "onSaleCount": 1,
                "purchaseMaxPrice": 70.0,
                "purchaseCount": 1,
            }
            for name in market_hash_names
        }

class PurchaseGapC5Client:
    def price_statistics_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        return {
            name: {
                "marketHashName": name,
                "currentSellPrice": 75.0,
                "onSaleCount": 18,
                "purchaseMaxPrice": 48.68,
                "purchaseCount": 17,
            }
            for name in market_hash_names
        }

class ProfitTradeObservabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.temp_dir.name) / "assistant.db",
            c5_api_key="test-c5-key",
        )
        self.default_c5_client = StableWallC5Client()
        self.c5_builder_patch = patch.object(
            profit_trade_module,
            "_build_profit_trade_c5_client",
            return_value=self.default_c5_client,
        )
        self.c5_builder_patch.start()
        self.inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-tropical",
                    "marketHashName": MARKET_HASH_NAME,
                    "name": "USP消音版 | 椰风花语（崭新出厂）",
                    "steamId": "76561199119018953",
                    "ifTradable": True,
                    "price": 75.0,
                    "token": "safe-test-token",
                    "styleToken": "safe-test-style-token",
                }
            ],
        }

    def tearDown(self) -> None:
        self.c5_builder_patch.stop()
        self.temp_dir.cleanup()

    def _open_db(self) -> Database:
        db = Database(self.settings.db_path)
        db.initialize()
        return db

    def test_positive_roi_below_minimum_is_watched_but_never_executable(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_min_roi=0.08),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            record=True,
            lock_asset=True,
        )

        self.assertEqual(0, report.opportunity_count)
        self.assertEqual([], report.created_trade_ids)
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual(1, watch["total"])
        self.assertGreater(watch["items"][0]["expectedRoi"], 0)
        self.assertEqual("observe_only", watch["items"][0]["executionStatus"])
        self.assertEqual("below_min_roi", watch["items"][0]["executionStatusCode"])

        db = self._open_db()
        try:
            self.assertEqual([], db.list_profit_trades(limit=10))
            self.assertIsNone(db.get_active_asset_reservation("asset-tropical"))
        finally:
            db.close()

    def test_orderbook_snapshot_is_persisted_in_watch_history_and_origin_trade(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_min_roi=0.05),
            inventory_payload=self.inventory_payload,
            market_service=OrderbookSnapshotMarketService(c5_price=75.0),
            record=True,
            lock_asset=True,
        )

        self.assertEqual(1, len(report.created_trade_ids))
        watch_item = build_profit_trade_roi_watch_payload(self.settings)["items"][0]
        history_item = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
        )["items"][0]
        for item in (watch_item, history_item):
            snapshot = item["steamOrderbook"]
            self.assertEqual(100.0, snapshot["sellerFloorPrice"])
            self.assertEqual(101.0, snapshot["buyerMaxPrice"])
            self.assertEqual(-1.0, snapshot["spreadAmount"])
            self.assertTrue(snapshot["crossed"])
            self.assertEqual([{"price": 100.0, "count": 1}], snapshot["sellLevels"])
            self.assertEqual([{"price": 101.0, "count": 3}], snapshot["buyLevels"])

        db = self._open_db()
        try:
            trade = db.get_profit_trade(report.created_trade_ids[0])
            note = json.loads(trade["note"])
            self.assertEqual(101.0, note["scanOrderbookSnapshot"]["buyerMaxPrice"])
        finally:
            db.close()

    def test_crossed_interruption_exposes_warning_and_complete_persistent_timeline(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_min_roi=0.05),
            inventory_payload=self.inventory_payload,
            market_service=OrderbookSnapshotMarketService(c5_price=75.0),
            record=True,
            lock_asset=True,
        )
        trade_id = report.created_trade_ids[0]
        db = self._open_db()
        try:
            row = db.get_profit_trade(trade_id)
            self.assertTrue(
                profit_trade_module._cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason="test terminal price protection",
                    source="test_price_guard",
                )
            )
        finally:
            db.close()

        interruption = build_profit_trade_interruptions_payload(self.settings)["items"][0]
        evidence = interruption["steamOrderbookEvidence"]
        self.assertTrue(evidence["crossedObserved"])
        self.assertEqual("scan", evidence["snapshots"][0]["stage"])
        self.assertEqual(100.0, evidence["snapshots"][0]["sellerFloorPrice"])
        self.assertEqual(101.0, evidence["snapshots"][0]["buyerMaxPrice"])

        timeline = build_profit_trade_interruption_timeline_payload(
            self.settings,
            trade_id,
        )
        event_types = [event["eventType"] for event in timeline["events"]]
        self.assertIn("orderbook_snapshot", event_types)
        terminal = timeline["events"][-1]
        self.assertEqual("cancelled", terminal["statusTo"])
        self.assertEqual("test terminal price protection", terminal["reason"])
        self.assertEqual("test_price_guard", terminal["context"]["cancelSource"])

    def test_crossed_inventory_scan_never_searches_listings(self) -> None:
        market_service = InventoryProbeMarketService(c5_price=75.0)
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_min_roi=0.05),
            inventory_payload=self.inventory_payload,
            market_service=market_service,
            record=False,
            lock_asset=False,
        )

        self.assertEqual([], market_service.probe_client.search_calls)
        watch_item = build_profit_trade_roi_watch_payload(self.settings)["items"][0]
        history_item = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
        )["items"][0]
        for item in (watch_item, history_item):
            self.assertTrue(item["steamOrderbook"]["crossed"])
            self.assertIsNone(item["crossedListingProbe"])

    def test_buy_order_reference_uses_same_snapshot_and_excludes_unsafe_statuses_from_total(self) -> None:
        names = {
            "valid": "P250 | Buy Reference Valid (Field-Tested)",
            "crossed": "P250 | Buy Reference Crossed (Field-Tested)",
            "missing": "P250 | Buy Reference Missing (Field-Tested)",
            "currency": "P250 | Buy Reference Currency (Field-Tested)",
        }
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": f"asset-reference-{key}",
                    "marketHashName": market_hash_name,
                    "name": market_hash_name,
                    "steamId": "76561199119018953",
                    "ifTradable": True,
                    "price": 75.0,
                    "token": "safe-test-token",
                    "styleToken": "safe-test-style-token",
                }
                for key, market_hash_name in names.items()
            ],
        }
        base_snapshot = {
            "observedAt": "2026-07-22T12:00:00+00:00",
            "sellerFloorPrice": 100.0,
            "sellerFloorCount": 1,
            "sellLevels": [{"price": 100.0, "count": 1}],
            "buyLevels": [{"price": 90.0, "count": 1}],
        }
        snapshots = {
            names["valid"]: {
                **base_snapshot,
                "currencyId": 23,
                "currencyValid": True,
                "buyerMaxPrice": 90.0,
                "buyerMaxCount": 1,
                "crossed": False,
            },
            names["crossed"]: {
                **base_snapshot,
                "currencyId": 23,
                "currencyValid": True,
                "buyerMaxPrice": 101.0,
                "buyerMaxCount": 2,
                "crossed": True,
            },
            names["missing"]: {
                **base_snapshot,
                "currencyId": 23,
                "currencyValid": True,
                "buyerMaxPrice": None,
                "buyerMaxCount": None,
                "buyLevels": [],
                "crossed": False,
            },
            names["currency"]: {
                **base_snapshot,
                "currencyId": 1,
                "currencyValid": False,
                "buyerMaxPrice": 90.0,
                "buyerMaxCount": 1,
                "crossed": False,
            },
        }

        config = profit_config(profit_trade_min_roi=0.08)
        scan_profit_trade_opportunities(
            self.settings,
            config,
            inventory_payload=inventory_payload,
            market_service=ReferenceSnapshotMarketService(snapshots),
        )

        payload = build_profit_trade_roi_watch_payload(self.settings, page_size=1)
        by_name = {
            item["marketHashName"]: item
            for item in build_profit_trade_roi_watch_payload(self.settings, page_size=20)["items"]
        }
        valid = by_name[names["valid"]]
        # A long-buy reference must use the proceeds of the price we would
        # actually list on C5: first apply the initial listing discount, floor
        # to a CNY cent, then apply C5's 1% net factor.
        first_c5_listing_price = math.floor(
            75.0 * (1.0 - config.profit_trade_initial_listing_discount_pct / 100.0) * 100.0
        ) / 100.0
        first_c5_expected_net = first_c5_listing_price * 0.99
        self.assertAlmostEqual(0.050025, valid["expectedRoi"])
        self.assertAlmostEqual(
            round((first_c5_expected_net / 90.0) - 0.69, 4),
            valid["buyOrderReferenceRoi"],
        )
        self.assertAlmostEqual(
            round(first_c5_expected_net - (90.0 * 0.69), 2),
            valid["buyOrderReferenceProfit"],
        )
        self.assertEqual("valid", valid["buyOrderReferenceStatus"])
        self.assertEqual(0.69, valid["roiBasis"])
        self.assertEqual("crossed_possible_stale", by_name[names["crossed"]]["buyOrderReferenceStatus"])
        self.assertEqual("missing_buy_book", by_name[names["missing"]]["buyOrderReferenceStatus"])
        self.assertEqual("currency_invalid", by_name[names["currency"]]["buyOrderReferenceStatus"])

        # page_size=1 proves headline amounts come from all active inventory
        # rows, not the current page.  One valid reference is the only one
        # eligible for the diagnostic total; crossed/missing/currency rows are
        # visible but deliberately excluded.
        summary = payload["summary"]
        self.assertEqual(4, summary["activeItemCount"])
        self.assertEqual(4, summary["tradableQuantity"])
        self.assertAlmostEqual(4 * 5.0025, summary["currentExpectedProfitTotal"])
        self.assertAlmostEqual(
            round(first_c5_expected_net - (90.0 * 0.69), 2),
            summary["buyOrderReferenceProfitTotal"],
        )
        self.assertEqual(2, summary["buyOrderReferenceCoveredItems"])
        self.assertEqual(1, summary["buyOrderReferenceEligibleItems"])

    def test_roi_history_stats_follow_time_filter_and_old_reference_fields_stay_null(self) -> None:
        db = self._open_db()
        try:
            observations = [
                (
                    "2026-04-01T00:00:00+00:00",
                    0.05,
                    0.69,
                    None,
                    None,
                    None,
                ),
                (
                    "2026-06-15T00:00:00+00:00",
                    0.10,
                    0.70,
                    0.13,
                    13.0,
                    "valid",
                ),
                (
                    "2026-07-20T00:00:00+00:00",
                    0.15,
                    0.73,
                    0.18,
                    18.0,
                    "valid",
                ),
            ]
            for index, (observed_at, expected_roi, basis, reference_roi, reference_profit, reference_status) in enumerate(observations):
                db.record_profit_trade_roi_scan(
                    [
                        {
                            "market_hash_name": MARKET_HASH_NAME,
                            "name_cn": "history fixture",
                            "steam_buy_price": 100.0,
                            "c5_listing_price": 75.0,
                            "c5_expected_net_price": 74.25,
                            "balance_discount": basis,
                            "expected_profit": 5.0,
                            "expected_roi": expected_roi,
                            "buy_order_reference_roi": reference_roi,
                            "buy_order_reference_profit": reference_profit,
                            "buy_order_reference_status": reference_status,
                            "inventory_count": 1,
                            "tradable_count": 1,
                            "risk_status": "passed",
                            "execution_status": "observe_only",
                            # The first row intentionally represents a legacy
                            # record without an orderbook/buy-order snapshot.
                            "raw": {} if index == 0 else {"steamOrderbook": {"currencyId": 23}},
                        }
                    ],
                    scan_id=f"PTSCAN-history-{index}",
                    observed_at=observed_at,
                )
        finally:
            db.close()

        all_time = build_profit_trade_roi_history_payload(self.settings, MARKET_HASH_NAME)
        self.assertEqual(3, all_time["stats"]["validObservationCount"])
        self.assertAlmostEqual(0.15, all_time["stats"]["highestRoi"])
        self.assertAlmostEqual(0.10, all_time["stats"]["averageRoi"])
        self.assertIsNone(all_time["stats"]["roiBasis"])
        self.assertEqual(0.69, all_time["stats"]["roiBasisMin"])
        self.assertEqual(0.73, all_time["stats"]["roiBasisMax"])
        self.assertEqual(3, all_time["trend"]["totalValidPoints"])
        self.assertFalse(all_time["trend"]["sampled"])
        self.assertEqual(
            [0.05, 0.10, 0.15],
            [point["expectedRoi"] for point in all_time["trend"]["points"]],
        )
        self.assertIsNone(all_time["trend"]["points"][0]["buyOrderReferenceRoi"])
        self.assertEqual(0.69, all_time["trend"]["points"][0]["roiBasis"])
        old = next(item for item in all_time["items"] if item["scanId"] == "PTSCAN-history-0")
        self.assertIsNone(old["buyOrderReferenceRoi"])
        self.assertIsNone(old["buyOrderReferenceProfit"])
        self.assertIsNone(old["buyOrderReferenceStatus"])

        ninety_day = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
            from_time="2026-06-01T00:00:00+00:00",
        )
        self.assertEqual(2, ninety_day["stats"]["validObservationCount"])
        self.assertAlmostEqual(0.125, ninety_day["stats"]["averageRoi"])
        self.assertEqual(0.70, ninety_day["stats"]["roiBasisMin"])
        self.assertEqual(0.73, ninety_day["stats"]["roiBasisMax"])
        self.assertEqual(
            [0.10, 0.15],
            [point["expectedRoi"] for point in ninety_day["trend"]["points"]],
        )

        seven_day = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
            from_time="2026-07-15T00:00:00+00:00",
            to_time="2026-07-23T23:59:59+00:00",
        )
        self.assertEqual(1, seven_day["stats"]["validObservationCount"])
        self.assertEqual(0.73, seven_day["stats"]["roiBasis"])
        self.assertAlmostEqual(0.15, seven_day["stats"]["highestRoi"])
        self.assertEqual(1, seven_day["trend"]["totalValidPoints"])
        self.assertEqual(1, len(seven_day["trend"]["points"]))

    def test_roi_history_trend_is_bounded_and_preserves_time_bucket_extrema(self) -> None:
        db = self._open_db()
        try:
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            rows = []
            for index in range(500):
                roi = index / 10_000
                if index == 123:
                    roi = -0.25
                elif index == 377:
                    roi = 0.75
                rows.append(
                    (
                        f"trend-{index}",
                        MARKET_HASH_NAME,
                        "observed",
                        (start + timedelta(minutes=index * 10)).isoformat(),
                        0.69,
                        roi,
                        roi + 0.01 if index % 3 else None,
                        "{}",
                    )
                )
            db.conn.executemany(
                """
                INSERT INTO profit_trade_roi_observations (
                    scan_id, market_hash_name, event_type, observed_at,
                    balance_discount, expected_roi, buy_order_reference_roi, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            db.conn.commit()
        finally:
            db.close()

        payload = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
            page_size=1,
        )
        trend = payload["trend"]
        self.assertEqual(500, trend["totalValidPoints"])
        self.assertTrue(trend["sampled"])
        self.assertLessEqual(len(trend["points"]), 240)
        self.assertEqual(rows[0][3], trend["points"][0]["observedAt"])
        self.assertEqual(rows[-1][3], trend["points"][-1]["observedAt"])
        self.assertIn(-0.25, [point["expectedRoi"] for point in trend["points"]])
        self.assertIn(0.75, [point["expectedRoi"] for point in trend["points"]])
        self.assertEqual(
            sorted(point["observedAt"] for point in trend["points"]),
            [point["observedAt"] for point in trend["points"]],
        )

    def test_old_roi_history_returns_unrecorded_orderbook_fields(self) -> None:
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_min_roi=0.08),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
        )

        item = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
        )["items"][0]
        self.assertEqual(100.0, item["steamOrderbook"]["sellerFloorPrice"])
        self.assertIsNone(item["steamOrderbook"]["buyerMaxPrice"])
        self.assertIsNone(item["steamOrderbook"]["observedAt"])
        self.assertEqual([], item["steamOrderbook"]["buyLevels"])

    def test_positive_roi_blocked_by_c5_depth_is_still_watched(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_min_roi=0.01,
                profit_trade_require_c5_market_depth=True,
                profit_trade_c5_min_on_sale_count=3,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            c5_client=LowDepthC5Client(),
            record=True,
        )

        self.assertEqual(0, report.opportunity_count)
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual("blocked", watch["items"][0]["executionStatus"])
        self.assertEqual("c5_risk_blocked", watch["items"][0]["executionStatusCode"])
        self.assertEqual("blocked_low_c5_listing_depth", watch["items"][0]["riskStatus"])

    def test_c5_purchase_gap_exposes_actual_ratio_and_configured_threshold(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_min_roi=0.01,
                profit_trade_require_c5_market_depth=True,
                profit_trade_c5_min_on_sale_count=3,
                profit_trade_c5_min_purchase_count=3,
                profit_trade_c5_min_purchase_sell_ratio=0.70,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            c5_client=PurchaseGapC5Client(),
            record=True,
        )

        self.assertEqual(0, report.opportunity_count)
        item = build_profit_trade_roi_watch_payload(self.settings)["items"][0]
        self.assertEqual("blocked_c5_purchase_price_gap", item["riskStatus"])
        self.assertAlmostEqual(48.68 / 75.0, item["c5PurchaseSellRatio"])
        self.assertEqual(0.70, item["c5MinPurchaseSellRatio"])
        history_item = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
        )["items"][0]
        self.assertEqual(75.0, history_item["c5CurrentSellPrice"])
        self.assertEqual(48.68, history_item["c5PurchaseMaxPrice"])
        self.assertAlmostEqual(48.68 / 75.0, history_item["c5PurchaseSellRatio"])

    def test_manual_review_market_evaluation_is_watched_and_not_auto_locked(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_min_roi=0.01,
                profit_trade_manual_review_roi=0.04,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            record=True,
            lock_asset=True,
        )

        self.assertEqual(1, report.opportunity_count)
        self.assertEqual([], report.locked_trade_ids)
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual("manual_review", watch["items"][0]["executionStatus"])
        db = self._open_db()
        try:
            trade = db.get_profit_trade(report.created_trade_ids[0])
            self.assertEqual("manual_required", trade["status"])
            self.assertIsNone(db.get_active_asset_reservation("asset-tropical"))
        finally:
            db.close()

    def test_executable_observation_links_to_live_trade_status_and_completion(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_min_roi=0.05),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            record=True,
            lock_asset=True,
        )

        self.assertEqual(1, report.opportunity_count)
        trade_id = report.created_trade_ids[0]
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual(trade_id, watch["items"][0]["latestTrade"]["tradeId"])
        self.assertEqual("locked", watch["items"][0]["latestTrade"]["status"])

        history = build_profit_trade_roi_history_payload(self.settings, MARKET_HASH_NAME)
        linked = history["items"][0]["relatedTrade"]
        self.assertEqual(trade_id, linked["tradeId"])
        self.assertEqual("locked", linked["status"])

        db = self._open_db()
        try:
            db.update_profit_trade(
                trade_id,
                status="steam_bought",
                step_key="steam_bought",
                step_index=3,
                steam_buy_price=100.0,
                note=json.dumps(
                    {
                        **json.loads(db.get_profit_trade(trade_id)["note"]),
                        "steamBuySucceededAt": "2026-07-14T12:16:49+00:00",
                    },
                    ensure_ascii=False,
                ),
            )
        finally:
            db.close()

        bought = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
        )["items"][0]["relatedTrade"]
        self.assertEqual("steam_bought", bought["status"])
        self.assertEqual("2026-07-14T12:16:49+00:00", bought["steamBoughtAt"])

        db = self._open_db()
        try:
            db.update_profit_trade(
                trade_id,
                status="completed",
                step_key="settled",
                step_index=6,
                c5_sold_net_price=80.0,
                realized_profit=11.0,
                realized_roi=0.11,
                completed_at="2026-07-14T14:19:38+00:00",
            )
        finally:
            db.close()

        completed = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
        )["items"][0]["relatedTrade"]
        self.assertEqual("completed", completed["status"])
        self.assertEqual(80.0, completed["c5SoldNetPrice"])
        self.assertEqual(11.0, completed["realizedProfit"])
        self.assertEqual(0.11, completed["realizedRoi"])

    def test_completed_scan_exits_stale_watch_and_preserves_history(self) -> None:
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_long_buy_enabled=False),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
        )
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_long_buy_enabled=False),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=60.0),
            c5_client=StableWallC5Client(price=60.0),
        )

        self.assertEqual(0, build_profit_trade_roi_watch_payload(self.settings)["total"])
        inactive = build_profit_trade_roi_watch_payload(self.settings, active=False)
        self.assertEqual(1, inactive["total"])
        self.assertEqual("exited", inactive["items"][0]["executionStatus"])
        self.assertIn("not positive", inactive["items"][0]["exitReason"])
        history = build_profit_trade_roi_history_payload(self.settings, MARKET_HASH_NAME)
        self.assertEqual(["exited", "entered"], [item["eventType"] for item in history["items"]])
        self.assertLess(history["items"][0]["expectedRoi"], 0)

    def test_non_positive_seller_roi_stays_visible_for_eligible_long_buy(
        self,
    ) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_long_buy_enabled=True),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=60.0),
            c5_client=StableWallC5Client(price=60.0),
        )

        self.assertEqual(0, report.opportunity_count)
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual(1, watch["total"])
        item = watch["items"][0]
        self.assertLess(item["expectedRoi"], 0)
        self.assertIsNone(item["longBuyOrder"])
        self.assertTrue(item["longBuyProposal"]["eligible"])
        self.assertEqual(
            "standard_no_competitor",
            item["longBuyProposal"]["decision"],
        )

    def test_global_scan_failure_does_not_clear_existing_watch(self) -> None:
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
        )

        with self.assertRaisesRegex(RuntimeError, "global market refresh failed"):
            scan_profit_trade_opportunities(
                self.settings,
                profit_config(),
                inventory_payload=self.inventory_payload,
                market_service=FailingMarketService(),
            )

        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual(1, watch["total"])
        history = build_profit_trade_roi_history_payload(self.settings, MARKET_HASH_NAME)
        self.assertEqual(1, history["total"])

    def test_protected_item_is_removed_from_current_watch_without_execution(self) -> None:
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
        )
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_protected_market_hash_names=[MARKET_HASH_NAME]),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            record=True,
        )

        self.assertEqual(0, report.evaluated_count)
        self.assertEqual(0, report.opportunity_count)
        self.assertEqual([], report.created_trade_ids)
        self.assertEqual(0, build_profit_trade_roi_watch_payload(self.settings)["total"])

    def test_state_transition_and_event_are_one_transaction(self) -> None:
        db = self._open_db()
        try:
            trade_id = db.add_profit_trade(
                trade_no="PT-observability-transaction",
                market_hash_name=MARKET_HASH_NAME,
            )
            db.conn.execute(
                """
                CREATE TRIGGER fail_profit_trade_transition
                BEFORE INSERT ON profit_trade_state_events
                WHEN NEW.event_type = 'transition'
                BEGIN
                    SELECT RAISE(ABORT, 'forced state event failure');
                END
                """
            )
            db.conn.commit()

            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced state event failure"):
                db.update_profit_trade(
                    trade_id,
                    status="locked",
                    step_key="asset_locked",
                    step_index=2,
                )

            trade = db.get_profit_trade(trade_id)
            self.assertEqual("candidate", trade["status"])
            self.assertEqual("discovered", trade["step_key"])
            self.assertEqual(1, len(db.list_profit_trade_state_events(trade_id)))
        finally:
            db.close()

    def test_historical_trade_without_events_gets_only_truthful_snapshot(self) -> None:
        db = self._open_db()
        try:
            trade_id = db.add_profit_trade(
                trade_no="PT-observability-history",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="HTTP 429 before purchase",
            )
            db.conn.execute("DELETE FROM profit_trade_state_events WHERE trade_id = ?", (trade_id,))
            db.conn.commit()
        finally:
            db.close()

        timeline = build_profit_trade_interruption_timeline_payload(self.settings, trade_id)
        self.assertEqual(1, len(timeline["events"]))
        event = timeline["events"][0]
        self.assertEqual("historical_snapshot", event["eventType"])
        self.assertTrue(event["isSnapshot"])
        self.assertEqual("cancelled", event["statusTo"])
        self.assertEqual("asset_locked", event["stepKeyTo"])
        self.assertIsNone(event["statusFrom"])

    def test_acknowledgement_filters_and_refuses_uncertain_buy_order(self) -> None:
        db = self._open_db()
        try:
            safe_trade_id = db.add_profit_trade(
                trade_no="PT-observability-safe-ack",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="search_listings HTTP 429",
            )
            unsafe_trade_id = db.add_profit_trade(
                trade_no="PT-observability-unsafe-ack",
                market_hash_name="Dreams & Nightmares Case",
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                note='{"steamBuyMethod":"createbuyorder","steamBuyOrderId":"buy-order-live"}',
            )
        finally:
            db.close()

        safe = set_profit_trade_interruption_acknowledged(
            self.settings,
            safe_trade_id,
            acknowledged=True,
            reason="user reviewed the 429 evidence",
        )
        self.assertTrue(safe["ok"])
        default_list = build_profit_trade_interruptions_payload(self.settings)
        self.assertEqual([unsafe_trade_id], [item["id"] for item in default_list["items"]])
        acknowledged_list = build_profit_trade_interruptions_payload(
            self.settings,
            acknowledged="only",
        )
        self.assertEqual([safe_trade_id], [item["id"] for item in acknowledged_list["items"]])

        unsafe = set_profit_trade_interruption_acknowledged(
            self.settings,
            unsafe_trade_id,
            acknowledged=True,
            reason="must not disappear silently",
        )
        self.assertFalse(unsafe["ok"])
        self.assertTrue(unsafe["conflict"])
        self.assertTrue(unsafe["requiresRemoteResolution"])
        still_visible = build_profit_trade_interruptions_payload(self.settings)
        self.assertIn(unsafe_trade_id, [item["id"] for item in still_visible["items"]])

        restored = set_profit_trade_interruption_acknowledged(
            self.settings,
            safe_trade_id,
            acknowledged=False,
            reason="restore to active problem list",
        )
        self.assertTrue(restored["ok"])
        visible = build_profit_trade_interruptions_payload(self.settings)
        self.assertIn(safe_trade_id, [item["id"] for item in visible["items"]])

    def test_interruption_search_supports_chinese_name_note_and_js_iso_time(self) -> None:
        db = self._open_db()
        try:
            trade_id = db.add_profit_trade(
                trade_no="PT-observability-chinese-search",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="Steam listings search failed",
                note=(
                    '{"name":"USP消音版 | 椰风花语（崭新出厂）",'
                    '"cancelSource":"profit_trade_search_listings",'
                    '"cancelReason":"Steam HTTP 429"}'
                ),
            )
            row = db.get_profit_trade(trade_id)
            boundary = str(row["completed_at"] or row["updated_at"])
        finally:
            db.close()

        js_boundary = boundary.replace("+00:00", "Z")
        payload = build_profit_trade_interruptions_payload(
            self.settings,
            keyword="椰风花语",
            from_time=js_boundary,
            to_time=js_boundary,
        )
        self.assertEqual(1, payload["total"])
        self.assertEqual(trade_id, payload["items"][0]["id"])
        self.assertEqual("profit_trade_search_listings", payload["items"][0]["cancelSource"])
        self.assertEqual("Steam HTTP 429", payload["items"][0]["cancelReason"])
        self.assertEqual(1, payload["summary"]["total"])

    def test_id103_style_history_projects_not_sent_only_from_strict_pre_buy_evidence(self) -> None:
        db = self._open_db()
        try:
            id103_style = db.add_profit_trade(
                trade_no="PT-20260711-85483d1c68",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                note=(
                    '{"cancelSource":"profit_trade_pre_buy_cancel",'
                    '"cancelReason":"automatic run cancelled locked trade before Steam buy after error: '
                    'Steam listings search failed: 429 Too Many Requests"}'
                ),
            )
            ambiguous = db.add_profit_trade(
                trade_no="PT-ambiguous-asset-locked-cancel",
                market_hash_name="AK-47 | Redline (Field-Tested)",
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                note='{"cancelSource":"user_cancelled","cancelReason":"user stopped the trade"}',
            )
            purchase_evidence = db.add_profit_trade(
                trade_no="PT-pre-buy-source-with-purchase-evidence",
                market_hash_name="Dreams & Nightmares Case",
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                note=(
                    '{"cancelSource":"profit_trade_pre_buy_cancel",'
                    '"cancelReason":"legacy inconsistent record",'
                    '"steamBuyRequestedAt":"2026-07-11T07:03:45+00:00"}'
                ),
            )
            listing_price_guard = db.add_profit_trade(
                trade_no="PT-listing-price-guard",
                market_hash_name="USP-S | Tropical Breeze (Factory New)",
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                steam_listing_id="507367553952201686",
                error=(
                    "Steam listing price moved too much above orderbook before buy: "
                    "17.17 > 15.87 * 1.0100"
                ),
                note=(
                    '{"cancelSource":"profit_trade_buy_listing_price_guard",'
                    '"cancelReason":"Steam listing price moved too much above orderbook '
                    'before buy: 17.17 > 15.87 * 1.0100"}'
                ),
            )
            explicit_not_sent_after_buying_transition = db.add_profit_trade(
                trade_no="PT-explicit-not-sent-after-buying-transition",
                market_hash_name="M4A4 | Neo-Noir (Field-Tested)",
                status="cancelled",
                step_key="steam_bought",
                step_index=3,
                steam_listing_id="orderbook_floor",
                note=(
                    '{"steamBuyMethod":"createbuyorder",'
                    '"steamBuyRequestedAt":"2026-07-31T00:08:16+00:00",'
                    '"cancelledBeforeSteamBuyAt":"2026-07-31T00:09:29+00:00",'
                    '"cancelSource":"profit_trade_buy_price_guard",'
                    '"cancelReason":"Steam buy price moved too much before buy"}'
                ),
            )
        finally:
            db.close()

        projected = build_profit_trade_interruption_timeline_payload(self.settings, id103_style)["trade"]
        self.assertIs(projected["purchaseRequestSent"], False)
        self.assertIs(projected["listingIdObtained"], False)
        self.assertIs(projected["note"]["purchaseRequestSent"], False)
        self.assertIs(projected["note"]["listingIdObtained"], False)

        unknown = build_profit_trade_interruption_timeline_payload(self.settings, ambiguous)["trade"]
        self.assertIsNone(unknown["purchaseRequestSent"])
        self.assertIsNone(unknown["listingIdObtained"])
        self.assertNotIn("purchaseRequestSent", unknown["note"])

        sent = build_profit_trade_interruption_timeline_payload(self.settings, purchase_evidence)["trade"]
        self.assertIs(sent["purchaseRequestSent"], True)

        guarded = build_profit_trade_interruption_timeline_payload(
            self.settings,
            listing_price_guard,
        )["trade"]
        self.assertIs(guarded["listingIdObtained"], True)
        self.assertIs(guarded["purchaseRequestSent"], False)
        self.assertEqual("507367553952201686", guarded["steamListingId"])

        explicit_not_sent = build_profit_trade_interruption_timeline_payload(
            self.settings,
            explicit_not_sent_after_buying_transition,
        )["trade"]
        self.assertIs(explicit_not_sent["purchaseRequestSent"], False)
        self.assertIs(explicit_not_sent["listingIdObtained"], False)

        db = self._open_db()
        try:
            original = db.get_profit_trade(id103_style)
            original_note = json.loads(str(original["note"] or "{}"))
        finally:
            db.close()
        self.assertNotIn("purchaseRequestSent", original_note)
        self.assertNotIn("listingIdObtained", original_note)

    def test_interruption_query_rejects_non_interruption_status_and_bad_ack_filter(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid.*status"):
            build_profit_trade_interruptions_payload(
                self.settings,
                statuses=("completed",),
            )
        with self.assertRaisesRegex(ValueError, "acknowledged"):
            build_profit_trade_interruptions_payload(
                self.settings,
                acknowledged="anything",
            )

    def test_existing_database_is_backed_up_before_additive_observability_upgrade(self) -> None:
        db = self._open_db()
        try:
            trade_id = db.add_profit_trade(
                trade_no="PT-observability-before-upgrade",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
            )
            db.conn.executescript(
                """
                DROP TABLE profit_trade_acknowledgements;
                DROP TABLE profit_trade_state_events;
                DROP TABLE profit_trade_roi_observations;
                DROP TABLE profit_trade_roi_watch;
                DROP TABLE profit_trade_runtime_state;
                DROP TABLE profit_trade_long_buy_fills;
                DROP TABLE profit_trade_long_buy_events;
                DROP TABLE profit_trade_long_buy_orders;
                """
            )
            db.conn.commit()
        finally:
            db.close()

        upgraded = Database(self.settings.db_path)
        try:
            upgraded.initialize()
            upgraded.initialize()
            tables = {
                str(row[0])
                for row in upgraded.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            upgraded.close()

        self.assertTrue(
            {
                "profit_trade_roi_watch",
                "profit_trade_roi_observations",
                "profit_trade_state_events",
                "profit_trade_acknowledgements",
            }.issubset(tables)
        )
        backups = list(
            self.settings.db_path.parent.glob(
                f"{self.settings.db_path.stem}.pre-profit-trade-observability-*{self.settings.db_path.suffix}"
            )
        )
        self.assertEqual(1, len(backups))
        backup = sqlite3.connect(backups[0])
        try:
            row = backup.execute("SELECT trade_no FROM profit_trades WHERE id = ?", (trade_id,)).fetchone()
            backup_tables = {
                str(value[0])
                for value in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            backup.close()
        self.assertEqual("PT-observability-before-upgrade", row[0])
        self.assertFalse(PROFIT_TRADE_OBSERVABILITY_TABLES.intersection(backup_tables))

    def test_existing_roi_watch_tables_get_reference_columns_additively(self) -> None:
        # Simulate the immediately previous observability schema.  The new
        # fields must be additive so a user's existing ROI history remains
        # readable after a backend restart.
        legacy = sqlite3.connect(self.settings.db_path)
        try:
            legacy.executescript(
                """
                CREATE TABLE profit_trade_roi_watch (
                    market_hash_name TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 1,
                    expected_roi REAL,
                    last_observed_at TEXT NOT NULL
                );
                CREATE TABLE profit_trade_roi_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL,
                    market_hash_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    expected_roi REAL
                );
                """
            )
            legacy.commit()
        finally:
            legacy.close()

        upgraded = Database(self.settings.db_path)
        try:
            upgraded.initialize()
            for table in ("profit_trade_roi_watch", "profit_trade_roi_observations"):
                columns = {
                    str(row[1])
                    for row in upgraded.conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                self.assertTrue(
                    {
                        "buy_order_reference_roi",
                        "buy_order_reference_profit",
                        "buy_order_reference_status",
                    }.issubset(columns)
                )
        finally:
            upgraded.close()

    def test_original_three_purchase_attempt_policy_is_unchanged(self) -> None:
        self.assertEqual(3, STEAM_BUY_LISTING_RETRY_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
