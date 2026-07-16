from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
import cs2_assistant.services.web_api as web_api


class OneRequestServer(HTTPServer):
    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.handle_request()


class FakeSweeper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def close(self) -> None:
        return


class FakeProfitLogger:
    pass


class FakeGuadaoLogger:
    def __init__(self) -> None:
        self.export_filters: dict | None = None

    def query(self, filters: dict) -> dict:
        return {
            "events": [
                {
                    "event_id": "gd-1",
                    "timestamp_utc": "2026-07-16T10:00:00+00:00",
                    "level": "INFO",
                    "component": "guadao_runtime",
                    "operation": "scan",
                    "trade_no": "GD-42",
                    "status_code": 429,
                    "account_id": "guadao-account",
                    "market_hash_name": "Dreams & Nightmares Case",
                    "message": "scan complete",
                    "safe_context": {"operationId": 42},
                }
            ],
            "hasMore": False,
        }

    def storage_status(self) -> dict:
        return {
            "retentionDays": 90,
            "totalBytes": 1024,
            "fileCount": 1,
            "earliestTimestamp": "2026-07-16T00:00:00Z",
            "latestTimestamp": "2026-07-16T23:59:59Z",
        }

    def export_iter(self, filters: dict, *, format: str):
        self.export_filters = dict(filters)
        yield b'{"event_id":"gd-export"}\n'


class FakeRuntime:
    def __init__(self) -> None:
        self.operation_kwargs: dict | None = None
        self.migration_confirmed = False
        self.wake_count = 0

    def start(self) -> None:
        return

    def operations(self, **kwargs):
        self.operation_kwargs = kwargs
        return {"operations": [], "items": [], "total": 0, "page": kwargs["page"]}

    def dashboard(self) -> dict:
        return {
            "runtime": {"enabled": False},
            "cookieGate": {
                "status": "preparing",
                "validCount": 4,
                "totalCount": 5,
                "accounts": [{"accountId": "account-1", "status": "invalid"}],
            },
        }

    def retry_failed_steam_auth_now(self) -> dict:
        return {"ok": True, "queued": 1, "accountIds": ["account-1"]}

    def settings_payload(self) -> dict:
        return {"settings": {"global": {"maxListingRatioPct": 69.0}}}

    def update_settings(self, _payload: dict) -> dict:
        return self.settings_payload()

    def wake(self) -> None:
        self.wake_count += 1

    def confirm_migration(self) -> dict:
        self.migration_confirmed = True
        return {"ok": True}

    def _public_guadao_log(self, event: dict) -> dict:
        return {
            "id": event.get("event_id"),
            "timestamp": event.get("timestamp_utc"),
            "level": event.get("level"),
            "service": event.get("component"),
            "operation": event.get("operation"),
            "accountName": event.get("account_id"),
            "marketHashName": event.get("market_hash_name"),
            "message": event.get("message"),
            "httpStatus": event.get("status_code"),
            "operationId": (event.get("safe_context") or {}).get("operationId"),
            "tradeNo": event.get("trade_no"),
            "caller": "guadao",
            "detail": event.get("safe_context") or {},
        }

    def steam_scheduler_log_rows(self, *, limit: int = 1000) -> list[dict]:
        return [
            {
                "id": "steamq:pt-1",
                "timestamp": "2026-07-16T10:00:01+00:00",
                "level": "ERROR",
                "service": "steam_request_scheduler",
                "operation": "shared_request",
                "message": "profit_trade GET market/listings failed",
                "requestId": "pt-1",
                "caller": "profit_trade",
                "endpoint": "market/listings",
                "httpStatus": 429,
                "detail": {"source": "profit_trade"},
            }
        ]


class GuadaoWebApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()
        self.runtime = FakeRuntime()
        self.guadao_logger = FakeGuadaoLogger()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _request_bytes(self, method: str, path: str) -> tuple[int, bytes]:
        port = self._free_port()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                web_api.run_profit_trade_api_server(
                    self.settings,
                    host="127.0.0.1",
                    port=port,
                    runtime_controller=self.runtime,
                )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        with (
            patch.object(web_api, "ThreadingHTTPServer", OneRequestServer),
            patch.object(web_api, "C5CaseSweeper", FakeSweeper),
            patch.object(web_api, "get_profit_trade_event_logger", return_value=FakeProfitLogger()),
            patch.object(web_api, "get_guadao_event_logger", return_value=self.guadao_logger),
        ):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            request = Request(f"http://127.0.0.1:{port}{path}", method=method)
            deadline = time.monotonic() + 3
            while True:
                try:
                    with urlopen(request, timeout=2) as response:
                        status = int(response.status)
                        body = response.read()
                    break
                except HTTPError as exc:
                    status = int(exc.code)
                    body = exc.read()
                    break
                except URLError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            thread.join(timeout=3)
        if errors:
            raise errors[0]
        return status, body

    def _request(self, method: str, path: str) -> tuple[int, dict]:
        status, body = self._request_bytes(method, path)
        return status, json.loads(body.decode("utf-8"))

    def test_operations_forwards_pagination_and_filters(self) -> None:
        status, payload = self._request(
            "GET",
            "/api/guadao/operations?page=3&pageSize=25&q=case&account=acc-1&status=listed",
        )
        self.assertEqual(200, status)
        self.assertEqual(3, payload["page"])
        self.assertEqual(
            {
                "limit": 50000,
                "page": 3,
                "page_size": 25,
                "keyword": "case",
                "account_name": "acc-1",
                "status": "listed",
                "start_at": None,
                "end_at": None,
            },
            self.runtime.operation_kwargs,
        )

    def test_dashboard_exposes_safe_auth_health_after_public_sanitizing(self) -> None:
        status, payload = self._request("GET", "/api/guadao/dashboard")
        self.assertEqual(200, status)
        self.assertNotIn("cookieGate", payload)
        self.assertEqual(4, payload["steamAuthHealth"]["validCount"])
        self.assertEqual("account-1", payload["steamAuthHealth"]["accounts"][0]["accountId"])

    def test_retry_failed_auth_endpoint_only_queues_failed_accounts(self) -> None:
        status, payload = self._request("POST", "/api/guadao/auth/retry-failed")
        self.assertEqual(202, status)
        self.assertEqual(1, payload["queued"])

    def test_logs_can_include_cross_executor_scheduler_metadata(self) -> None:
        status, payload = self._request(
            "GET",
            "/api/guadao/logs?page=1&pageSize=20&includeSteamScheduler=true",
        )
        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["logs"]))
        self.assertEqual("profit_trade", payload["logs"][0]["caller"])
        self.assertEqual(429, payload["logs"][0]["httpStatus"])
        self.assertEqual(90, payload["meta"]["retentionDays"])

    def test_logs_filter_http_status_and_operation_id(self) -> None:
        status, payload = self._request(
            "GET",
            "/api/guadao/logs?page=1&pageSize=20&httpStatus=429&operationId=42",
        )
        self.assertEqual(200, status)
        self.assertEqual(["gd-1"], [row["id"] for row in payload["logs"]])

        status, payload = self._request(
            "GET",
            "/api/guadao/logs?page=1&pageSize=20&httpStatus=5xx&operationId=42",
        )
        self.assertEqual(200, status)
        self.assertEqual([], payload["logs"])

    def test_scheduler_metadata_obeys_the_same_public_log_filters(self) -> None:
        filters = (
            "endAt=2026-07-16T10%3A00%3A00.500000%2B00%3A00",
            "service=guadao_runtime",
            "operation=scan",
            "account=guadao-account",
            "marketHashName=Dreams%20%26%20Nightmares%20Case",
            "q=scan%20complete",
        )
        for query_filter in filters:
            with self.subTest(query_filter=query_filter):
                status, payload = self._request(
                    "GET",
                    (
                        "/api/guadao/logs?page=1&pageSize=20"
                        f"&includeSteamScheduler=true&{query_filter}"
                    ),
                )
                self.assertEqual(200, status)
                self.assertEqual(["gd-1"], [row["id"] for row in payload["logs"]])

    def test_guadao_migration_alias_and_nested_settings_contract(self) -> None:
        status, payload = self._request("POST", "/api/guadao/migration/confirm")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertTrue(self.runtime.migration_confirmed)

        status, payload = self._request("GET", "/api/guadao/settings")
        self.assertEqual(200, status)
        self.assertEqual(69.0, payload["settings"]["global"]["maxListingRatioPct"])
        self.assertEqual("single_channel", payload["settings"]["steamScheduler"]["mode"])
        self.assertEqual(120, payload["settings"]["steamScheduler"]["accountRouteCooldownSeconds"])
        self.assertEqual(600, payload["settings"]["steamScheduler"]["globalCooldownSeconds"])
        self.assertEqual(3600, payload["settings"]["steamScheduler"]["degradedAfterSeconds"])
        self.assertEqual(1800, payload["settings"]["steamScheduler"]["degradedProbeSeconds"])
        self.assertTrue(payload["settings"]["steamScheduler"]["quietWindowEnabled"])

    def test_settings_post_returns_same_scheduler_contract_as_get(self) -> None:
        status, payload = self._request("POST", "/api/guadao/settings")
        self.assertEqual(200, status)
        self.assertEqual("single_channel", payload["settings"]["steamScheduler"]["mode"])
        self.assertEqual(120, payload["settings"]["steamScheduler"]["accountRouteCooldownSeconds"])
        self.assertTrue(payload["settings"]["steamScheduler"]["quietWindowEnabled"])

    def test_run_due_explicitly_reports_unified_runtime_scope(self) -> None:
        status, payload = self._request("POST", "/api/guadao/runtime/run-due")
        self.assertEqual(202, status)
        self.assertEqual("unified_runtime", payload["scope"])
        self.assertEqual(1, self.runtime.wake_count)

    def test_log_export_forwards_scheduler_and_independent_text_filters(self) -> None:
        status, body = self._request_bytes(
            "GET",
            (
                "/api/guadao/logs/export?format=jsonl"
                "&includeSteamScheduler=true"
                "&marketHashName=Dreams%20%26%20Nightmares%20Case"
                "&operationId=42"
                "&httpStatus=429"
                "&q=429"
            ),
        )
        self.assertEqual(200, status)
        self.assertIn(b"gd-export", body)
        self.assertEqual(
            {
                "keyword": "429",
                "marketHashName": "Dreams & Nightmares Case",
                "operationId": "42",
                "httpStatus": "429",
                "includeSteamScheduler": True,
            },
            self.guadao_logger.export_filters,
        )


if __name__ == "__main__":
    unittest.main()
