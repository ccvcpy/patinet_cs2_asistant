from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.services.t_yield_scan import (
    INVENTORY_FILTER_COOLDOWN_ONLY,
    INVENTORY_FILTER_MIXED_ONLY,
    INVENTORY_FILTER_TRADABLE_ONLY,
    TYieldAccountRef,
    TYieldCandidate,
    TYieldScanReport,
    filter_candidates_by_inventory_filter,
    scan_t_yield,
    summarize_inventory_types,
)


class FakeMarketService:
    def __init__(self) -> None:
        self.received_market_hash_names: list[str] = []

    def refresh_items(self, items: list[dict[str, object]]) -> list[SimpleNamespace]:
        self.received_market_hash_names = [str(item["market_hash_name"]) for item in items]
        return [
            SimpleNamespace(
                market_hash_name=item["market_hash_name"],
                name_cn=item["name_cn"],
                c5_sell_price=item["reference_price"],
                c5_price_source="c5_batch",
                steam_sell_price=15.0,
                steam_price_source="steam_orderbook",
                raw_json={},
            )
            for item in items
        ]


class FakeC5PreflightMarketService:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def refresh_items(self, items: list[dict[str, object]]) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                market_hash_name=item["market_hash_name"],
                c5_sell_price=item["reference_price"],
                c5_sell_count=3,
                c5_item_id=item.get("c5_item_id"),
                c5_website=None,
                c5_price_source="c5_batch",
                raw_json={"c5_batch": {"price": item["reference_price"]}},
            )
            for item in items
        ]


