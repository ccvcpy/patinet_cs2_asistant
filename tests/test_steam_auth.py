from __future__ import annotations

import os
import tempfile
import unittest
import json
from unittest.mock import patch

import requests

from cs2_assistant.accounts.store import AccountStore
from cs2_assistant.accounts.steam_auth import (
    _Cs2SteamLoginExecutor,
    _do_steampy_login,
    try_steam_auto_relogin,
    _verify_steam_cookies_valid,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        url: str,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict[str, object]:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class SteamAuthTests(unittest.TestCase):
    def test_login_request_posts_input_json_form_payload(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.post_calls: list[dict[str, object]] = []

            def post(self, url: str, **kwargs: object) -> _FakeResponse:
                self.post_calls.append({"url": url, **kwargs})
                return _FakeResponse(status_code=200, url=url, payload={"response": {}})

        fake_session = FakeSession()
        executor = _Cs2SteamLoginExecutor("user", "pass", "shared", fake_session)

        with patch.object(executor, "_fetch_rsa_params", return_value={"rsa_timestamp": "123", "rsa_key": object()}):
            with patch.object(executor, "_encrypt_password", return_value=b"encrypted"):
                executor._send_login_request()

        self.assertEqual(1, len(fake_session.post_calls))
        call = fake_session.post_calls[0]
        data = call["data"]
        self.assertIsInstance(data, dict)
        self.assertIn("input_json", data)
        payload = json.loads(data["input_json"])
        self.assertEqual("user", payload["account_name"])
        self.assertEqual("encrypted", payload["encrypted_password"])
        self.assertNotIn("json", call)

    def test_login_request_includes_current_steam_auth_session_context(self) -> None:
        executor = _Cs2SteamLoginExecutor("user", "pass", "shared", requests.Session())

        payload = executor._prepare_login_request_data("encrypted", "123456")

        self.assertEqual("user", payload["account_name"])
        self.assertEqual("encrypted", payload["encrypted_password"])
        self.assertEqual("123456", payload["encryption_timestamp"])
        self.assertEqual("1", payload["persistence"])
        self.assertEqual("Community", payload["website_id"])
        self.assertEqual(2, payload["platform_type"])
        self.assertIn("device_friendly_name", payload)
        self.assertIn("language", payload)
        self.assertIn("device_details", payload)
        self.assertIsInstance(payload["device_details"], dict)

    def test_login_maps_steam_throttle_eresult_to_clear_status(self) -> None:
        executor = _Cs2SteamLoginExecutor("user", "pass", "shared", requests.Session())

        with patch.object(
            executor,
            "_send_login_request",
            return_value=_FakeResponse(
                status_code=200,
                url="https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1",
                payload={"response": {}},
                headers={"x-eresult": "87"},
            ),
        ):
            with self.assertRaisesRegex(Exception, "steam_auth_throttled"):
                executor.login()

    def test_steampy_login_maps_missing_client_id_to_actionable_status(self) -> None:
        class FakeSession:
            headers: dict[str, object] = {}
            cookies: object = object()

        class FakeSteamClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._session = FakeSession()

            def login(self) -> None:
                raise KeyError("client_id")

        with patch("steampy.client.SteamClient", FakeSteamClient):
            ok, status, cookies = _do_steampy_login(
                "user",
                "pass",
                {"shared_secret": "secret", "identity_secret": "", "device_id": "", "steamid": ""},
            )

        self.assertFalse(ok)
        self.assertEqual("steam_auth_session_unavailable", status)
        self.assertEqual({}, cookies)

    def test_auto_relogin_falls_back_to_browser_login_when_steampy_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"CS2_MASTER_KEY": "test-key"}):
                store = AccountStore(tmpdir)
                account = store.add_account(
                    name="steam-account",
                    username="steam-account",
                    password="correct-password",
                    shared_secret="shared-secret",
                    identity_secret="identity-secret",
                    device_id="android:test",
                )

                with patch(
                    "cs2_assistant.accounts.steam_auth._do_steampy_login",
                    return_value=(False, "ip_blocked", {}),
                ):
                    with patch(
                        "cs2_assistant.accounts.steam_auth._do_playwright_login",
                        return_value=(
                            True,
                            "browser_auto_ok",
                            {
                                "sessionid": "session-1",
                                "steamLoginSecure": "76561198000000000%7C%7Ctoken",
                            },
                        ),
                    ) as browser_login:
                        ok, status, updated = try_steam_auto_relogin(
                            store,
                            account_id=account.id,
                            force_login=True,
                        )

                self.assertTrue(ok)
                self.assertEqual("auto_ok", status)
                self.assertIsNotNone(updated)
                self.assertEqual("76561198000000000", updated.steam_id64)
                self.assertIn("steamLoginSecure=76561198000000000%7C%7Ctoken", updated.cookies or "")
                browser_login.assert_called_once()

    def test_verify_steam_cookies_valid_rejects_market_400(self) -> None:
        responses = [
            _FakeResponse(
                status_code=400,
                url="https://steamcommunity.com/market/mylistings?start=0&count=1&norender=1",
            )
        ]

        with patch("requests.Session.get", side_effect=responses):
            ok = _verify_steam_cookies_valid(
                "sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
                "76561198000000000",
            )

        self.assertFalse(ok)

    def test_verify_steam_cookies_valid_falls_back_on_market_timeout(self) -> None:
        def fake_get(*args: object, **kwargs: object) -> object:
            url = str(args[1] if len(args) > 1 else kwargs.get("url") or "")
            if "steamcommunity.com/market/mylistings" in url:
                raise requests.Timeout("boom")
            if "store.steampowered.com/pointssummary/ajaxgetasyncconfig" in url:
                return _FakeResponse(
                    status_code=200,
                    url=url,
                    payload={"logged_in": True},
                )
            raise AssertionError(f"unexpected url: {url}")

        with patch("requests.Session.get", side_effect=fake_get):
            ok = _verify_steam_cookies_valid(
                "sessionid=session-1; steamLoginSecure=76561198000000000%7C%7Ctoken",
                "76561198000000000",
            )

        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
