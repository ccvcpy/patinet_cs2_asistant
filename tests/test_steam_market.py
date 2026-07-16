from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

import requests

from cs2_assistant.accounts import Account
from cs2_assistant.clients.steam_market import SteamListing, SteamMarketClient, SteamMarketError


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        status_code: int = 200,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = dict(headers or {})

    def json(self) -> dict[str, object]:
        return self._payload

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.url = "https://steamcommunity.com/market/mylistings?start=0&count=1&norender=1"
            raise requests.HTTPError(response=response)


class SteamMarketClientTests(unittest.TestCase):
    def test_safety_terminal_reads_are_scheduled_as_p0(self) -> None:
        scheduled: list[dict[str, object]] = []

        class CaptureScheduler:
            def call(self, **kwargs: object) -> _FakeResponse:
                scheduled.append(dict(kwargs))
                return kwargs["callback"]()  # type: ignore[operator]

        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            request_source="profit_trade",
        )

        def fake_request(**kwargs: object) -> _FakeResponse:
            url = str(kwargs.get("url") or "")
            if url.rstrip("/").endswith("/market"):
                return _FakeResponse(
                    {},
                    text=(
                        'g_rgWalletInfo = {"wallet_balance":0,'
                        '"wallet_delayed_balance":0,"wallet_currency":23};'
                    ),
                )
            if "myhistory" in url:
                return _FakeResponse({"success": True, "events": []})
            return _FakeResponse({"success": True, "buy_orders": []})

        client._session.request = fake_request  # type: ignore[method-assign]
        with patch(
            "cs2_assistant.services.steam_request_scheduler.get_shared_steam_scheduler",
            return_value=CaptureScheduler(),
        ):
            client.wallet_balance(safety_terminal=True)
            client.my_listings(safety_terminal=True)
            client.market_history(safety_terminal=True)

        self.assertEqual([0, 0, 0], [int(row["priority"]) for row in scheduled])

    def test_real_action_methods_forward_last_moment_execution_guard(self) -> None:
        scheduled: list[dict[str, object]] = []

        class CaptureScheduler:
            def call(self, **kwargs: object) -> _FakeResponse:
                scheduled.append(dict(kwargs))
                return kwargs["callback"]()  # type: ignore[operator]

        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            request_source="profit_trade",
        )
        client._session.request = lambda **kwargs: _FakeResponse(  # type: ignore[method-assign]
            {"success": 1}
        )
        client._session.post = lambda *args, **kwargs: _FakeResponse(  # type: ignore[method-assign]
            {"success": 1}
        )
        guard = lambda: True
        with patch(
            "cs2_assistant.services.steam_request_scheduler.get_shared_steam_scheduler",
            return_value=CaptureScheduler(),
        ):
            client.sell_item(
                app_id=730,
                context_id="2",
                asset_id="asset-1",
                price=10.0,
                execution_guard=guard,
            )
            client.buy_listing(
                listing_id="listing-1",
                app_id=730,
                subtotal=100,
                fee=10,
                total=110,
                execution_guard=guard,
            )
            client.create_buy_order(
                app_id=730,
                market_hash_name="Kilowatt Case",
                price_total=110,
                execution_guard=guard,
            )

        self.assertEqual(3, len(scheduled))
        self.assertTrue(all(row.get("execution_guard") is guard for row in scheduled))

    def test_search_listings_forwards_only_explicit_bounded_retry_to_scheduler(self) -> None:
        scheduled: list[dict[str, object]] = []

        class CaptureScheduler:
            def call(self, **kwargs: object) -> _FakeResponse:
                scheduled.append(dict(kwargs))
                return kwargs["callback"]()  # type: ignore[operator]

        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            request_source="profit_trade",
        )
        client._session.request = lambda **kwargs: _FakeResponse(  # type: ignore[method-assign]
            {"success": True, "listinginfo": {}}
        )
        with patch(
            "cs2_assistant.services.steam_request_scheduler.get_shared_steam_scheduler",
            return_value=CaptureScheduler(),
        ):
            client.search_listings(
                app_id=730,
                market_hash_name="USP-S | Tropical Breeze (Factory New)",
                bounded_retry=True,
            )

        self.assertEqual(1, len(scheduled))
        self.assertIs(scheduled[0]["bounded_retry"], True)
        self.assertIs(scheduled[0]["quiet_before"], True)

    def test_request_telemetry_captures_429_without_changing_relogin_or_retry(self) -> None:
        events: list[dict[str, object]] = []
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            telemetry_callback=events.append,
            telemetry_context={"source": "profit_trade", "trade_no": "PT-429"},
        )
        client._try_account_relogin = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("429 must not trigger relogin")
        )
        calls = 0

        def fake_request(**kwargs: object) -> _FakeResponse:
            nonlocal calls
            calls += 1
            return _FakeResponse(
                {"success": False},
                status_code=429,
                headers={"Retry-After": "7"},
            )

        client._session.request = fake_request  # type: ignore[method-assign]

        with self.assertRaises(SteamMarketError) as caught:
            client.search_listings(
                app_id=730,
                market_hash_name="USP-S | Tropical Breeze (Factory New)",
            )

        self.assertEqual(1, calls)
        self.assertEqual(429, caught.exception.status_code)
        self.assertEqual("7", caught.exception.retry_after)
        request_events = [event for event in events if event.get("operation") == "search_listings"]
        self.assertEqual(2, len(request_events))
        self.assertEqual("DEBUG", request_events[0]["level"])
        failure = request_events[1]
        self.assertEqual("ERROR", failure["level"])
        self.assertEqual(429, failure["status_code"])
        self.assertEqual("7", failure["retry_after"])
        self.assertEqual("PT-429", failure["trade_no"])
        self.assertEqual("profit_trade", failure["source"])
        self.assertEqual("POST", failure["method"])
        self.assertNotIn("session-1", str(request_events))

    def test_telemetry_callback_failure_does_not_change_steam_request(self) -> None:
        def broken_callback(event: dict[str, object]) -> None:
            raise RuntimeError("logging is unavailable")

        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            telemetry_callback=broken_callback,
            telemetry_context={"source": "profit_trade"},
        )
        client._session.request = lambda **kwargs: _FakeResponse(  # type: ignore[method-assign]
            {"success": True, "rgCompactSellOrders": []}
        )

        payload = client.order_book(app_id=730, market_hash_name="Kilowatt Case")

        self.assertTrue(payload["success"])

    def test_order_book_passes_market_name_to_scheduler_metadata(self) -> None:
        scheduled: list[dict[str, object]] = []

        class CaptureScheduler:
            def call(self, **kwargs: object) -> _FakeResponse:
                scheduled.append(dict(kwargs))
                return kwargs["callback"]()  # type: ignore[operator]

        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            request_source="guadao",
        )
        client._session.request = lambda **kwargs: _FakeResponse(  # type: ignore[method-assign]
            {"success": True, "rgCompactSellOrders": []}
        )

        with patch(
            "cs2_assistant.services.steam_request_scheduler.get_shared_steam_scheduler",
            return_value=CaptureScheduler(),
        ):
            client.order_book(app_id=730, market_hash_name="Kilowatt Case")

        self.assertEqual(1, len(scheduled))
        metadata = scheduled[0]["metadata"]
        self.assertIsInstance(metadata, dict)
        self.assertEqual("Kilowatt Case", metadata["marketHashName"])  # type: ignore[index]

    def test_get_transport_retry_count_remains_three_with_telemetry(self) -> None:
        events: list[dict[str, object]] = []
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            telemetry_callback=events.append,
            telemetry_context={"source": "profit_trade"},
        )
        outcomes: list[object] = [
            requests.Timeout("first timeout"),
            requests.ConnectionError("second connection failure"),
            _FakeResponse({"success": True}),
        ]

        def fake_request(**kwargs: object) -> _FakeResponse:
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            assert isinstance(outcome, _FakeResponse)
            return outcome

        client._session.request = fake_request  # type: ignore[method-assign]
        with patch("cs2_assistant.clients.steam_market.time.sleep") as sleep_mock:
            response = client._request("GET", "/market/mylistings")

        self.assertEqual(200, response.status_code)
        self.assertEqual([], outcomes)
        self.assertEqual(2, sleep_mock.call_count)
        starts = [
            event
            for event in events
            if event.get("operation") == "my_listings"
            and (event.get("safe_context") or {}).get("phase") == "start"  # type: ignore[union-attr]
        ]
        self.assertEqual([1, 2, 3], [event["attempt"] for event in starts])

    def test_confirm_all_refuses_unscoped_confirmation(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
        )

        with self.assertRaises(SteamMarketError):
            client.confirm_all()

    def test_confirm_listing_assets_only_allows_matching_asset_confirmation(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            identity_secret=base64.b64encode(b"1" * 20).decode("ascii"),
            device_id="device-1",
        )
        client.list_confirmation_pending_listings = lambda: [  # type: ignore[method-assign]
            SteamListing("listing-good", "asset-good", "Kilowatt Case", None, None),
            SteamListing("listing-other", "asset-other", "AK-47 | Redline", None, None),
        ]
        client.fetch_confirmations = lambda: [  # type: ignore[method-assign]
            {"id": "conf-good", "nonce": "nonce-good", "creator_id": "listing-good"},
            {"id": "conf-other", "nonce": "nonce-other", "creator_id": "listing-other"},
            {"id": "conf-trade", "nonce": "nonce-trade", "creator_id": "trade-offer-1"},
        ]
        captured: dict[str, object] = {}

        def fake_post(
            url: str,
            *,
            params: dict[str, object],
            files: list[tuple[str, tuple[None, str]]],
            timeout: int,
        ) -> _FakeResponse:
            captured["url"] = url
            captured["params"] = params
            captured["files"] = files
            return _FakeResponse({"success": True})

        client._session.post = fake_post  # type: ignore[method-assign]

        confirmed = client.confirm_listing_assets(asset_ids=["asset-good"])

        self.assertEqual(1, confirmed)
        self.assertEqual("https://steamcommunity.com/mobileconf/multiajaxop", captured["url"])
        params = captured["params"]
        assert isinstance(params, dict)
        self.assertEqual("allow", params["op"])
        self.assertEqual(
            [("cid[]", (None, "conf-good")), ("ck[]", (None, "nonce-good"))],
            captured["files"],
        )

    def test_create_buy_order_confirms_exact_purchase_confirmation_and_reposts(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            identity_secret=base64.b64encode(b"1" * 20).decode("ascii"),
            device_id="device-1",
        )
        client.fetch_confirmations = lambda: [  # type: ignore[method-assign]
            {"id": "conf-other", "nonce": "nonce-other", "creator_id": "other-confirmation"},
            {"id": "conf-buy", "nonce": "nonce-buy", "creator_id": "buy-confirmation-1"},
        ]
        allowed: list[dict[str, object]] = []

        def fake_allow(confirmations: object) -> int:
            selected = list(confirmations)  # type: ignore[arg-type]
            allowed.extend(selected)
            return len(selected)

        client._allow_confirmations = fake_allow  # type: ignore[method-assign]
        posts: list[dict[str, object]] = []

        def fake_post(
            url: str,
            *,
            data: dict[str, object],
            headers: dict[str, str],
            timeout: int,
            allow_redirects: bool,
        ) -> _FakeResponse:
            posts.append({"url": url, "data": dict(data), "headers": headers})
            if len(posts) == 1:
                return _FakeResponse(
                    {
                        "need_confirmation": True,
                        "confirmation": {"confirmation_id": "buy-confirmation-1"},
                        "success": 22,
                    },
                    status_code=406,
                )
            return _FakeResponse({"success": 1, "buy_orderid": "buy-order-1"})

        client._session.post = fake_post  # type: ignore[method-assign]

        payload = client.create_buy_order(
            app_id=730,
            market_hash_name="Sticker | Patsi | Antwerp 2022",
            price_total=21,
            quantity=1,
        )

        self.assertEqual({"success": 1, "buy_orderid": "buy-order-1"}, payload)
        self.assertEqual(2, len(posts))
        self.assertEqual("0", posts[0]["data"]["confirmation"])  # type: ignore[index]
        self.assertEqual("buy-confirmation-1", posts[1]["data"]["confirmation"])  # type: ignore[index]
        self.assertEqual([{"id": "conf-buy", "nonce": "nonce-buy", "creator_id": "buy-confirmation-1"}], allowed)
        self.assertEqual(21, posts[0]["data"]["price_total"])  # type: ignore[index]

    def test_create_buy_order_can_return_uncertain_payload_after_confirmation(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            identity_secret=base64.b64encode(b"1" * 20).decode("ascii"),
            device_id="device-1",
        )
        client.fetch_confirmations = lambda: [  # type: ignore[method-assign]
            {"id": "conf-buy", "nonce": "nonce-buy", "creator_id": "buy-confirmation-1"},
        ]
        client._allow_confirmations = lambda confirmations: len(list(confirmations))  # type: ignore[method-assign]
        posts: list[dict[str, object]] = []

        def fake_post(
            url: str,
            *,
            data: dict[str, object],
            headers: dict[str, str],
            timeout: int,
            allow_redirects: bool,
        ) -> _FakeResponse:
            posts.append({"url": url, "data": dict(data), "headers": headers})
            if len(posts) == 1:
                return _FakeResponse(
                    {
                        "need_confirmation": True,
                        "confirmation": {"confirmation_id": "buy-confirmation-1"},
                        "success": 22,
                    },
                    status_code=406,
                )
            return _FakeResponse(
                {"success": 0, "message": "active order already exists"},
                status_code=406,
            )

        client._session.post = fake_post  # type: ignore[method-assign]

        payload = client.create_buy_order(
            app_id=730,
            market_hash_name="SSG 08 | Turbo Peek (Field-Tested)",
            price_total=16481,
            return_uncertain_after_confirmation=True,
        )

        self.assertEqual(2, len(posts))
        self.assertEqual(406, payload["_steam_http_status"])
        self.assertTrue(payload["_outcome_uncertain_after_confirmation"])
        self.assertEqual("active order already exists", payload["message"])

    def test_cancel_buy_order_posts_order_id(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
        )
        captured: dict[str, object] = {}

        def fake_post(
            url: str,
            *,
            data: dict[str, object],
            headers: dict[str, str],
            timeout: int,
            allow_redirects: bool,
        ) -> _FakeResponse:
            captured["url"] = url
            captured["data"] = dict(data)
            captured["headers"] = headers
            captured["allow_redirects"] = allow_redirects
            return _FakeResponse({"success": 1})

        client._session.post = fake_post  # type: ignore[method-assign]

        payload = client.cancel_buy_order(buy_order_id="buy-order-1")

        self.assertEqual({"success": 1}, payload)
        self.assertEqual("https://steamcommunity.com/market/cancelbuyorder/", captured["url"])
        self.assertEqual("session-1", captured["data"]["sessionid"])  # type: ignore[index]
        self.assertEqual("buy-order-1", captured["data"]["buy_orderid"])  # type: ignore[index]
        self.assertFalse(captured["allow_redirects"])
    def test_search_listings_uses_new_market_route_action_and_normalizes_listing_ids(self) -> None:
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
            data: dict[str, object] | str | None = None,
            headers: dict[str, str] | None = None,
            _allow_retry: bool = True,
            _scheduler_metadata: dict[str, object] | None = None,
        ) -> _FakeResponse:
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            captured["data"] = data
            captured["headers"] = headers
            captured["scheduler_metadata"] = _scheduler_metadata
            return _FakeResponse(
                {
                    "more": True,
                    "start": 0,
                    "total_count": 2,
                    "listings": [
                        {
                            "listingid": "listing-1",
                            "unPrice": 893,
                            "unFee": 133,
                            "eCurrency": 23,
                            "strSubtotal": "CNY 10.26",
                            "description": {
                                "market_hash_name": "Glock-18 | Candy Apple (Factory New)",
                            },
                            "asset": {"amount": 1},
                        },
                        {
                            "listingid": "listing-other",
                            "unPrice": 1,
                            "unFee": 1,
                            "eCurrency": 23,
                            "description": {
                                "market_hash_name": "Glock-18 | Candy Apple (Field-Tested)",
                            },
                        },
                    ],
                }
            )

        client._request = fake_request  # type: ignore[method-assign]

        payload = client.search_listings(
            app_id=730,
            market_hash_name="Glock-18 | Candy Apple (Factory New)",
            start=0,
            count=10,
            currency=23,
            country="CN",
            language="schinese",
        )

        self.assertEqual("POST", captured["method"])
        self.assertEqual(
            "/market/listings/730/Glock-18%20%7C%20Candy%20Apple%20%28Factory%20New%29",
            captured["path"],
        )
        self.assertEqual({"currency": 23, "language": "schinese", "country": "CN"}, captured["params"])
        self.assertEqual(
            {"marketHashName": "Glock-18 | Candy Apple (Factory New)"},
            captured["scheduler_metadata"],
        )
        headers = captured["headers"]
        assert isinstance(headers, dict)
        self.assertEqual("routeAction", headers["X-Valve-Request-Type"])
        self.assertEqual("4OPT6VBA:Search", headers["X-Valve-Action-Type"])
        body = captured["data"]
        self.assertIsInstance(body, str)
        self.assertIn('"disableGrouping":true', body)
        listinginfo = payload["listinginfo"]
        self.assertEqual(["listing-1"], list(listinginfo.keys()))
        self.assertEqual(893, listinginfo["listing-1"]["converted_price"])
        self.assertEqual(133, listinginfo["listing-1"]["converted_fee"])
        self.assertEqual(1026, listinginfo["listing-1"]["converted_total"])
        self.assertEqual(23, listinginfo["listing-1"]["converted_currencyid"])

    def test_buy_listing_uses_listing_price_fields_and_browser_headers(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
        )
        captured: dict[str, object] = {}

        def fake_post(
            url: str,
            *,
            files: list[tuple[str, tuple[None, str]]] | None = None,
            headers: dict[str, str] | None = None,
            timeout: int | None = None,
            allow_redirects: bool = False,
        ) -> _FakeResponse:
            captured["url"] = url
            captured["files"] = files
            captured["headers"] = headers
            return _FakeResponse({"success": 1, "wallet_info": {"success": 1}})

        client._session.post = fake_post  # type: ignore[method-assign]

        payload = client.buy_listing(
            listing_id="listing-1",
            app_id=730,
            subtotal=893,
            fee=133,
            total=1026,
            currency=23,
            country="CN",
        )

        self.assertEqual({"success": 1, "wallet_info": {"success": 1}}, payload)
        self.assertEqual("https://steamcommunity.com/market/buylisting/listing-1", captured["url"])
        self.assertEqual(
            [
                ("sessionid", (None, "session-1")),
                ("currency", (None, "23")),
                ("subtotal", (None, "893")),
                ("fee", (None, "133")),
                ("total", (None, "1026")),
                ("tradefee_tax", (None, "0")),
                ("quantity", (None, "1")),
                ("first_name", (None, "")),
                ("last_name", (None, "")),
                ("billing_address", (None, "")),
                ("billing_address_two", (None, "")),
                ("billing_country", (None, "CN")),
                ("billing_city", (None, "")),
                ("billing_state", (None, "")),
                ("billing_postal_code", (None, "")),
                ("confirmation", (None, "0")),
                ("save_my_address", (None, "0")),
            ],
            captured["files"],
        )
        headers = captured["headers"]
        assert isinstance(headers, dict)
        self.assertEqual("https://steamcommunity.com", headers["Origin"])
        self.assertEqual("XMLHttpRequest", headers["X-Requested-With"])
        self.assertIn("/market/listings/730", headers["Referer"])

    def test_buy_listing_confirms_exact_confirmation_and_reposts(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
            identity_secret=base64.b64encode(b"1" * 20).decode("ascii"),
            device_id="device-1",
        )
        client.fetch_confirmations = lambda: [  # type: ignore[method-assign]
            {"id": "conf-other", "nonce": "nonce-other", "creator_id": "other-confirmation"},
            {"id": "conf-buy", "nonce": "nonce-buy", "creator_id": "buylisting-confirmation-1"},
        ]
        allowed: list[dict[str, object]] = []

        def fake_allow(confirmations: object) -> int:
            selected = list(confirmations)  # type: ignore[arg-type]
            allowed.extend(selected)
            return len(selected)

        client._allow_confirmations = fake_allow  # type: ignore[method-assign]
        posts: list[dict[str, object]] = []

        def fake_post(
            url: str,
            *,
            files: list[tuple[str, tuple[None, str]]],
            headers: dict[str, str],
            timeout: int,
            allow_redirects: bool,
        ) -> _FakeResponse:
            posts.append({"url": url, "files": list(files), "headers": headers})
            if len(posts) == 1:
                return _FakeResponse(
                    {
                        "need_confirmation": True,
                        "confirmation": {"confirmation_id": "buylisting-confirmation-1"},
                        "success": 22,
                    },
                    status_code=406,
                )
            return _FakeResponse({"success": 1, "wallet_info": {"success": 1}})

        client._session.post = fake_post  # type: ignore[method-assign]

        payload = client.buy_listing(
            listing_id="listing-1",
            app_id=730,
            subtotal=893,
            fee=133,
            total=1026,
            currency=23,
            country="CN",
            market_hash_name="Glock-18 | Candy Apple (Factory New)",
        )

        self.assertEqual({"success": 1, "wallet_info": {"success": 1}}, payload)
        self.assertEqual(2, len(posts))
        self.assertIn(("confirmation", (None, "0")), posts[0]["files"])  # type: ignore[operator]
        self.assertIn(
            ("confirmation", (None, "buylisting-confirmation-1")),
            posts[1]["files"],  # type: ignore[operator]
        )
        self.assertEqual(
            [{"id": "conf-buy", "nonce": "nonce-buy", "creator_id": "buylisting-confirmation-1"}],
            allowed,
        )

    def test_wallet_balance_parses_market_wallet_info(self) -> None:
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
            captured["headers"] = dict(headers or {})
            return _FakeResponse(
                {},
                text=(
                    '<script>var g_rgWalletInfo = '
                    '{"wallet_currency":23,"wallet_balance":"2139","wallet_delayed_balance":"0","success":1};'
                    "</script>"
                ),
            )

        client._request = fake_request  # type: ignore[method-assign]

        wallet = client.wallet_balance()

        self.assertEqual("GET", captured["method"])
        self.assertEqual("/market/", captured["path"])
        self.assertEqual(
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            captured["headers"]["Accept"],  # type: ignore[index]
        )
        self.assertIn("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", captured["headers"]["User-Agent"])  # type: ignore[index]
        self.assertEqual("1", captured["headers"]["Upgrade-Insecure-Requests"])  # type: ignore[index]
        self.assertEqual(21.39, wallet["balance"])
        self.assertEqual(0.0, wallet["delayed_balance"])
        self.assertEqual("CNY", wallet["currency"])

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
            files: object | None = None,
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
                            "time_sold": 1780247000,
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
        self.assertEqual(1780247000, receipt["timeSold"])
        self.assertEqual(12.17, receipt["receivedAmount"])
        self.assertEqual("2023", receipt["receivedCurrencyId"])


    def test_find_sale_receipt_by_asset_reads_asset_from_purchase_payload(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
        )

        def fake_request(
            method: str,
            path: str,
            *,
            params: dict[str, object] | None = None,
            data: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
            _allow_retry: bool = True,
        ) -> _FakeResponse:
            return _FakeResponse(
                {
                    "success": True,
                    "total_count": 1,
                    "events": [
                        {
                            "listingid": "listing-1",
                            "purchaseid": "purchase-1",
                            "event_type": 3,
                            "time_event": 1783512933,
                        }
                    ],
                    "purchases": {
                        "listing-1_purchase-1": {
                            "asset": {"id": "asset-1"},
                            "time_sold": 1783512000,
                            "received_amount": 156,
                            "received_currencyid": "2023",
                        }
                    },
                }
            )

        client._request = fake_request  # type: ignore[method-assign]

        receipt = client.find_sale_receipt_by_asset("asset-1")

        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual("listing-1", receipt["listingId"])
        self.assertEqual("purchase-1", receipt["purchaseId"])
        self.assertEqual(1783512000, receipt["timeSold"])
        self.assertEqual(1.56, receipt["receivedAmount"])
        self.assertEqual("2023", receipt["receivedCurrencyId"])

    def test_find_sale_receipts_for_targets_shares_history_pages_and_finds_page_three(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
        )
        starts: list[int] = []

        def fake_history(*, start: int = 0, count: int = 100) -> dict[str, object]:
            starts.append(start)
            if start < 200:
                return {
                    "success": True,
                    "total_count": 300,
                    "events": [
                        {
                            "listingid": f"unrelated-{start}",
                            "purchaseid": f"purchase-{start}",
                            "event_type": 3,
                        }
                    ],
                    "purchases": {},
                }
            return {
                "success": True,
                "total_count": 300,
                "events": [
                    {
                        "listingid": "listing-page-3",
                        "purchaseid": "purchase-page-3",
                        "event_type": 3,
                        "time_event": 1783512933,
                    }
                ],
                "purchases": {
                    "listing-page-3_purchase-page-3": {
                        "asset": {"id": "asset-page-3"},
                        "time_sold": 1783512000,
                        "received_amount": 156,
                        "received_currencyid": "2023",
                    }
                },
            }

        client.market_history = fake_history  # type: ignore[method-assign]

        receipts = client.find_sale_receipts_for_targets(
            [
                {
                    "key": "operation-1",
                    "listingId": "listing-page-3",
                    "assetId": "asset-page-3",
                },
                {
                    "key": "operation-2",
                    "listingId": "listing-never-sold",
                    "assetId": "asset-never-sold",
                },
            ],
            max_pages=3,
        )

        self.assertEqual([0, 100, 200], starts)
        self.assertEqual({"operation-1"}, set(receipts))
        self.assertEqual("purchase-page-3", receipts["operation-1"]["purchaseId"])
        self.assertEqual(1.56, receipts["operation-1"]["receivedAmount"])

    def test_find_purchase_receipt_matches_item_time_and_paid_total(self) -> None:
        client = SteamMarketClient(
            cookies="sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
            steam_id64="76561198000000000",
        )

        def fake_request(
            method: str,
            path: str,
            *,
            params: dict[str, object] | None = None,
            data: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
            _allow_retry: bool = True,
        ) -> _FakeResponse:
            return _FakeResponse(
                {
                    "success": True,
                    "total_count": 1,
                    "events": [
                        {
                            "listingid": "listing-buy-1",
                            "purchaseid": "purchase-buy-1",
                            "event_type": 4,
                            "time_event": 1783608287,
                        }
                    ],
                    "purchases": {
                        "listing-buy-1_purchase-buy-1": {
                            "paid_amount": 14713,
                            "paid_fee": 2206,
                            "currencyid": "2023",
                            "asset": {
                                "appid": 730,
                                "contextid": "16",
                                "id": "old-asset-1",
                                "new_id": "new-asset-1",
                                "new_contextid": "2",
                            },
                        }
                    },
                    "assets": {
                        "730": {
                            "2": {
                                "old-asset-1": {
                                    "id": "old-asset-1",
                                    "market_hash_name": "Desert Eagle | Mulberry (Factory New)",
                                }
                            }
                        }
                    },
                }
            )

        client._request = fake_request  # type: ignore[method-assign]

        receipt = client.find_purchase_receipt(
            market_hash_name="Desert Eagle | Mulberry (Factory New)",
            expected_total=169.19,
            earliest_time=1783608000,
        )

        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual("listing-buy-1", receipt["listingId"])
        self.assertEqual("purchase-buy-1", receipt["purchaseId"])
        self.assertEqual(1783608287, receipt["timePurchased"])
        self.assertEqual(147.13, receipt["paidAmount"])
        self.assertEqual(22.06, receipt["paidFee"])
        self.assertEqual(169.19, receipt["paidTotal"])
        self.assertEqual("old-asset-1", receipt["assetId"])
        self.assertEqual("new-asset-1", receipt["newAssetId"])
        self.assertEqual("Desert Eagle | Mulberry (Factory New)", receipt["marketHashName"])


if __name__ == "__main__":
    unittest.main()
