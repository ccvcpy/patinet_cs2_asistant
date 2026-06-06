from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from cs2_assistant.accounts.steam_auth import _verify_steam_cookies_valid


class _FakeResponse:
    def __init__(self, *, status_code: int, url: str, payload: dict[str, object] | None = None) -> None:
        self.status_code = status_code
        self.url = url
        self._payload = payload

    def json(self) -> dict[str, object]:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class SteamAuthTests(unittest.TestCase):
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
