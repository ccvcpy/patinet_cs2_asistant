from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.services.market import MarketService, calculate_t_yield_rate


class FakeSteamDTClient:
    def price_single(self, market_hash_name: str) -> list[dict]:
        return [
            {
                "platform": "C5",
                "sellPrice": 101.0,
                "biddingPrice": 88.0,
                "biddingCount": 7,
                "platformItemId": "from-steamdt",
            },
            {
                "platform": "Steam",
                "sellPrice": 200.0,
                "sellCount": 3,
            },
        ]

    def price_batch(self, market_hash_names: list[str]) -> list[dict]:
        return [
            {
                "marketHashName": market_hash_name,
                "dataList": [
                    {
                        "platform": "C5",
                        "sellPrice": 101.0,
                        "biddingPrice": 88.0,
                        "biddingCount": 7,
                        "platformItemId": "from-steamdt",
                    },
                    {
                        "platform": "Steam",
                        "sellPrice": 200.0,
                        "sellCount": 3,
                    },
                ],
            }
            for market_hash_name in market_hash_names
        ]


class FakeSteamDTMissingSteamClient:
    def price_single(self, market_hash_name: str) -> list[dict]:
        return [
            {
                "platform": "C5",
                "sellPrice": 101.0,
                "platformItemId": "from-steamdt",
            }
        ]

    def price_batch(self, market_hash_names: list[str]) -> list[dict]:
        return [
            {
                "marketHashName": market_hash_name,
                "dataList": [
                    {
                        "platform": "C5",
                        "sellPrice": 101.0,
                        "platformItemId": "from-steamdt",
                    }
                ],
            }
            for market_hash_name in market_hash_names
        ]


class FakeCSQAQClient:
    def price_by_market_hash_names(self, market_hash_names: list[str]) -> dict[str, dict]:
        return {
            market_hash_name: {
                "name": market_hash_name,
                "steamSellPrice": 222.0,
                "steamSellNum": 9,
            }
            for market_hash_name in market_hash_names
        }


class FakeC5Client:
    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        return {
            market_hash_name: {
                "price": 99.0,
                "count": 2,
                "itemId": "from-c5",
            }
            for market_hash_name in market_hash_names
        }

    def purchase_max_price(self, market_hash_name: str, app_id: int = 730) -> dict:
        return {
            "maxPrice": 95.5,
            "count": 11,
        }


class MarketServiceTestCase(unittest.TestCase):
    def test_c5_bid_price_comes_from_c5_official_api(self) -> None:
        service = MarketService(
            steamdt_client=FakeSteamDTClient(),
            c5_client=FakeC5Client(),
        )
        states = service.refresh_items(
            [
                {
                    "market_hash_name": "AK-47 | Asiimov (Field-Tested)",
                    "name_cn": "AK-47 | 二西莫夫 (久经沙场)",
                    "c5_item_id": "123",
                }
            ]
        )
        state = states[0]
        self.assertEqual(99.0, state.c5_sell_price)
        self.assertEqual(95.5, state.c5_bid_price)
        self.assertEqual(11, state.c5_bid_count)
        self.assertEqual(200.0, state.steam_sell_price)

    def test_t_yield_formula_uses_ratio_times_0869_minus_073(self) -> None:
        t_yield_rate = calculate_t_yield_rate(0.95)
        self.assertAlmostEqual(0.95 * 0.869 - 0.73, t_yield_rate)

    def test_csqaq_fills_missing_steam_price_when_steamdt_lacks_it(self) -> None:
        service = MarketService(
            steamdt_client=FakeSteamDTMissingSteamClient(),
            csqaq_client=FakeCSQAQClient(),
            c5_client=FakeC5Client(),
        )
        states = service.refresh_items(
            [
                {
                    "market_hash_name": "Rezan The Ready | Sabre",
                    "name_cn": "准备就绪的列赞 | 军刀",
                    "c5_item_id": "553486492",
                }
            ]
        )
        state = states[0]
        self.assertEqual(222.0, state.steam_sell_price)
        self.assertEqual(9, state.steam_sell_count)
        self.assertEqual("csqaq_batch", state.steam_price_source)

    def test_csqaq_is_preferred_when_both_sources_return_steam_price(self) -> None:
        service = MarketService(
            steamdt_client=FakeSteamDTClient(),
            csqaq_client=FakeCSQAQClient(),
            c5_client=FakeC5Client(),
        )
        states = service.refresh_items(
            [
                {
                    "market_hash_name": "Rezan The Ready | Sabre",
                    "name_cn": "准备就绪的列赞 | 军刀",
                    "c5_item_id": "553486492",
                }
            ]
        )
        state = states[0]
        self.assertEqual(222.0, state.steam_sell_price)
        self.assertEqual("csqaq_batch", state.steam_price_source)


if __name__ == "__main__":
    unittest.main()
