from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.clients.steam_market import SteamMarketError
from cs2_assistant.services.pricing import build_orderbook_snapshot, fetch_listing_price


class FakeSteamMarketClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def order_book(self, *, app_id: int, market_hash_name: str) -> dict:
        return self.payload


class PricingTestCase(unittest.TestCase):
    def test_orderbook_snapshot_normalizes_flat_and_nested_levels_to_same_depth(self) -> None:
        flat = build_orderbook_snapshot(
            {
                "data": {
                    "eCurrency": 23,
                    "cSellOrders": 12,
                    "cBuyOrders": 9,
                    "rgCompactSellOrders": [100, 1, 101, 2, 102, 3, 103, 4, 104, 5, 105, 6],
                    "rgCompactBuyOrders": [99, 7, 98, 8, 97, 9, 96, 10, 95, 11, 94, 12],
                }
            },
            observed_at="2026-07-22T12:00:00+00:00",
            depth=5,
        )
        nested = build_orderbook_snapshot(
            {
                "eCurrency": 23,
                "rgCompactSellOrders": [[100, 1], [101, 2], [102, 3], [103, 4], [104, 5], [105, 6]],
                "rgCompactBuyOrders": [[99, 7], [98, 8], [97, 9], [96, 10], [95, 11], [94, 12]],
            },
            observed_at="2026-07-22T12:00:00+00:00",
            depth=5,
        )

        self.assertEqual(5, len(flat["sellLevels"]))
        self.assertEqual(5, len(flat["buyLevels"]))
        self.assertEqual(flat["sellLevels"], nested["sellLevels"])
        self.assertEqual(flat["buyLevels"], nested["buyLevels"])
        self.assertEqual(1.0, flat["sellerFloorPrice"])
        self.assertEqual(0.99, flat["buyerMaxPrice"])
        self.assertAlmostEqual(0.01, flat["spreadAmount"])
        self.assertFalse(flat["crossed"])
        self.assertTrue(flat["currencyValid"])

    def test_orderbook_snapshot_marks_crossed_and_wrong_currency_without_extra_request(self) -> None:
        snapshot = build_orderbook_snapshot(
            {
                "eCurrency": 1,
                "rgCompactSellOrders": [100, 1],
                "rgCompactBuyOrders": [101, 2],
            },
            expected_currency=23,
        )

        self.assertFalse(snapshot["currencyValid"])
        self.assertTrue(snapshot["crossed"])
        self.assertAlmostEqual(-0.01, snapshot["spreadAmount"])

    def test_fetch_listing_price_rejects_wrong_orderbook_currency(self) -> None:
        client = FakeSteamMarketClient(
            {
                "success": 1,
                "eCurrency": 1,
                "rgCompactSellOrders": [[94, 10]],
            }
        )

        decision = fetch_listing_price(
            client,  # type: ignore[arg-type]
            app_id=730,
            market_hash_name="Antwerp 2022 Legends Sticker Capsule",
            wall_min_count=1,
            price_offset=0,
            currency=23,
            force_refresh=True,
        )

        self.assertIsNone(decision)

    def test_fetch_listing_price_raises_on_wrong_currency_in_debug_mode(self) -> None:
        client = FakeSteamMarketClient(
            {
                "success": 1,
                "data": {
                    "eCurrency": 1,
                    "rgCompactSellOrders": [[94, 10]],
                },
            }
        )

        with self.assertRaises(SteamMarketError):
            fetch_listing_price(
                client,  # type: ignore[arg-type]
                app_id=730,
                market_hash_name="Antwerp 2022 Legends Sticker Capsule",
                wall_min_count=1,
                price_offset=0,
                currency=23,
                force_refresh=True,
                debug=True,
            )

    def test_fetch_listing_price_accepts_matching_orderbook_currency(self) -> None:
        client = FakeSteamMarketClient(
            {
                "success": 1,
                "eCurrency": 23,
                "rgCompactSellOrders": [[486, 10]],
            }
        )

        decision = fetch_listing_price(
            client,  # type: ignore[arg-type]
            app_id=730,
            market_hash_name="Antwerp 2022 Legends Sticker Capsule",
            wall_min_count=1,
            price_offset=0,
            currency=23,
            force_refresh=True,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(4.86, decision.list_price)


if __name__ == "__main__":
    unittest.main()
