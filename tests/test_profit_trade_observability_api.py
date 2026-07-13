from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
import cs2_assistant.services.web_api as web_api


MARKET_HASH_NAME = "USP-S | Tropical Breeze (Factory New)"


class OneRequestServer(HTTPServer):
    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.handle_request()


class FakeSweeper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def close(self) -> None:
        return


class FakeLogger:
    pass


class ProfitTradeObservabilityApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.temp_dir.name) / "assistant.db")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
        finally:
            db.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
    ) -> tuple[int, dict]:
        port = self._free_port()
        server_errors: list[BaseException] = []

        def run() -> None:
            try:
                web_api.run_profit_trade_api_server(
                    self.settings,
                    host="127.0.0.1",
                    port=port,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                server_errors.append(exc)

        with (
            patch.object(web_api, "ThreadingHTTPServer", OneRequestServer),
            patch.object(web_api, "C5CaseSweeper", FakeSweeper),
            patch.object(web_api, "get_profit_trade_event_logger", return_value=FakeLogger()),
        ):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            raw_body = json.dumps(body or {}).encode("utf-8") if body is not None else None
            request = Request(
                f"http://127.0.0.1:{port}{path}",
                data=raw_body,
                method=method,
                headers={"Content-Type": "application/json"} if raw_body is not None else {},
            )
            response_body = b""
            status = 0
            deadline = time.monotonic() + 3.0
            while True:
                try:
                    with urlopen(request, timeout=2.0) as response:
                        status = int(response.status)
                        response_body = response.read()
                    break
                except HTTPError as exc:
                    status = int(exc.code)
                    response_body = exc.read()
                    break
                except URLError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            thread.join(timeout=3.0)
        if thread.is_alive():
            self.fail("one-request API server did not stop")
        if server_errors:
            raise server_errors[0]
        return status, json.loads(response_body.decode("utf-8"))

    def test_roi_watch_and_history_endpoints_match_frontend_contract(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.record_profit_trade_roi_scan(
                [
                    {
                        "market_hash_name": MARKET_HASH_NAME,
                        "name_cn": "USP消音版 | 椰风花语（崭新出厂）",
                        "steam_buy_price": 100.0,
                        "c5_listing_price": 75.0,
                        "c5_expected_net_price": 74.25,
                        "balance_discount": 0.69,
                        "expected_profit": 5.25,
                        "expected_roi": 0.0525,
                        "min_roi": 0.08,
                        "manual_review_roi": 0.20,
                        "inventory_count": 1,
                        "tradable_count": 1,
                        "risk_status": "passed",
                        "execution_status": "below_min_roi",
                        "execution_reason": "ROI below automatic threshold",
                    }
                ],
                scan_id="PTSCAN-api-contract",
                observed_at="2026-07-13T01:02:03+00:00",
            )
        finally:
            db.close()

        status, payload = self._request(
            "GET",
            "/api/profit-trade/roi-watch?active=true&page=1&pageSize=12&sort=roi_desc",
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual({"items", "total", "page", "pageSize"}, {key for key in payload if key in {"items", "total", "page", "pageSize"}})
        self.assertEqual("observe_only", payload["items"][0]["executionStatus"])
        self.assertEqual("below_min_roi", payload["items"][0]["executionStatusCode"])

        query = urlencode(
            {
                "marketHashName": MARKET_HASH_NAME,
                "from": "2026-07-13T01:02:03.000Z",
                "to": "2026-07-13T01:02:03.000Z",
                "page": 1,
                "pageSize": 20,
            }
        )
        history_status, history = self._request(
            "GET",
            f"/api/profit-trade/roi-watch/history?{query}",
        )
        self.assertEqual(200, history_status)
        self.assertEqual(1, history["total"])
        self.assertEqual("PTSCAN-api-contract", history["items"][0]["scanId"])

    def test_interruption_endpoint_searches_chinese_name_and_rejects_completed(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-interruption",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="Steam search_listings HTTP 429",
                note=(
                    '{"name":"USP消音版 | 椰风花语（崭新出厂）",'
                    '"cancelSource":"profit_trade_search_listings",'
                    '"cancelReason":"Steam HTTP 429"}'
                ),
            )
        finally:
            db.close()

        query = urlencode(
            {
                "keyword": "椰风花语",
                "status": "cancelled",
                "acknowledged": "exclude",
                "page": 1,
                "pageSize": 20,
            }
        )
        status, payload = self._request("GET", f"/api/profit-trade/interruptions?{query}")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["total"])
        self.assertEqual(trade_id, payload["items"][0]["id"])
        self.assertEqual("profit_trade_search_listings", payload["items"][0]["cancelSource"])
        self.assertEqual(payload["total"], payload["summary"]["total"])

        invalid_status, invalid = self._request(
            "GET",
            "/api/profit-trade/interruptions?status=completed",
        )
        self.assertEqual(400, invalid_status)
        self.assertIn("invalid", invalid["error"])

    def test_timeline_endpoint_returns_only_truthful_historical_snapshot(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-history",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="HTTP 429 before Steam purchase",
            )
            db.conn.execute("DELETE FROM profit_trade_state_events WHERE trade_id = ?", (trade_id,))
            db.conn.commit()
        finally:
            db.close()

        status, payload = self._request(
            "GET",
            f"/api/profit-trade/interruptions/timeline?tradeId={trade_id}",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["events"]))
        event = payload["events"][0]
        self.assertTrue(event["isSnapshot"])
        self.assertEqual("historical_snapshot", event["eventType"])
        self.assertIsNone(event["statusFrom"])
        self.assertEqual("cancelled", event["statusTo"])

    def test_acknowledge_endpoint_never_hides_uncertain_order_silently(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-unsafe-ack",
                market_hash_name="Dreams & Nightmares Case",
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                note='{"steamBuyMethod":"createbuyorder","steamBuyOrderId":"buy-order-live"}',
            )
        finally:
            db.close()

        with patch.object(
            web_api,
            "dismiss_profit_trade",
            return_value={
                "ok": False,
                "changed": True,
                "dismissed": False,
                "message": "Steam buy completed; trade restored",
            },
        ) as dismiss:
            status, payload = self._request(
                "POST",
                "/api/profit-trade/interruptions/acknowledge",
                body={"tradeId": trade_id, "action": "acknowledge", "reason": "reviewed"},
            )
        self.assertEqual(409, status)
        self.assertFalse(payload["ok"])
        dismiss.assert_called_once()

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            acknowledgement = db.conn.execute(
                "SELECT * FROM profit_trade_acknowledgements WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        finally:
            db.close()
        self.assertIsNone(acknowledgement)

    def test_public_profit_trade_payloads_never_expose_authentication_material(self) -> None:
        secret_values = {
            "SECRET_TOKEN_SENTINEL",
            "SECRET_STYLE_TOKEN_SENTINEL",
            "SECRET_COOKIE_SENTINEL",
            "SECRET_SESSION_SENTINEL",
            "SECRET_API_KEY_SENTINEL",
            "SECRET_APP_KEY_SENTINEL",
            "SECRET_PASSWORD_SENTINEL",
            "SECRET_STEAM_GUARD_SENTINEL",
            "SECRET_IDENTITY_SENTINEL",
            "SECRET_DEVICE_SENTINEL",
            "SECRET_SHARED_SENTINEL",
            "SECRET_DEVICE_ID_SENTINEL",
            "https://steamcommunity.com/tradeoffer/new/?partner=123&token=SECRET_TRADE_URL_SENTINEL",
        }
        note = {
            "name": "USP消音版 | 椰风花语（崭新出厂）",
            "token": "SECRET_TOKEN_SENTINEL",
            "styleToken": "SECRET_STYLE_TOKEN_SENTINEL",
            "cookies": "SECRET_COOKIE_SENTINEL",
            "sessionid": "SECRET_SESSION_SENTINEL",
            "apiKey": "SECRET_API_KEY_SENTINEL",
            "app-key": "SECRET_APP_KEY_SENTINEL",
            "password": "SECRET_PASSWORD_SENTINEL",
            "steamGuardSecret": "SECRET_STEAM_GUARD_SENTINEL",
            "identity_secret": "SECRET_IDENTITY_SENTINEL",
            "device_secret": "SECRET_DEVICE_SENTINEL",
            "sharedSecret": "SECRET_SHARED_SENTINEL",
            "deviceId": "SECRET_DEVICE_ID_SENTINEL",
            "tradeUrl": "https://steamcommunity.com/tradeoffer/new/?partner=123&token=SECRET_TRADE_URL_SENTINEL",
            "request": {
                "headers": {
                    "Cookie": "sessionid=SECRET_SESSION_SENTINEL; steamLoginSecure=SECRET_COOKIE_SENTINEL"
                }
            },
            "cancelSource": "profit_trade_search_listings",
            "cancelReason": "Steam HTTP 429",
            "purchaseRequestSent": False,
            "walletBalanceBefore": 321.45,
            "walletBalanceAfter": 321.45,
            "walletDelta": 0.0,
        }
        error = (
            "request failed sessionid=SECRET_SESSION_SENTINEL "
            "api_key=SECRET_API_KEY_SENTINEL "
            "https://steamcommunity.com/tradeoffer/new/?partner=123&token=SECRET_TRADE_URL_SENTINEL"
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-sensitive-payload",
                market_hash_name=MARKET_HASH_NAME,
                status="manual_required",
                step_key="asset_locked",
                step_index=2,
                error=error,
                note=json.dumps(note, ensure_ascii=False),
            )
            db.record_profit_trade_roi_scan(
                [
                    {
                        "market_hash_name": MARKET_HASH_NAME,
                        "name_cn": note["name"],
                        "steam_buy_price": 100.0,
                        "c5_listing_price": 75.0,
                        "c5_expected_net_price": 74.25,
                        "balance_discount": 0.69,
                        "expected_profit": 5.25,
                        "expected_roi": 0.0525,
                        "min_roi": 0.08,
                        "manual_review_roi": 0.20,
                        "inventory_count": 1,
                        "tradable_count": 1,
                        "risk_status": "passed",
                        "execution_status": "below_min_roi",
                        "execution_reason": "sessionid=SECRET_SESSION_SENTINEL",
                        "raw": {
                            "token": "SECRET_TOKEN_SENTINEL",
                            "styleToken": "SECRET_STYLE_TOKEN_SENTINEL",
                        },
                    }
                ],
                scan_id="PTSCAN-sensitive-payload",
            )
        finally:
            db.close()

        responses: list[dict] = []
        for path in (
            "/api/profit-trade/dashboard",
            "/api/profit-trade/interruptions?status=manual_required&acknowledged=include",
            f"/api/profit-trade/interruptions/timeline?tradeId={trade_id}",
            "/api/profit-trade/roi-watch?active=true",
            f"/api/profit-trade/roi-watch/history?{urlencode({'marketHashName': MARKET_HASH_NAME})}",
        ):
            status, payload = self._request("GET", path)
            self.assertEqual(200, status, path)
            responses.append(payload)

        serialized = json.dumps(responses, ensure_ascii=False)
        for secret in secret_values:
            self.assertNotIn(secret, serialized)
        for sensitive_key in (
            '"token"',
            '"styleToken"',
            '"cookies"',
            '"sessionid"',
            '"apiKey"',
            '"app-key"',
            '"password"',
            '"steamGuardSecret"',
            '"identity_secret"',
            '"device_secret"',
            '"sharedSecret"',
            '"deviceId"',
            '"tradeUrl"',
        ):
            self.assertNotIn(sensitive_key, serialized)

        dashboard_trade = responses[0]["trades"][0]
        self.assertEqual("profit_trade_search_listings", dashboard_trade["cancelSource"])
        self.assertEqual("Steam HTTP 429", dashboard_trade["cancelReason"])
        self.assertFalse(dashboard_trade["purchaseRequestSent"])
        self.assertEqual(321.45, dashboard_trade["note"]["walletBalanceBefore"])
        self.assertEqual(321.45, dashboard_trade["note"]["walletBalanceAfter"])
        self.assertEqual(0.0, dashboard_trade["note"]["walletDelta"])

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            stored = db.get_profit_trade(trade_id)
            stored_note = json.loads(str(stored["note"]))
        finally:
            db.close()
        self.assertEqual("SECRET_TOKEN_SENTINEL", stored_note["token"])
        self.assertEqual("SECRET_STYLE_TOKEN_SENTINEL", stored_note["styleToken"])


if __name__ == "__main__":
    unittest.main()
