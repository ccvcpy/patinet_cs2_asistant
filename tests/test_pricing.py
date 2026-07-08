from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.clients.steam_market import SteamMarketError
from cs2_assistant.services.pricing import fetch_listing_price


class FakeSteamMarketClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def order_book(self, *, app_id: int, market_hash_name: str) -> dict:
        return self.payload


class PricingTestCase(unittest.TestCase):
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
