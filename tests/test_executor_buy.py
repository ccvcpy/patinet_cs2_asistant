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
        return {
            "orderAssetId": "asset-order-1",
            "orderId": "trade-order-1",
            "payStatus": 1,
        }


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
        self.assertEqual("confirmed", result.submission_outcome)
        self.assertIsNotNone(result.submitted_at)
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

    def test_ratio_no_longer_profitable_returns_reference_prices(self) -> None:
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
            guadao_max_listing_ratio=0.60,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertFalse(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual("ratio_no_longer_profitable", result.reason)
        self.assertAlmostEqual(3.83, float(result.steam_price_now), places=6)
        self.assertAlmostEqual(3.83, float(result.steam_reference_price), places=6)
        self.assertAlmostEqual(3.83 * 0.869 * 0.60, float(result.max_price), places=6)

    def test_execute_rebuy_uses_sold_price_for_max_price_without_steam_realtime_check(self) -> None:
        class KilowattC5Client(FakeC5Client):
            def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
                return {market_hash_names[0]: {"price": 1.30}}

        client = KilowattC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=object(),
            market_hash_name="Kilowatt Case",
            expected_price=1.30,
            expected_steam_list_price=2.38,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            guadao_max_listing_ratio=0.67,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertTrue(result.success)
        call = client.quick_buy_calls[0]
        self.assertAlmostEqual(2.38 * 0.869 * 0.67, float(call["max_price"]), places=6)
        self.assertEqual(2.38, result.steam_price_now)
        self.assertEqual(2.38, result.steam_reference_price)

    def test_execute_rebuy_can_force_current_c5_price_without_ratio_guard(self) -> None:
        class KilowattC5Client(FakeC5Client):
            def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
                return {market_hash_names[0]: {"price": 1.30}}

        client = KilowattC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.00,
            expected_steam_list_price=1.00,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            guadao_max_listing_ratio=None,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
            use_live_price_as_max=True,
        )

        self.assertTrue(result.success)
        call = client.quick_buy_calls[0]
        self.assertAlmostEqual(1.30 * 1.01, float(call["max_price"]), places=6)

    def test_execute_rebuy_uses_exact_replacement_price_cap_with_frozen_ratio_guard(self) -> None:
        class KilowattC5Client(FakeC5Client):
            def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
                return {market_hash_names[0]: {"price": 1.58}}

        client = KilowattC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.60,
            expected_steam_list_price=3.00,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            guadao_max_listing_ratio=0.62,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
            max_price_override=1.60,
        )

        self.assertTrue(result.success)
        call = client.quick_buy_calls[0]
        self.assertAlmostEqual(1.60, float(call["max_price"]), places=6)

    def test_replacement_price_cap_does_not_bypass_frozen_dynamic_ratio(self) -> None:
        class KilowattC5Client(FakeC5Client):
            def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
                return {market_hash_names[0]: {"price": 1.90}}

        client = KilowattC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=2.00,
            expected_steam_list_price=3.00,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            guadao_max_listing_ratio=0.62,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
            max_price_override=2.00,
        )

        self.assertFalse(result.success)
        self.assertEqual("ratio_no_longer_profitable", result.reason)
        self.assertAlmostEqual(2.00, float(result.max_price), places=6)
        self.assertEqual([], client.quick_buy_calls)

    def test_execute_rebuy_uses_exact_steam_net_amount_for_ratio_and_cap(self) -> None:
        class ExactNetC5Client(FakeC5Client):
            def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
                return {market_hash_names[0]: {"price": 1.49}}

        client = ExactNetC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.49,
            expected_steam_list_price=3.00,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            guadao_max_listing_ratio=0.75,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
            steam_net_amount_override=2.00,
        )

        self.assertTrue(result.success)
        self.assertAlmostEqual(1.49 / 2.00, float(result.listing_ratio_now), places=6)
        self.assertAlmostEqual(2.00 * 0.75, float(result.max_price), places=6)
        self.assertAlmostEqual(1.50, float(client.quick_buy_calls[0]["max_price"]), places=6)

    def test_exact_steam_net_amount_guard_can_reject_when_legacy_estimate_would_pass(self) -> None:
        class ExactNetC5Client(FakeC5Client):
            def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
                return {market_hash_names[0]: {"price": 1.50}}

        client = ExactNetC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.50,
            expected_steam_list_price=3.00,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            guadao_max_listing_ratio=0.70,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
            steam_net_amount_override=2.00,
        )

        self.assertFalse(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual("ratio_no_longer_profitable", result.reason)
        self.assertAlmostEqual(0.75, float(result.listing_ratio_now), places=6)
        self.assertAlmostEqual(1.40, float(result.max_price), places=6)
        self.assertEqual([], client.quick_buy_calls)

    def test_execute_rebuy_treats_c5_price_network_error_as_retryable(self) -> None:
        class NetworkErrorC5Client(FakeC5Client):
            def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, dict[str, float]]:
                raise C5GameError("C5 request failed: connection reset")

        client = NetworkErrorC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.23,
            expected_steam_list_price=2.12,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertFalse(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual("c5_network_error", result.reason)
        self.assertEqual([], client.quick_buy_calls)

    def test_execute_rebuy_treats_c5_buy_network_error_as_submission_unconfirmed(self) -> None:
        class NetworkErrorC5Client(FakeC5Client):
            def quick_buy(self, **kwargs: object) -> dict[str, object]:
                self.quick_buy_calls.append(dict(kwargs))
                raise C5GameError("C5 request failed: connection reset")

        client = NetworkErrorC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.23,
            expected_steam_list_price=2.12,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertFalse(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual("c5_submission_unconfirmed", result.reason)
        self.assertEqual("unconfirmed", result.submission_outcome)
        self.assertIsNotNone(result.out_trade_no)
        self.assertIsNotNone(result.submitted_at)
        self.assertIsInstance(result.payload, dict)
        self.assertIn("connection reset", str(result.payload.get("error")))
        self.assertEqual(1, len(client.quick_buy_calls))

    def test_execute_rebuy_does_not_confirm_http_200_without_order_credentials(self) -> None:
        class MissingOrderIdsC5Client(FakeC5Client):
            def quick_buy(self, **kwargs: object) -> dict[str, object]:
                self.quick_buy_calls.append(dict(kwargs))
                return {
                    "orderAssetId": None,
                    "orderId": None,
                    "payStatus": 2,
                    "actualPay": None,
                }

        client = MissingOrderIdsC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.23,
            expected_steam_list_price=2.12,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertFalse(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual("c5_submission_unconfirmed", result.reason)
        self.assertEqual("unconfirmed", result.submission_outcome)
        self.assertIsNotNone(result.out_trade_no)
        self.assertIsNotNone(result.submitted_at)
        self.assertEqual(2, result.payload["payStatus"])
        self.assertEqual(1, len(client.quick_buy_calls))

    def test_execute_rebuy_requires_both_order_ids(self) -> None:
        incomplete_payloads = (
            {"orderAssetId": "asset-order-1", "orderId": None, "payStatus": 1},
            {"orderAssetId": None, "orderId": "trade-order-1", "payStatus": 1},
        )

        for payload in incomplete_payloads:
            with self.subTest(payload=payload):
                class IncompleteCredentialC5Client(FakeC5Client):
                    def quick_buy(self, **kwargs: object) -> dict[str, object]:
                        self.quick_buy_calls.append(dict(kwargs))
                        return dict(payload)

                result = execute_rebuy(
                    client=IncompleteCredentialC5Client(),
                    steam_client=None,
                    market_hash_name="Kilowatt Case",
                    expected_price=1.23,
                    expected_steam_list_price=2.12,
                    app_id=730,
                    tolerance_pct=1.0,
                    dry_run=False,
                    trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
                )

                self.assertFalse(result.success)
                self.assertEqual("unconfirmed", result.submission_outcome)
                self.assertEqual("c5_submission_unconfirmed", result.reason)

    def test_execute_rebuy_recognizes_both_order_ids_even_when_pay_status_is_not_one(self) -> None:
        class PayStatusPendingC5Client(FakeC5Client):
            def quick_buy(self, **kwargs: object) -> dict[str, object]:
                self.quick_buy_calls.append(dict(kwargs))
                return {
                    "orderAssetId": "asset-order-pending",
                    "orderId": "trade-order-pending",
                    "payStatus": 2,
                }

        client = PayStatusPendingC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.23,
            expected_steam_list_price=2.12,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual("ok", result.reason)
        self.assertEqual("confirmed", result.submission_outcome)
        self.assertEqual(2, result.payload["payStatus"])
        self.assertEqual(1, len(client.quick_buy_calls))

    def test_execute_rebuy_treats_unexpected_post_submit_exception_as_unconfirmed(self) -> None:
        class UnexpectedFailureC5Client(FakeC5Client):
            def quick_buy(self, **kwargs: object) -> dict[str, object]:
                self.quick_buy_calls.append(dict(kwargs))
                raise RuntimeError("response parser crashed")

        client = UnexpectedFailureC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.23,
            expected_steam_list_price=2.12,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertFalse(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual("c5_submission_unconfirmed", result.reason)
        self.assertEqual("unconfirmed", result.submission_outcome)
        self.assertEqual("RuntimeError", result.payload["exceptionType"])
        self.assertEqual(1, len(client.quick_buy_calls))

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
        self.assertEqual("rejected", result.submission_outcome)
        self.assertIsInstance(result.payload, dict)
        self.assertEqual(1317, result.payload["errorCode"])

    def test_execute_rebuy_treats_stale_c5_listing_as_retryable_no_matching_listing(self) -> None:
        class StaleListingC5Client(FakeC5Client):
            def quick_buy(self, **kwargs: object) -> dict[str, object]:
                raise C5GameError(
                    '{"success": false, "data": null, "errorCode": 1014452, '
                    '"errorMsg": "当前饰品已经不是在售", "errorData": null, "errorCodeStr": null}'
                )

        client = StaleListingC5Client()
        result = execute_rebuy(
            client=client,
            steam_client=None,
            market_hash_name="Kilowatt Case",
            expected_price=1.23,
            expected_steam_list_price=4.00,
            app_id=730,
            tolerance_pct=1.0,
            dry_run=False,
            guadao_max_listing_ratio=0.70,
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        )

        self.assertFalse(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual("no_matching_listing", result.reason)
        self.assertEqual("rejected", result.submission_outcome)
        self.assertIsInstance(result.payload, dict)
        self.assertEqual(1014452, result.payload["errorCode"])


if __name__ == "__main__":
    unittest.main()
