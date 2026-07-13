from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from cs2_assistant.clients.c5game import C5GameClient, C5GameError


class C5GameClientTests(unittest.TestCase):
    def test_request_telemetry_records_c5_activity_without_auth_or_body(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"success": true, "data": {"productId": "p-1"}}'
        events: list[dict[str, object]] = []
        client = C5GameClient(
            "secret-app-key",
            telemetry_callback=events.append,
            telemetry_context={"source": "profit_trade", "trade_no": "PT-C5"},
        )

        with patch("requests.request", return_value=response) as mocked:
            payload = client.quick_buy(
                app_id=730,
                item_id="item-1",
                max_price=12.34,
                trade_url=(
                    "https://steamcommunity.com/tradeoffer/new/"
                    "?partner=1&token=private-trade-token"
                ),
            )

        self.assertEqual("p-1", payload["productId"])
        request_events = [event for event in events if event.get("operation") == "quick_buy"]
        self.assertEqual(2, len(request_events))
        success = request_events[-1]
        self.assertEqual("profit_trade", success["source"])
        self.assertEqual("c5", success["provider"])
        self.assertEqual("POST", success["method"])
        self.assertEqual(200, success["status_code"])
        serialized = str(request_events)
        self.assertNotIn("secret-app-key", serialized)
        self.assertNotIn("private-trade-token", serialized)
        headers = mocked.call_args.kwargs["headers"]
        self.assertEqual("gzip, deflate, br", headers["Accept-Encoding"])

    def test_c5_telemetry_callback_failure_is_fail_open(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"success": true, "data": {"balance": 10}}'

        def broken_callback(event: dict[str, object]) -> None:
            raise RuntimeError("logging unavailable")

        client = C5GameClient(
            "secret-key",
            telemetry_callback=broken_callback,
            telemetry_context={"source": "profit_trade"},
        )
        with patch("requests.request", return_value=response):
            result = client.steam_info()

        self.assertEqual(10, result["balance"])

    def test_request_sends_compression_accept_encoding(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"success": true, "data": {}}'

        with patch("requests.request", return_value=response) as mocked:
            C5GameClient("secret-key").steam_info()

        headers = mocked.call_args.kwargs["headers"]
        self.assertEqual("gzip, deflate, br", headers["Accept-Encoding"])

    def test_request_error_redacts_app_key_from_message(self) -> None:
        response = requests.Response()
        response.status_code = 502
        response.reason = "Bad Gateway"
        response.url = "https://openapi.c5game.com/merchant/account/v1/steamInfo?app-key=secret-key"

        with patch("requests.request", return_value=response):
            client = C5GameClient("secret-key")
            with self.assertRaises(C5GameError) as raised:
                client.steam_info()

        message = str(raised.exception)
        self.assertNotIn("secret-key", message)
        self.assertIn("app-key=<redacted>", message)

    def test_market_products_search_posts_batch_listing_filters(self) -> None:
        client = C5GameClient("secret-key")
        with patch.object(client, "_request", return_value={"list": []}) as mocked:
            result = client.market_products_search(
                app_id=730,
                market_hash_name="Kilowatt Case",
                price_max=1.10,
                delivery=2,
                page_size=80,
            )

        self.assertEqual({"list": []}, result)
        mocked.assert_called_once_with(
            "POST",
            "/merchant/market/v2/products/search",
            json_body={
                "appId": 730,
                "marketHashName": "Kilowatt Case",
                "priceMax": 1.10,
                "delivery": 2,
                "acceptBargain": False,
                "pageSize": 80,
            },
        )

    def test_batch_buy_posts_all_products_in_one_request(self) -> None:
        product_list = [
            {"productId": "p-1", "buyPrice": 1.01, "outTradeNo": "trade-1"},
            {"productId": "p-2", "buyPrice": 1.02, "outTradeNo": "trade-2"},
        ]
        client = C5GameClient("secret-key")
        with patch.object(client, "_request", return_value={"successNum": 2}) as mocked:
            result = client.batch_buy(
                product_list=product_list,
                trade_url="https://steamcommunity.com/tradeoffer/new/?partner=1&token=test",
            )

        self.assertEqual({"successNum": 2}, result)
        mocked.assert_called_once_with(
            "POST",
            "/merchant/trade/v1/batch/buy",
            json_body={
                "productList": product_list,
                "tradeUrl": "https://steamcommunity.com/tradeoffer/new/?partner=1&token=test",
            },
        )

    def test_market_products_list_posts_explicit_page(self) -> None:
        client = C5GameClient("secret-key")
        with patch.object(client, "_request", return_value={"list": [], "hasMore": True}) as mocked:
            result = client.market_products_list(
                item_id="1097999762394030080",
                delivery=2,
                page_num=2,
                page_size=50,
            )

        self.assertEqual({"list": [], "hasMore": True}, result)
        mocked.assert_called_once_with(
            "POST",
            "/merchant/market/v2/products/list",
            json_body={
                "itemId": "1097999762394030080",
                "delivery": 2,
                "pageNum": 2,
                "pageSize": 50,
            },
        )


if __name__ == "__main__":
    unittest.main()
