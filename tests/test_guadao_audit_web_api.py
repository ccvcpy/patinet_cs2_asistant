from __future__ import annotations

import gc
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.services.guadao_audit import (
    AUDIT_TABLE_NAMES,
    cancel_guadao_audit_run,
    create_guadao_audit_run,
    export_guadao_audit,
    get_guadao_audit_run,
    initialize_guadao_audit_schema,
    list_guadao_audit_rows,
    retry_guadao_audit_run,
)
import cs2_assistant.services.web_api as web_api


START_AT = "2026-07-19T15:20:00+08:00"
END_AT = "2026-07-28T23:50:00+08:00"
TRADING_TABLES = (
    "inventory_pool",
    "inventory_assets",
    "pool_operations",
    "profit_trades",
)
MISSING = object()


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


class FakeCaseMonitor:
    def start(self) -> None:
        return


def snapshot_trading_tables(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return {
            table: [
                dict(row)
                for row in conn.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid ASC'
                ).fetchall()
            ]
            for table in TRADING_TABLES
        }


def seed_trading_sentinels(db_path: Path) -> None:
    timestamp = "2026-08-04T00:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO inventory_pool (
                market_hash_name, quantity, status, note, created_at, updated_at
            ) VALUES ('AUDIT-WEB-SENTINEL', 2, 'holding', 'must-not-change', ?, ?)
            """,
            (timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO inventory_assets (
                asset_id, market_hash_name, steam_id, tradable, status,
                last_seen_at, created_at
            ) VALUES (
                'AUDIT-WEB-ASSET', 'AUDIT-WEB-SENTINEL', 'steam-sentinel',
                1, 'available', ?, ?
            )
            """,
            (timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO pool_operations (
                market_hash_name, strategy, operation_type, status, quantity,
                expected_price, actual_price, asset_id, note, created_at
            ) VALUES (
                'AUDIT-WEB-SENTINEL', 'guadao', 'sentinel', 'completed', 1,
                1.23, 1.23, 'AUDIT-WEB-ASSET', 'must-not-change', ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO profit_trades (
                trade_no, market_hash_name, status, step_key, step_index,
                note, created_at, updated_at
            ) VALUES (
                'AUDIT-WEB-TRADE', 'AUDIT-WEB-SENTINEL', 'completed',
                'completed', 99, 'must-not-change', ?, ?
            )
            """,
            (timestamp, timestamp),
        )
        conn.commit()


class AuditRuntimeDouble:
    """A read-only runtime seam; HTTP tests still use the real audit service DB."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[str] = []
        self.retry_calls: list[str] = []

    def start(self) -> None:
        return

    @staticmethod
    def _pick(payload: dict[str, Any], camel: str, snake: str, default: Any = MISSING) -> Any:
        if camel in payload:
            return payload[camel]
        if snake in payload:
            return payload[snake]
        if default is MISSING:
            raise ValueError(f"{camel} is required")
        return default

    def _normalize_create_call(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        if args:
            if len(args) != 1 or not isinstance(args[0], dict) or kwargs:
                raise TypeError("queue_guadao_audit_run accepts one payload or keyword fields")
            raw = dict(args[0])
        else:
            raw = dict(kwargs)
        allowed = {
            "startAt",
            "start_at",
            "endAt",
            "end_at",
            "initialBalance",
            "initial_balance",
            "initialRealValue",
            "initial_real_value",
            "accountIds",
            "account_ids",
            "expectedAccountCount",
            "expected_account_count",
            "reportedComprehensiveRatio",
            "reported_comprehensive_ratio",
            "balanceToleranceCents",
            "balance_tolerance_cents",
            "ratioTolerance",
            "ratio_tolerance",
        }
        unsupported = sorted(set(raw) - allowed)
        if unsupported:
            raise ValueError(f"unsupported field(s): {', '.join(unsupported)}")
        account_ids = self._pick(raw, "accountIds", "account_ids", [])
        if not isinstance(account_ids, list) or not all(
            isinstance(value, str) and value.strip() for value in account_ids
        ):
            raise ValueError("accountIds must be an array of non-empty strings")
        normalized = {
            "start_at": self._pick(raw, "startAt", "start_at", START_AT),
            "end_at": self._pick(raw, "endAt", "end_at"),
            "initial_balance": self._pick(
                raw,
                "initialBalance",
                "initial_balance",
                "2502.92",
            ),
            "initial_real_value": self._pick(
                raw,
                "initialRealValue",
                "initial_real_value",
                "1755.474",
            ),
            "account_ids": account_ids,
            "expected_account_count": self._pick(
                raw,
                "expectedAccountCount",
                "expected_account_count",
                5,
            ),
            "reported_comprehensive_ratio": self._pick(
                raw,
                "reportedComprehensiveRatio",
                "reported_comprehensive_ratio",
                None,
            ),
            "balance_tolerance_cents": self._pick(
                raw,
                "balanceToleranceCents",
                "balance_tolerance_cents",
                1,
            ),
            "ratio_tolerance": self._pick(
                raw,
                "ratioTolerance",
                "ratio_tolerance",
                "0.000001",
            ),
        }
        return normalized

    def queue_guadao_audit_run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        normalized = self._normalize_create_call(args, kwargs)
        self.queue_calls.append(dict(normalized))
        return create_guadao_audit_run(self.settings, **normalized)

    def get_guadao_audit_run(self, request_id: str) -> dict[str, Any]:
        result = get_guadao_audit_run(self.settings, request_id)
        if result is None:
            raise KeyError(f"guadao audit run not found: {request_id}")
        return result

    guadao_audit_status = get_guadao_audit_run
    guadao_audit_run_status = get_guadao_audit_run

    def list_guadao_audit_rows(
        self,
        request_id: str,
        *,
        section: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        if section not in AUDIT_TABLE_NAMES:
            raise ValueError(f"unsupported guadao audit section: {section}")
        if int(page) < 1:
            raise ValueError("page must be at least 1")
        if int(page_size) < 1 or int(page_size) > 200:
            raise ValueError("pageSize must be between 1 and 200")
        rows = list_guadao_audit_rows(self.settings, request_id, table=section)
        start = (int(page) - 1) * int(page_size)
        items = rows[start : start + int(page_size)]
        return {
            "requestId": request_id,
            "section": section,
            "page": int(page),
            "pageSize": int(page_size),
            "total": len(rows),
            "hasMore": start + len(items) < len(rows),
            "items": items,
        }

    guadao_audit_rows = list_guadao_audit_rows

    def cancel_guadao_audit_run(self, request_id: str) -> dict[str, Any]:
        self.cancel_calls.append(request_id)
        return cancel_guadao_audit_run(self.settings, request_id)

    def retry_guadao_audit_run(self, request_id: str) -> dict[str, Any]:
        self.retry_calls.append(request_id)
        return retry_guadao_audit_run(self.settings, request_id)

    def export_guadao_audit(
        self,
        request_id: str,
        format_name: str,
    ) -> dict[str, str]:
        return export_guadao_audit(self.settings, request_id, format_name)


class GuadaoAuditWebApiIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            db_path=root / "assistant.db",
            steamdt_base_path=root / "steamdt.json",
            c5_api_key="audit-web-test-only-c5-key",
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
        finally:
            db.close()
        initialize_guadao_audit_schema(self.settings)
        seed_trading_sentinels(self.settings.db_path)
        self.runtime = AuditRuntimeDouble(self.settings)
        self.case_monitor = FakeCaseMonitor()

    def tearDown(self) -> None:
        # Release cyclic sqlite3 objects before Windows removes the temporary
        # WAL database.  A cleanup race must not obscure an HTTP contract gap.
        gc.collect()
        self.temp_dir.cleanup()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _request_bytes(
        self,
        method: str,
        path: str,
        payload: Any = MISSING,
        *,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        if payload is not MISSING and raw_body is not None:
            raise ValueError("payload and raw_body are mutually exclusive")
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

        if raw_body is not None:
            data = raw_body
        elif payload is not MISSING:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            data = None
        headers = {"Content-Type": "application/json"} if data is not None else {}

        with (
            patch.object(web_api, "ThreadingHTTPServer", OneRequestServer),
            patch.object(web_api, "C5CaseSweeper", FakeSweeper),
            patch.object(web_api, "get_profit_trade_event_logger", return_value=FakeLogger()),
            patch.object(web_api, "get_guadao_event_logger", return_value=FakeLogger()),
        ):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            request = Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                headers=headers,
                method=method,
            )
            deadline = time.monotonic() + 3
            while True:
                try:
                    with urlopen(request, timeout=2) as response:
                        status = int(response.status)
                        response_headers = {
                            key.lower(): value for key, value in response.headers.items()
                        }
                        body = response.read()
                    break
                except HTTPError as exc:
                    status = int(exc.code)
                    response_headers = {
                        key.lower(): value for key, value in exc.headers.items()
                    }
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
        return status, response_headers, body

    def _request_preserving_trading_tables(
        self,
        method: str,
        path: str,
        payload: Any = MISSING,
        *,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        before = snapshot_trading_tables(self.settings.db_path)
        response = self._request_bytes(
            method,
            path,
            payload,
            raw_body=raw_body,
        )
        self.assertEqual(before, snapshot_trading_tables(self.settings.db_path))
        return response

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Any = MISSING,
        *,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        status, headers, body = self._request_preserving_trading_tables(
            method,
            path,
            payload,
            raw_body=raw_body,
        )
        return status, headers, json.loads(body.decode("utf-8"))

    def _create_run(self) -> dict[str, Any]:
        return create_guadao_audit_run(
            self.settings,
            start_at=START_AT,
            end_at=END_AT,
            account_ids=["steam-1"],
            expected_account_count=1,
            reported_comprehensive_ratio="0.700000",
        )

    def _seed_rows(self, request_id: str, count: int = 5) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.settings.db_path) as conn:
            for index in range(1, count + 1):
                row = {
                    "rowKey": f"sale-{index}",
                    "marketHashName": f"Case {index}",
                    "verdict": "passed",
                    "reason": "fixture",
                }
                conn.execute(
                    """
                    INSERT INTO guadao_audit_checks (
                        request_id, table_name, row_key, verdict,
                        expected_value, actual_value, difference_value,
                        payload_json, created_at
                    ) VALUES (?, 'steam_sales', ?, 'passed', '1', '1', '0', ?, ?)
                    """,
                    (
                        request_id,
                        row["rowKey"],
                        json.dumps(row, ensure_ascii=False),
                        now,
                    ),
                )
            conn.commit()

    @staticmethod
    def _valid_create_payload() -> dict[str, Any]:
        return {
            "startAt": START_AT,
            "endAt": END_AT,
            "initialBalance": "2502.92",
            "initialRealValue": "1755.474",
            "accountIds": ["steam-1"],
            "expectedAccountCount": 1,
            "reportedComprehensiveRatio": "0.700000",
            "balanceToleranceCents": 1,
            "ratioTolerance": "0.000001",
        }

    def test_post_create_returns_202_then_get_status_is_pollable(self) -> None:
        create_status, _headers, created = self._request_json(
            "POST",
            "/api/guadao-audit/runs",
            self._valid_create_payload(),
        )

        self.assertEqual(202, create_status)
        self.assertTrue(created.get("ok"), created)
        request_id = str(created.get("requestId") or "")
        self.assertTrue(request_id.startswith("GDA-"), created)
        self.assertEqual("pending", created.get("status"))
        self.assertEqual(1, len(self.runtime.queue_calls))
        self.assertEqual(["steam-1"], self.runtime.queue_calls[0]["account_ids"])

        get_status, _headers, status_payload = self._request_json(
            "GET",
            f"/api/guadao-audit/runs/{request_id}",
        )
        self.assertEqual(200, get_status)
        self.assertTrue(status_payload.get("ok"), status_payload)
        self.assertEqual(request_id, status_payload.get("requestId"))
        self.assertEqual("pending", status_payload.get("status"))

    def test_rows_forwards_section_and_returns_real_pagination_metadata(self) -> None:
        request_id = str(self._create_run()["requestId"])
        self._seed_rows(request_id, count=5)

        status, _headers, payload = self._request_json(
            "GET",
            (
                f"/api/guadao-audit/runs/{request_id}/rows"
                "?section=steam_sales&page=2&pageSize=2"
            ),
        )

        self.assertEqual(200, status)
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual("steam_sales", payload.get("section"))
        self.assertEqual(2, payload.get("page"))
        self.assertEqual(2, payload.get("pageSize"))
        self.assertEqual(5, payload.get("total"))
        self.assertTrue(payload.get("hasMore"))
        self.assertEqual(
            ["sale-3", "sale-4"],
            [row.get("rowKey") for row in payload.get("rows") or []],
        )

    def test_cancel_and_retry_are_accepted_and_retry_preserves_old_run(self) -> None:
        old_request_id = str(self._create_run()["requestId"])

        cancel_status, _headers, cancelled = self._request_json(
            "POST",
            f"/api/guadao-audit/runs/{old_request_id}/cancel",
            {},
        )
        self.assertEqual(202, cancel_status)
        self.assertEqual("cancelled", cancelled.get("status"))
        self.assertEqual([old_request_id], self.runtime.cancel_calls)
        old_before_retry = get_guadao_audit_run(self.settings, old_request_id)

        retry_status, _headers, retried = self._request_json(
            "POST",
            f"/api/guadao-audit/runs/{old_request_id}/retry",
            {},
        )
        self.assertEqual(202, retry_status)
        new_request_id = str(retried.get("requestId") or "")
        self.assertTrue(new_request_id.startswith("GDA-"), retried)
        self.assertNotEqual(old_request_id, new_request_id)
        self.assertEqual(old_request_id, retried.get("retryOfRequestId"))
        self.assertEqual("pending", retried.get("status"))
        self.assertEqual([old_request_id], self.runtime.retry_calls)
        self.assertEqual(
            old_before_retry,
            get_guadao_audit_run(self.settings, old_request_id),
        )

    def test_export_json_csv_and_markdown_set_content_headers(self) -> None:
        request_id = str(self._create_run()["requestId"])
        self._seed_rows(request_id, count=1)
        expectations = {
            "json": ("application/json", ".json"),
            "csv": ("text/csv", ".csv"),
            "markdown": ("text/markdown", ".md"),
        }

        for format_name, (content_type, suffix) in expectations.items():
            with self.subTest(format_name=format_name):
                status, headers, body = self._request_preserving_trading_tables(
                    "GET",
                    (
                        f"/api/guadao-audit/runs/{request_id}/export"
                        f"?format={format_name}"
                    ),
                )
                self.assertEqual(200, status)
                self.assertTrue(
                    headers.get("content-type", "").startswith(content_type),
                    headers,
                )
                disposition = headers.get("content-disposition", "")
                self.assertIn("attachment", disposition.lower())
                self.assertIn(suffix, disposition)
                self.assertTrue(body)

    def test_export_xlsx_is_explicitly_rejected(self) -> None:
        request_id = str(self._create_run()["requestId"])

        status, _headers, payload = self._request_json(
            "GET",
            f"/api/guadao-audit/runs/{request_id}/export?format=xlsx",
        )

        self.assertEqual(400, status)
        self.assertFalse(payload.get("ok", False), payload)
        self.assertIn("json", str(payload.get("error") or "").lower())
        self.assertIn("csv", str(payload.get("error") or "").lower())
        self.assertIn("markdown", str(payload.get("error") or "").lower())

    def test_unknown_request_id_is_404_for_every_run_route(self) -> None:
        cases = (
            ("GET", "/api/guadao-audit/runs/GDA-UNKNOWN", MISSING),
            (
                "GET",
                "/api/guadao-audit/runs/GDA-UNKNOWN/rows?section=steam_sales&page=1&pageSize=20",
                MISSING,
            ),
            (
                "GET",
                "/api/guadao-audit/runs/GDA-UNKNOWN/export?format=json",
                MISSING,
            ),
            ("POST", "/api/guadao-audit/runs/GDA-UNKNOWN/cancel", {}),
            ("POST", "/api/guadao-audit/runs/GDA-UNKNOWN/retry", {}),
        )

        for method, path, payload in cases:
            with self.subTest(method=method, path=path):
                status, _headers, response = self._request_json(
                    method,
                    path,
                    payload,
                )
                self.assertEqual(404, status)
                self.assertFalse(response.get("ok", False), response)
                self.assertIn("audit", str(response.get("error") or "").lower())

    def test_create_rejects_malformed_non_object_and_semantically_invalid_input(self) -> None:
        invalid_requests = (
            ("malformed JSON", MISSING, b"{"),
            ("JSON array", MISSING, b"[]"),
            ("missing endAt", {"startAt": START_AT}, None),
            (
                "invalid endAt",
                {**self._valid_create_payload(), "endAt": "not-a-date"},
                None,
            ),
            (
                "reverse range",
                {
                    **self._valid_create_payload(),
                    "startAt": END_AT,
                    "endAt": START_AT,
                },
                None,
            ),
            (
                "accountIds is not an array",
                {**self._valid_create_payload(), "accountIds": "steam-1"},
                None,
            ),
            (
                "expectedAccountCount is zero",
                {**self._valid_create_payload(), "expectedAccountCount": 0},
                None,
            ),
            (
                "unsupported field",
                {**self._valid_create_payload(), "executeNow": True},
                None,
            ),
        )

        for label, payload, raw_body in invalid_requests:
            with self.subTest(label=label):
                calls_before = len(self.runtime.queue_calls)
                status, _headers, response = self._request_json(
                    "POST",
                    "/api/guadao-audit/runs",
                    payload,
                    raw_body=raw_body,
                )
                self.assertEqual(400, status)
                self.assertFalse(response.get("ok", False), response)
                if raw_body is not None:
                    self.assertEqual(
                        calls_before,
                        len(self.runtime.queue_calls),
                        "malformed/non-object JSON reached the runtime queue",
                    )

    def test_rows_rejects_invalid_section_page_and_page_size(self) -> None:
        request_id = str(self._create_run()["requestId"])
        invalid_queries = (
            "section=not_a_section&page=1&pageSize=20",
            "section=steam_sales&page=0&pageSize=20",
            "section=steam_sales&page=abc&pageSize=20",
            "section=steam_sales&page=1&pageSize=0",
            "section=steam_sales&page=1&pageSize=201",
            "page=1&pageSize=20",
        )
        observed_statuses: dict[str, int] = {}

        for query in invalid_queries:
            with self.subTest(query=query):
                status, _headers, response = self._request_json(
                    "GET",
                    f"/api/guadao-audit/runs/{request_id}/rows?{query}",
                )
                observed_statuses[query] = status
                if status == 400:
                    self.assertFalse(response.get("ok", False), response)

        accepted_invalid_queries = {
            query: status
            for query, status in observed_statuses.items()
            if status != 400
        }
        self.assertEqual(
            {},
            accepted_invalid_queries,
            "invalid pagination must be rejected, not clamped or replaced with defaults",
        )


if __name__ == "__main__":
    unittest.main()
