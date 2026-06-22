from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from cs2_assistant.clients.c5game import C5GameClient, C5GameError


class C5GameClientTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
