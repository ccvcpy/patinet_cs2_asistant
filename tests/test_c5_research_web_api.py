from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
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
from cs2_assistant.db import Database
from cs2_assistant.services.runtime_controller import UnifiedRuntimeController
import cs2_assistant.services.web_api as web_api


class OneRequestServer(HTTPServer):
    """Use the same real-socket, one-request pattern as test_guadao_web_api."""

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.handle_request()


class _FakeSweeper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def close(self) -> None:
        return


class _FakeCaseMonitor:
    def start(self) -> None:
        return


class _NoThreadRuntime(UnifiedRuntimeController):
    """Keep real queue/control methods while preventing a background worker."""

    def start(self) -> None:
        return


class C5ResearchWebApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(
            db_path=Path(self.temporary.name) / "assistant.db",
        )
        self.runtime = _NoThreadRuntime(self.settings, poll_seconds=0.2)
        self.case_monitor = _FakeCaseMonitor()
        self.catalog_names = [
            "Alpha Integration Case",
            "Bravo Integration Case",
            "Charlie Integration Case",
        ]
        self._seed_catalog()

    def tearDown(self) -> None:
        self.runtime.stop(timeout=0)

    def _seed_catalog(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
        finally:
            db.close()

        now = "2026-08-04T00:00:00+00:00"
        rows = []
        for index, market_hash_name in enumerate(self.catalog_names):
            raw = {
                "market_hash_name": market_hash_name,
                "name": f"Integration Case {index + 1}",
                "csgoApi": {
                    "source": "ByMykel/CSGO-API",
                    "category": "crates",
                    "categories": ["crates"],
                },
                "type": "Case",
                "rarity": {
                    "id": "rarity_common",
                    "name": "Common",
                    "color": "#b0c3d9",
                },
            }
            rows.append(
                (
                    market_hash_name,
                    f"Integration Case {index + 1}",
                    json.dumps(raw, ensure_ascii=False),
                    now,
                    now,
                )
            )
        with closing(sqlite3.connect(self.settings.db_path)) as connection, connection:
            connection.executemany(
                """
                INSERT INTO items (
                    market_hash_name, name_cn, raw_json, imported_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _request_bytes(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[int, bytes]:
        port = self._free_port()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                web_api.run_profit_trade_api_server(
                    self.settings,
                    host="127.0.0.1",
                    port=port,
                    runtime_controller=self.runtime,
                    case_monitor_controller=self.case_monitor,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with (
            patch.object(web_api, "ThreadingHTTPServer", OneRequestServer),
            patch.object(web_api, "C5CaseSweeper", _FakeSweeper),
            patch.object(
                web_api,
                "get_profit_trade_event_logger",
                return_value=object(),
            ),
            patch.object(
                web_api,
                "get_guadao_event_logger",
                return_value=object(),
            ),
        ):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            request = Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                headers={"Content-Type": "application/json"} if data is not None else {},
                method=method,
            )
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
        self.assertFalse(thread.is_alive(), "one-request API server did not stop")
        return status, body

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[int, dict]:
        status, body = self._request_bytes(method, path, payload)
        return status, json.loads(body.decode("utf-8"))

    def _create_scan(self) -> tuple[str, dict]:
        status, payload = self._request(
            "POST",
            "/api/c5-research/scans",
            {"categoryIds": ["crates"]},
        )
        self.assertEqual(202, status, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["researchOnly"])
        self.assertFalse(payload["canExecute"])
        request_id = str(payload["requestId"])
        self.assertTrue(request_id.startswith("C5RS-"))
        return request_id, payload

    @staticmethod
    def _assert_research_only(test_case: unittest.TestCase, payload: dict) -> None:
        test_case.assertTrue(payload["researchOnly"])
        test_case.assertFalse(payload["canExecute"])

    def test_post_scan_returns_202_and_get_status_reads_the_persisted_job(self) -> None:
        request_id, created = self._create_scan()

        self.assertEqual(202, created["httpStatus"])
        self.assertEqual("queued", created["status"])
        self.assertEqual(3, created["matchedCount"])

        status, persisted = self._request(
            "GET",
            f"/api/c5-research/scans/{request_id}",
        )

        self.assertEqual(200, status, persisted)
        self.assertTrue(persisted["ok"])
        self.assertEqual(request_id, persisted["requestId"])
        self.assertEqual("queued", persisted["status"])
        self.assertEqual(3, persisted["matchedCount"])
        self._assert_research_only(self, persisted)

    def test_get_results_applies_server_side_pagination_and_roi_sorting(self) -> None:
        request_id, _ = self._create_scan()
        roi_by_name = {
            "Alpha Integration Case": 0.10,
            "Bravo Integration Case": 0.30,
            "Charlie Integration Case": 0.20,
        }
        with closing(sqlite3.connect(self.settings.db_path)) as connection, connection:
            for market_hash_name, expected_roi in roi_by_name.items():
                connection.execute(
                    """
                    UPDATE c5_research_scan_results
                    SET status = 'observed', expected_roi = ?,
                        expected_profit = ?, c5_listing_price = 10.0,
                        steam_sell_price = 10.0
                    WHERE request_id = ? AND market_hash_name = ?
                    """,
                    (
                        expected_roi,
                        expected_roi * 10.0,
                        request_id,
                        market_hash_name,
                    ),
                )

        first_status, first_page = self._request(
            "GET",
            f"/api/c5-research/scans/{request_id}/results?page=1&pageSize=2&sort=roi_desc",
        )
        second_status, second_page = self._request(
            "GET",
            f"/api/c5-research/scans/{request_id}/results?page=2&pageSize=2&sort=roi_desc",
        )

        self.assertEqual(200, first_status, first_page)
        self.assertEqual(200, second_status, second_page)
        self.assertEqual(3, first_page["total"])
        self.assertEqual(1, first_page["page"])
        self.assertEqual(2, first_page["pageSize"])
        self.assertEqual(
            ["Bravo Integration Case", "Charlie Integration Case"],
            [item["marketHashName"] for item in first_page["items"]],
        )
        self.assertEqual(
            ["Alpha Integration Case"],
            [item["marketHashName"] for item in second_page["items"]],
        )
        self._assert_research_only(self, first_page)
        self._assert_research_only(self, second_page)
        for item in first_page["items"] + second_page["items"]:
            self._assert_research_only(self, item)

    def test_estimate_is_read_only_and_uses_the_real_catalog_filter(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/c5-research/estimate",
            {"categoryIds": ["crates"]},
        )

        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(3, payload["totalCatalogCount"])
        self.assertEqual(3, payload["catalogMatchedCount"])
        self.assertEqual(3, payload["requiresC5PriceCount"])
        self._assert_research_only(self, payload)

    def test_pause_resume_and_cancel_return_202_and_persist_each_state(self) -> None:
        request_id, _ = self._create_scan()

        for action, expected_status in (
            ("pause", "paused"),
            ("resume", "queued"),
            ("cancel", "cancelled"),
        ):
            with self.subTest(action=action):
                status, payload = self._request(
                    "POST",
                    f"/api/c5-research/scans/{request_id}/{action}",
                    {},
                )
                self.assertEqual(202, status, payload)
                self.assertTrue(payload["ok"])
                self.assertEqual(action, payload["action"])
                self.assertEqual(expected_status, payload["status"])
                self._assert_research_only(self, payload)

        final_status, final_payload = self._request(
            "GET",
            f"/api/c5-research/scans/{request_id}",
        )
        self.assertEqual(200, final_status, final_payload)
        self.assertEqual("cancelled", final_payload["status"])
        self.assertTrue(final_payload["terminal"])
        self._assert_research_only(self, final_payload)

    def test_invalid_inputs_are_400_and_missing_resources_are_404(self) -> None:
        request_id, _ = self._create_scan()
        cases = (
            (
                "missing status",
                "GET",
                "/api/c5-research/scans/C5RS-does-not-exist",
                None,
                404,
            ),
            (
                "missing results",
                "GET",
                "/api/c5-research/scans/C5RS-does-not-exist/results",
                None,
                404,
            ),
            (
                "missing action target",
                "POST",
                "/api/c5-research/scans/C5RS-does-not-exist/pause",
                {},
                404,
            ),
            (
                "invalid sort",
                "GET",
                f"/api/c5-research/scans/{request_id}/results?sort=not-a-sort",
                None,
                400,
            ),
            (
                "invalid page",
                "GET",
                f"/api/c5-research/scans/{request_id}/results?page=not-an-int",
                None,
                400,
            ),
            (
                "invalid filter",
                "POST",
                "/api/c5-research/scans",
                {"unsupportedFilter": True},
                400,
            ),
            (
                "unknown get subroute",
                "GET",
                f"/api/c5-research/scans/{request_id}/unknown",
                None,
                404,
            ),
            (
                "unknown action",
                "POST",
                f"/api/c5-research/scans/{request_id}/execute",
                {},
                404,
            ),
        )

        for label, method, path, body, expected_status in cases:
            with self.subTest(case=label):
                status, payload = self._request(method, path, body)
                self.assertEqual(expected_status, status, payload)
                self.assertFalse(payload.get("ok", False), payload)


if __name__ == "__main__":
    unittest.main()