class TYieldScanTestCase(unittest.TestCase):
    def test_bottom_rows_returns_lowest_yield_first(self) -> None:
        report = TYieldScanReport(
            generated_at="2026-04-01T00:00:00+00:00",
            inventory_source="live",
            inventory_cached_at=None,
            inventory_filter="all",
            accounts=[],
            inventory_type_total_count=3,
            inventory_type_count=3,
            candidates=[
                TYieldCandidate(
                    name="High",
                    market_hash_name="High",
                    inventory_count=1,
                    tradable_count=1,
                    inventory_status=INVENTORY_FILTER_TRADABLE_ONLY,
                    steam_accounts=[TYieldAccountRef(steam_id="1")],
                    c5_lowest_sell_price=10.0,
                    c5_price_source="inventory_price",
                    steam_lowest_sell_price=12.0,
                    steam_price_source="csqaq_batch",
                    ratio=0.95,
                    listing_ratio=0.95 / 0.869,
                    t_yield_rate=0.12,
                ),
                TYieldCandidate(
                    name="Low",
                    market_hash_name="Low",
                    inventory_count=1,
                    tradable_count=1,
                    inventory_status=INVENTORY_FILTER_TRADABLE_ONLY,
                    steam_accounts=[TYieldAccountRef(steam_id="1")],
                    c5_lowest_sell_price=10.0,
                    c5_price_source="inventory_price",
                    steam_lowest_sell_price=12.0,
                    steam_price_source="csqaq_batch",
                    ratio=0.82,
                    listing_ratio=0.82 / 0.869,
                    t_yield_rate=0.01,
                ),
                TYieldCandidate(
                    name="Mid",
                    market_hash_name="Mid",
                    inventory_count=1,
                    tradable_count=1,
                    inventory_status=INVENTORY_FILTER_TRADABLE_ONLY,
                    steam_accounts=[TYieldAccountRef(steam_id="1")],
                    c5_lowest_sell_price=10.0,
                    c5_price_source="inventory_price",
                    steam_lowest_sell_price=12.0,
                    steam_price_source="csqaq_batch",
                    ratio=0.88,
                    listing_ratio=0.88 / 0.869,
                    t_yield_rate=0.05,
                ),
            ],
            missing_steam_prices=[],
            missing_steam_price_path="data/missing.json",
        )

        rows = report.bottom_rows(2)
        self.assertEqual(["Low", "Mid"], [row["name"] for row in rows])

    def test_summarize_inventory_types_marks_inventory_status(self) -> None:
        summaries = summarize_inventory_types(
            [
                {
                    "marketHashName": "Tradable Item",
                    "name": "Tradable Item",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 12.0,
                },
                {
                    "marketHashName": "Cooldown Item",
                    "name": "Cooldown Item",
                    "steamId": "steam-a",
                    "ifTradable": False,
                    "price": 12.0,
                },
                {
                    "marketHashName": "Mixed Item",
                    "name": "Mixed Item",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 12.0,
                },
                {
                    "marketHashName": "Mixed Item",
                    "name": "Mixed Item",
                    "steamId": "steam-a",
                    "ifTradable": False,
                    "price": 12.0,
                },
            ]
        )

        summary_map = {row["market_hash_name"]: row for row in summaries}
        self.assertEqual(INVENTORY_FILTER_TRADABLE_ONLY, summary_map["Tradable Item"]["inventory_status"])
        self.assertEqual(INVENTORY_FILTER_COOLDOWN_ONLY, summary_map["Cooldown Item"]["inventory_status"])
        self.assertEqual(INVENTORY_FILTER_MIXED_ONLY, summary_map["Mixed Item"]["inventory_status"])

    def test_filter_candidates_by_inventory_filter_returns_matching_rows_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings = Settings(
                db_path=Path(temp_dir) / "assistant.db",
                c5_api_key="c5-token",
                csqaq_api_token="csqaq-token",
            )
            inventory_payload = {
                "source": "live",
                "cachedAt": "2026-04-01T00:00:00+00:00",
                "accounts": [{"steamId": "steam-a", "nickname": "115"}],
                "list": [
                    {
                        "marketHashName": "Tradable Item",
                        "name": "Tradable Item",
                        "steamId": "steam-a",
                        "ifTradable": True,
                        "price": 12.0,
                    },
                    {
                        "marketHashName": "Cooldown Item",
                        "name": "Cooldown Item",
                        "steamId": "steam-a",
                        "ifTradable": False,
                        "price": 12.0,
                    },
                    {
                        "marketHashName": "Mixed Item",
                        "name": "Mixed Item",
                        "steamId": "steam-a",
                        "ifTradable": True,
                        "price": 12.0,
                    },
                    {
                        "marketHashName": "Mixed Item",
                        "name": "Mixed Item",
                        "steamId": "steam-a",
                        "ifTradable": False,
                        "price": 12.0,
                    },
                ],
            }

            steam_market_service = FakeMarketService()
            with patch(
                "cs2_assistant.services.t_yield_scan.fetch_all_c5_inventories",
                return_value=inventory_payload,
            ), patch(
                "cs2_assistant.services.t_yield_scan.MarketService",
                FakeC5PreflightMarketService,
            ), patch(
                "cs2_assistant.services.t_yield_scan.build_market_service",
                return_value=steam_market_service,
            ):
                report = scan_t_yield(
                    settings,
                    min_price=10.0,
                    inventory_filter=INVENTORY_FILTER_MIXED_ONLY,
                )

        self.assertEqual(3, report.inventory_type_total_count)
        self.assertEqual(1, report.inventory_type_count)
        self.assertEqual(["Mixed Item"], [candidate.market_hash_name for candidate in report.candidates])
        candidate = report.candidates[0]
        self.assertAlmostEqual((12.0 / 15.0) * 0.99, candidate.ratio)
        self.assertAlmostEqual((12.0 / 15.0) * 0.99 - 0.73, candidate.t_yield_rate)
        self.assertAlmostEqual(12.0 / 15.0, candidate.listing_ratio)
        self.assertEqual(["Mixed Item"], steam_market_service.received_market_hash_names)
        self.assertEqual(
            ["Mixed Item"],
            [
                candidate.market_hash_name
                for candidate in filter_candidates_by_inventory_filter(
                    report.candidates,
                    INVENTORY_FILTER_MIXED_ONLY,
                )
            ],
        )

    def test_c5_min_price_filters_before_steam_market_refresh(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings = Settings(
                db_path=Path(temp_dir) / "assistant.db",
                c5_api_key="c5-token",
            )
            inventory_payload = {
                "source": "live",
                "accounts": [{"steamId": "steam-a", "nickname": "115"}],
                "list": [
                    {
                        "marketHashName": "High Value Item",
                        "name": "High Value Item",
                        "steamId": "steam-a",
                        "ifTradable": True,
                        "price": 12.0,
                    },
                    {
                        "marketHashName": "Low Value Item",
                        "name": "Low Value Item",
                        "steamId": "steam-a",
                        "ifTradable": True,
                        "price": 4.99,
                    },
                ],
            }
            steam_market_service = FakeMarketService()

            with patch(
                "cs2_assistant.services.t_yield_scan.fetch_all_c5_inventories",
                return_value=inventory_payload,
            ), patch(
                "cs2_assistant.services.t_yield_scan.MarketService",
                FakeC5PreflightMarketService,
            ), patch(
                "cs2_assistant.services.t_yield_scan.build_market_service",
                return_value=steam_market_service,
            ):
                report = scan_t_yield(settings, min_price=5.0)

        self.assertEqual(["High Value Item"], steam_market_service.received_market_hash_names)
        self.assertEqual(["High Value Item"], [candidate.market_hash_name for candidate in report.candidates])


if __name__ == "__main__":
    unittest.main()
