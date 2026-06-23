from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import CatalogItem, StrategyConfig
from cs2_assistant.services.guadao_case_monitor import (
    CaseMonitorTarget,
    _parse_price_history_liquidity,
    build_case_ratio_report,
    collect_case_ratio_snapshots,
    list_case_monitor_targets,
)
from cs2_assistant.services.pricing import summarize_orderbook_prices


class FakeC5Client:
    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
        return {
            market_hash_names[0]: {
                "price": 60.0,
                "count": 25,
            }
        }


class FakeSteamClient:
    account_id = "fake-steam"

    def order_book(self, *, app_id: int, market_hash_name: str) -> dict:
        return {
            "success": 1,
            "rgCompactSellOrders": [
                [9900, 10],
                [10000, 10],
                [10100, 10],
            ],
        }


class GuadaoCaseMonitorCollectTestCase(unittest.TestCase):
    def test_orderbook_summary_parses_sell_wall_and_buy_order_minor_units(self) -> None:
        summary = summarize_orderbook_prices(
            {
                "success": 1,
                "data": {
                    "cSellOrders": 1000,
                    "cBuyOrders": 2000,
                    "rgCompactSellOrders": [89, 10, 90, 10],
                    "rgCompactBuyOrders": [87, 200],
                },
            },
            wall_min_count=20,
            price_offset=-0.01,
            min_price=0.01,
        )

        self.assertEqual(0.89, summary.seller_floor_price)
        self.assertEqual(0.90, summary.seller_wall_price)
        self.assertEqual(0.91, summary.seller_wall_list_price)
        self.assertEqual(0.87, summary.buyer_max_price)
        self.assertEqual(1000, summary.sell_order_count_total)
        self.assertEqual(2000, summary.buy_order_count_total)

    def test_price_history_liquidity_parses_steam_plus_zero_timezone(self) -> None:
        liquidity = _parse_price_history_liquidity(
            {
                "success": True,
                "prices": [
                    ["Jun 18 2026 14: +0", 1.795, "5,297"],
                    ["Jun 18 2026 15: +0", 1.816, "6035"],
                    ["Jun 18 2026 16: +0", 1.82, "5698"],
                ],
            },
            now=datetime(2026, 6, 18, 16, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(17030, liquidity["steamVolume24h"])
        self.assertEqual(17030, liquidity["steamVolume7d"])

    def test_collect_uses_case_wall_offset_and_listing_ratio_formula(self) -> None:
        settings = Settings(c5_api_key="c5-token")
        config = StrategyConfig(
            steam_net_factor=0.869,
            listing_wall_min_count=20,
            listing_price_offset=0.01,
            case_listing_price_offset=-0.01,
        )

        snapshots = collect_case_ratio_snapshots(
            settings=settings,
            config=config,
            targets=[CaseMonitorTarget("Kilowatt Case", "千瓦武器箱")],
            c5_client=FakeC5Client(),
            steam_clients=[FakeSteamClient()],
            observed_at="2026-06-17T00:00:00+00:00",
        )

        self.assertEqual(1, len(snapshots))
        snapshot = snapshots[0]
        self.assertEqual("ok", snapshot.status)
        self.assertEqual(100.01, snapshot.steam_list_price)
        assert snapshot.steam_after_tax_price is not None
        self.assertAlmostEqual(100.01 * 0.869, snapshot.steam_after_tax_price)
        assert snapshot.listing_ratio is not None
        self.assertAlmostEqual(60.0 / (100.01 * 0.869), snapshot.listing_ratio)
        self.assertEqual("steam_orderbook", snapshot.steam_price_source)

    def test_collect_parses_sub_one_cny_compact_orderbook_price(self) -> None:
        class CheapSteamClient:
            account_id = "fake-steam"

            def order_book(self, *, app_id: int, market_hash_name: str) -> dict:
                return {
                    "success": 1,
                    "data": {
                        "eCurrency": 23,
                        "rgCompactSellOrders": [89, 430, 90, 222],
                    },
                }

        class CheapC5Client:
            def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
                return {market_hash_names[0]: {"price": 0.54, "count": 25}}

        settings = Settings(c5_api_key="c5-token")
        config = StrategyConfig(
            steam_net_factor=0.869,
            listing_wall_min_count=20,
            listing_price_offset=0.01,
            case_listing_price_offset=-0.01,
        )

        snapshots = collect_case_ratio_snapshots(
            settings=settings,
            config=config,
            targets=[CaseMonitorTarget("Paris 2023 Challengers Sticker Capsule", "巴黎胶囊")],
            c5_client=CheapC5Client(),
            steam_clients=[CheapSteamClient()],
            observed_at="2026-06-17T00:00:00+00:00",
        )

        snapshot = snapshots[0]
        self.assertEqual(0.90, round(snapshot.steam_list_price or 0, 2))
        self.assertEqual(0.89, round(snapshot.steam_wall_price or 0, 2))
        self.assertAlmostEqual(0.54 / (0.90 * 0.869), snapshot.listing_ratio or 0)


class GuadaoCaseMonitorReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "assistant.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def _save_ratio(self, observed_at: str, ratio: float) -> None:
        self.db.save_guadao_case_ratio_snapshots(
            [
                {
                    "market_hash_name": "Kilowatt Case",
                    "observed_at": observed_at,
                    "name_cn": "千瓦武器箱",
                    "c5_sell_price": 60.0,
                    "steam_list_price": 100.0,
                    "steam_after_tax_price": 86.9,
                    "listing_ratio": ratio,
                    "c5_price_source": "c5_api_batch",
                    "steam_price_source": "steam_orderbook",
                    "status": "ok",
                    "raw_json": {},
                }
            ]
        )

    def test_report_summarizes_extremes_and_bucket_durations(self) -> None:
        self._save_ratio("2026-06-17T00:00:00+00:00", 0.68)
        self._save_ratio("2026-06-17T00:05:00+00:00", 0.70)
        self._save_ratio("2026-06-17T00:10:00+00:00", 0.70)

        report = build_case_ratio_report(
            self.db,
            start_utc="2026-06-17T00:00:00+00:00",
            end_utc="2026-06-17T00:15:00+00:00",
            expected_interval_minutes=5,
            bucket_size=0.01,
        )

        self.assertEqual(3, report["snapshotCount"])
        self.assertEqual(1, report["itemCount"])
        item = report["items"][0]
        self.assertEqual(0.68, item["minRatio"])
        self.assertEqual(5.0, item["minRatioDurationMinutes"])
        self.assertEqual(0.70, item["maxRatio"])
        self.assertEqual(10.0, item["maxRatioDurationMinutes"])
        self.assertAlmostEqual(0.6933, item["avgRatio"], places=4)
        self.assertEqual(0.70, item["recommendedMaxListingRatio"])
        self.assertEqual(0.70, item["aggressiveMaxListingRatio"])
        thresholds = {threshold["key"]: threshold for threshold in item["ratioThresholds"]}
        self.assertEqual(15.0, thresholds["conservative"]["durationMinutes"])
        self.assertEqual(15.0, thresholds["stable"]["durationMinutes"])
        buckets = {bucket["bucket"]: bucket for bucket in item["buckets"]}
        self.assertEqual(5.0, buckets["0.6800-0.6900"]["durationMinutes"])
        self.assertEqual(10.0, buckets["0.7000-0.7100"]["durationMinutes"])

    def test_report_corrects_legacy_sub_one_cny_minor_unit_rows(self) -> None:
        self.db.save_guadao_case_ratio_snapshots(
            [
                {
                    "market_hash_name": "Paris 2023 Challengers Sticker Capsule",
                    "observed_at": "2026-06-17T00:00:00+00:00",
                    "name_cn": "巴黎胶囊",
                    "c5_sell_price": 0.54,
                    "steam_list_price": 89.01,
                    "steam_wall_price": 89.0,
                    "steam_after_tax_price": 77.34969,
                    "listing_ratio": 0.00698,
                    "c5_price_source": "c5_api_batch",
                    "steam_price_source": "steam_orderbook",
                    "status": "ok",
                    "raw_json": {
                        "config": {
                            "steamNetFactor": 0.869,
                            "caseListingPriceOffset": -0.01,
                            "listingPriceOffset": 0.01,
                        }
                    },
                }
            ]
        )

        report = build_case_ratio_report(
            self.db,
            start_utc="2026-06-17T00:00:00+00:00",
            end_utc="2026-06-17T00:05:00+00:00",
            expected_interval_minutes=5,
            bucket_size=0.01,
            recommendation_crate_type="all",
        )

        item = report["items"][0]
        self.assertEqual(1, report["legacySteamMinorUnitCorrectedCount"])
        self.assertEqual(1, item["legacySteamMinorUnitCorrectedCount"])
        self.assertEqual(0.9, round(item["latestSteamListPrice"], 2))
        self.assertAlmostEqual(0.54 / (0.90 * 0.869), item["latestRatio"], places=4)

    def test_list_case_monitor_targets_uses_csgo_api_crates(self) -> None:
        self.db.upsert_items(
            [
                CatalogItem(
                    market_hash_name="Kilowatt Case",
                    name_cn="千瓦武器箱",
                    raw_json={
                        "market_hash_name": "Kilowatt Case",
                        "csgoApi": {"categories": ["crates"]},
                    },
                ),
                CatalogItem(
                    market_hash_name="AK-47 | Redline (Field-Tested)",
                    name_cn="AK-47 | Redline (Field-Tested)",
                    raw_json={"market_hash_name": "AK-47 | Redline (Field-Tested)"},
                ),
            ]
        )

        targets = list_case_monitor_targets(self.db)

        self.assertEqual(["Kilowatt Case"], [target.market_hash_name for target in targets])


if __name__ == "__main__":
    unittest.main()
