from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from cs2_assistant.accounts import Account
from cs2_assistant.clients.steam_market import SteamMarketClient


class _FakeResponse:
    def __init__(self, payload: dict[str, object], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.url = "https://steamcommunity.com/market/mylistings?start=0&count=1&norender=1"
            raise requests.HTTPError(response=response)


class SteamMarketClientTests(unittest.TestCase):
    def test_sell_item_uses_inventory_referer_and_browser_headers(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
        )

        captured: dict[str, object] = {}

        def fake_request(
            method: str,
            path: str,
            *,
            params: dict[str, object] | None = None,
            data: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
            _allow_retry: bool = True,
        ) -> _FakeResponse:
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            captured["data"] = data
            captured["headers"] = headers
            captured["_allow_retry"] = _allow_retry
            return _FakeResponse({"success": 1, "listingid": "listing-1"})

        client._request = fake_request  # type: ignore[method-assign]

        payload = client.sell_item(
            app_id=730,
            context_id="2",
            asset_id="asset-1",
            price=6.52,
            quantity=1,
            steam_net_factor=0.869,
        )

        self.assertEqual(payload["listingid"], "listing-1")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/market/sellitem/")
        self.assertEqual(
            captured["data"],
            {
                "sessionid": "session-1",
                "appid": 730,
                "contextid": "2",
                "assetid": "asset-1",
                "amount": 1,
                "price": 567,
            },
        )
        headers = captured["headers"]
        assert isinstance(headers, dict)
        self.assertEqual(
            headers["Referer"],
            "https://steamcommunity.com/profiles/76561198000000000/inventory",
        )
        self.assertEqual(headers["Origin"], "https://steamcommunity.com")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertIn("application/json", headers["Accept"])

    def test_request_retries_with_fresh_cookies_after_auth_failure(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-old; steamLoginSecure=76561198000000000%7C%7Ctoken-old",
            steam_id64="76561198000000000",
        )
        client.account_id = "main"
        client._account_store = object()

        responses = iter(
            [
                _FakeResponse({"success": False}, status_code=401),
                _FakeResponse({"success": True}, status_code=200),
            ]
        )
        request_headers: list[dict[str, str] | None] = []

        def fake_request(
            method: str,
            url: str,
            *,
            params: dict[str, object] | None = None,
            data: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
            timeout: int | None = None,
        ) -> _FakeResponse:
            request_headers.append(headers)
            return next(responses)

        def fake_relogin(*args: object, **kwargs: object):
            return (
                True,
                "auto_ok",
                Account(
                    id="main",
                    name="main-account",
                    steam_id64="76561198000000000",
                    cookies="sessionid=session-new; steamLoginSecure=76561198000000000%7C%7Ctoken-new",
                ),
            )

        client._session.request = fake_request  # type: ignore[method-assign]

        with patch("cs2_assistant.clients.steam_market.try_steam_auto_relogin", side_effect=fake_relogin) as relogin_mock:
            response = client._request("GET", "/market/mylistings", params={"start": 0, "count": 1, "norender": 1})

        self.assertEqual({"success": True}, response.json())
        self.assertEqual(1, relogin_mock.call_count)
        self.assertEqual("session-new", client.sessionid)
        self.assertEqual(2, len(request_headers))

    def test_find_sale_receipt_reads_received_amount_from_market_history(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
        )

        captured: dict[str, object] = {}

        def fake_request(
            method: str,
            path: str,
            *,
            params: dict[str, object] | None = None,
            data: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
            _allow_retry: bool = True,
        ) -> _FakeResponse:
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            return _FakeResponse(
                {
                    "success": True,
                    "total_count": 1,
                    "events": [
                        {
                            "listingid": "listing-1",
                            "purchaseid": "purchase-1",
                            "event_type": 3,
                            "time_event": 1780247918,
                        }
                    ],
                    "purchases": {
                        "listing-1_purchase-1": {
                            "received_amount": 1217,
                            "received_currencyid": "2023",
                        }
                    },
                }
            )

        client._request = fake_request  # type: ignore[method-assign]

        receipt = client.find_sale_receipt("listing-1")

        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["path"], "/market/myhistory/render/")
        self.assertEqual({"query": "", "start": 0, "count": 100, "norender": 1}, captured["params"])
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual("purchase-1", receipt["purchaseId"])
        self.assertEqual(12.17, receipt["receivedAmount"])
        self.assertEqual("2023", receipt["receivedCurrencyId"])


if __name__ == "__main__":
    unittest.main()
