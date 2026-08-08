from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.services.market import MarketService, calculate_t_yield_rate
from cs2_assistant.services.pricing import choose_orderbook_price


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


class FakeSteamMarketClient:
    def __init__(self, *, fail: bool = False, currency: int | None = None, price: int = 22200) -> None:
        self.fail = fail
        self.currency = currency
        self.price = price

    def order_book(self, *, app_id: int, market_hash_name: str) -> dict:
        if self.fail:
            raise RuntimeError("steam account unavailable")
        payload = {
            "success": 1,
            "rgCompactSellOrders": [[self.price, 4]],
        }
        if self.currency is not None:
            payload["eCurrency"] = self.currency
        return payload


class RecordingBatchSteamMarketClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    def order_book(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        admission_timeout_seconds: float | None = None,
    ) -> dict:
        self.calls.append((market_hash_name, admission_timeout_seconds))
        return {
            "success": 1,
            "eCurrency": 23,
            "rgCompactSellOrders": [[100, 1], [200, 4]],
        }


class FakeC5PriceBatchFailureClient:
    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        raise RuntimeError("c5 ssl eof")

    def purchase_max_price(self, market_hash_name: str, app_id: int = 730) -> dict:
        raise RuntimeError("c5 purchase unavailable")


class MarketServiceTestCase(unittest.TestCase):
    def test_guadao_batch_uses_one_orderbook_snapshot_with_custom_wall_pricing(self) -> None:
        steam = RecordingBatchSteamMarketClient()
        service = MarketService(
            steam_market_client=steam,
            include_c5_purchase_prices=False,
            fallback_max_workers=1,
            steam_orderbook_admission_timeout_seconds=90.0,
            steam_orderbook_price_resolver=lambda _name, payload: choose_orderbook_price(
                payload,
                wall_min_count=5,
                price_offset=-0.01,
                min_price=0.01,
            ),
        )

        states = service.refresh_items(
            [
                {"market_hash_name": "Case A", "name_cn": "箱子 A"},
                {"market_hash_name": "Case B", "name_cn": "箱子 B"},
            ]
        )

        self.assertEqual(
            [("Case A", 90.0), ("Case B", 90.0)],
            steam.calls,
        )
        self.assertEqual([2.01, 2.01], [round(state.steam_sell_price or 0, 2) for state in states])
        self.assertTrue(
            all(state.raw_json.get("steam_orderbook_snapshot") for state in states)
        )

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

    def test_steam_orderbook_overrides_third_party_steam_price(self) -> None:
        service = MarketService(
            steamdt_client=FakeSteamDTClient(),
            csqaq_client=FakeCSQAQClient(),
            c5_client=FakeC5Client(),
            steam_market_client=FakeSteamMarketClient(),
        )

        states = service.refresh_items(
            [
                {
                    "market_hash_name": "Kilowatt Case",
                    "name_cn": "Kilowatt Case",
                    "c5_item_id": "case-1",
                }
            ]
        )

        state = states[0]
        self.assertEqual(222.0, state.steam_sell_price)
        self.assertEqual("steam_orderbook", state.steam_price_source)
        self.assertEqual(99.0, state.c5_sell_price)
        self.assertEqual("c5_batch", state.c5_price_source)

    def test_steam_orderbook_tries_all_configured_clients(self) -> None:
        service = MarketService(
            c5_client=FakeC5Client(),
            steam_market_clients=[
                FakeSteamMarketClient(fail=True),
                FakeSteamMarketClient(),
            ],
        )

        states = service.refresh_items(
            [
                {
                    "market_hash_name": "Kilowatt Case",
                    "name_cn": "Kilowatt Case",
                    "c5_item_id": "case-1",
                }
            ]
        )

        state = states[0]
        self.assertEqual(222.0, state.steam_sell_price)
        self.assertEqual("steam_orderbook", state.steam_price_source)

    def test_steam_orderbook_rejects_wrong_currency_and_tries_next_client(self) -> None:
        service = MarketService(
            c5_client=FakeC5Client(),
            steam_currency=23,
            steam_market_clients=[
                FakeSteamMarketClient(currency=1, price=3400),
                FakeSteamMarketClient(currency=23, price=179),
            ],
        )

        states = service.refresh_items(
            [
                {
                    "market_hash_name": "Kilowatt Case",
                    "name_cn": "Kilowatt Case",
                    "c5_item_id": "case-1",
                }
            ]
        )

        state = states[0]
        self.assertEqual(1.79, state.steam_sell_price)
        self.assertEqual("steam_orderbook", state.steam_price_source)
        self.assertIn("currency mismatch", " ".join(state.raw_json["steam_orderbook_retry_errors"]))

    def test_steam_orderbook_wrong_currency_does_not_override_third_party_price(self) -> None:
        service = MarketService(
            steamdt_client=FakeSteamDTClient(),
            c5_client=FakeC5Client(),
            steam_currency=23,
            steam_market_client=FakeSteamMarketClient(currency=1, price=3400),
        )

        states = service.refresh_items(
            [
                {
                    "market_hash_name": "Kilowatt Case",
                    "name_cn": "Kilowatt Case",
                    "c5_item_id": "case-1",
                }
            ]
        )

        state = states[0]
        self.assertEqual(200.0, state.steam_sell_price)
        self.assertEqual("steamdt", state.steam_price_source)
        self.assertIn("currency mismatch", state.raw_json["steam_orderbook_error"])

    def test_c5_price_batch_failure_does_not_abort_scan(self) -> None:
        service = MarketService(
            steamdt_client=FakeSteamDTClient(),
            c5_client=FakeC5PriceBatchFailureClient(),
            include_c5_purchase_prices=False,
        )

        states = service.refresh_items(
            [
                {
                    "market_hash_name": "Kilowatt Case",
                    "name_cn": "Kilowatt Case",
                    "c5_item_id": "case-1",
                }
            ]
        )

        state = states[0]
        self.assertEqual(200.0, state.steam_sell_price)
        self.assertEqual(101.0, state.c5_sell_price)
        self.assertEqual("steamdt", state.c5_price_source)
        self.assertIn("c5_batch_error", state.raw_json)


if __name__ == "__main__":
    unittest.main()
