from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.clients import C5GameError
from cs2_assistant.services.executor_buy import execute_rebuy


class FakeC5Client:
    def __init__(self) -> None:
        self.quick_buy_calls: list[dict[str, object]] = []

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
        return {
            market_hash_names[0]: {
                "price": 2.31,
            }
        }

    def quick_buy(self, **kwargs: object) -> dict[str, object]:
        self.quick_buy_calls.append(dict(kwargs))
        return {"ok": True}


class ExecuteRebuyTestCase(unittest.TestCase):
    def test_execute_rebuy_uses_list_strategy_quick_buy_defaults(self) -> None:
        client = FakeC5Client()

        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Revolution Case",
            expected_price=2.31,
            expected_steam_list_price=None,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(1, len(client.quick_buy_calls))

        call = client.quick_buy_calls[0]
        self.assertEqual(730, call["app_id"])
        self.assertEqual("Revolution Case", call["market_hash_name"])
        self.assertEqual(1, call["low_price"])
        self.assertEqual(
            "https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
            call["trade_url"],
        )
        self.assertAlmostEqual(2.3331, float(call["max_price"]), places=6)
        self.assertNotIn("delivery", call)

    def test_execute_rebuy_prefers_ratio_based_max_price_when_steam_price_available(self) -> None:
        client = FakeC5Client()

        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Revolution Case",
            expected_price=2.21,
            expected_steam_list_price=3.83,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            guadao_max_listing_ratio=0.73,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(1, len(client.quick_buy_calls))

        call = client.quick_buy_calls[0]
        self.assertAlmostEqual(3.83 * 0.869 * 0.73, float(call["max_price"]), places=6)
        self.assertGreater(float(call["max_price"]), 2.21 * 1.01)

    def test_execute_rebuy_treats_c5_1317_as_retryable_no_matching_listing(self) -> None:
        class NoMatchC5Client(FakeC5Client):
            def quick_buy(self, **kwargs: object) -> dict[str, object]:
                raise C5GameError(
                    '{"success": false, "data": null, "errorCode": 1317, '
                    '"errorMsg": "无满足条件的在售饰品", "errorData": null, "errorCodeStr": null}'
                )

        client = NoMatchC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Revolution Case",
            expected_price=2.31,
            expected_steam_list_price=3.83,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertFalse(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual("no_matching_listing", result.reason)
        self.assertIsInstance(result.payload, dict)
        self.assertEqual(1317, result.payload["errorCode"])


if __name__ == "__main__":
    unittest.main()
