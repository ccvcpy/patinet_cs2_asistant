from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cs2_assistant.services.guadao_logging import (
    DEFAULT_GUADAO_LOG_DIR,
    GUADAO_LOG_DIR_ENV,
    GuadaoEventLogger,
    get_guadao_event_logger,
)
from cs2_assistant.services.profit_trade_logging import (
    DEFAULT_PROFIT_TRADE_LOG_DIR,
    PROFIT_TRADE_LOG_DIR_ENV,
    get_profit_trade_event_logger,
)


class GuadaoLoggingTests(unittest.TestCase):
    def test_pytest_default_loggers_never_write_formal_log_directories(self) -> None:
        guadao_test_dir = Path(os.environ[GUADAO_LOG_DIR_ENV]).resolve()
        profit_test_dir = Path(os.environ[PROFIT_TRADE_LOG_DIR_ENV]).resolve()
        self.assertNotEqual(DEFAULT_GUADAO_LOG_DIR.resolve(), guadao_test_dir)
        self.assertNotEqual(DEFAULT_PROFIT_TRADE_LOG_DIR.resolve(), profit_test_dir)
        formal_before = {
            path: (path.stat().st_mtime_ns, path.stat().st_size)
            for directory in (DEFAULT_GUADAO_LOG_DIR, DEFAULT_PROFIT_TRADE_LOG_DIR)
            if directory.exists()
            for path in directory.rglob("*")
            if path.is_file()
        }

        get_guadao_event_logger().emit(
            provider="local",
            component="pytest",
            operation="isolation_probe",
            message="guadao pytest isolation",
        )
        get_profit_trade_event_logger().emit(
            provider="local",
            component="pytest",
            operation="isolation_probe",
            message="profit pytest isolation",
        )

        self.assertTrue(any(guadao_test_dir.glob("*.jsonl")))
        self.assertTrue(any(profit_test_dir.glob("*.jsonl")))
        formal_after = {
            path: (path.stat().st_mtime_ns, path.stat().st_size)
            for directory in (DEFAULT_GUADAO_LOG_DIR, DEFAULT_PROFIT_TRADE_LOG_DIR)
            if directory.exists()
            for path in directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(formal_before, formal_after)

    def test_guadao_source_is_isolated_redacted_and_sse_consumable(self) -> None:
        now = datetime(2026, 7, 16, 4, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = GuadaoEventLogger(temporary, now_provider=lambda: now)
            logger.telemetry_callback(
                {
                    "source": "profit_trade",
                    "provider": "steam",
                    "component": "steam_market",
                    "operation": "order_book",
                    "message": "must not enter guadao logs",
                }
            )
            callback = logger.bind_telemetry(account_id="account-a", trade_no="op-1")
            callback(
                {
                    "provider": "steam",
                    "component": "steam_market",
                    "operation": "remove_listing",
                    "message": "撤单失败 cookie=session-secret",
                    "request_id": "request-1",
                    "status_code": 429,
                    "safe_context": {
                        "phase": "failure",
                        "identitySecret": "never-write-this",
                        "reason": "HTTP 429",
                    },
                }
            )

            rows = logger.query()["events"]
            self.assertEqual(1, len(rows))
            event = rows[0]
            self.assertEqual("guadao", event["source"])
            self.assertEqual("gdlog_", event["event_id"][:6])
            serialized = json.dumps(event, ensure_ascii=False)
            self.assertNotIn("session-secret", serialized)
            self.assertNotIn("never-write-this", serialized)
            self.assertIn("HTTP 429", serialized)
            self.assertEqual(
                [event["event_id"]],
                [row["event_id"] for row in logger.wait_after(None, timeout=0)],
            )

    def test_csv_export_keeps_chinese_and_query_filters(self) -> None:
        now = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = GuadaoEventLogger(temporary, now_provider=lambda: now)
            logger.emit(
                provider="c5",
                component="executor_engine",
                operation="rebuy_wait",
                message="补仓等待：梦魇武器箱",
                account_id="xiaodigu11",
                market_hash_name="Dreams & Nightmares Case",
                safe_context={"ratio": 0.7442},
            )
            logger.emit(
                provider="local",
                component="executor_engine",
                operation="state_change",
                message="not selected",
            )

            csv_payload = b"".join(
                logger.export_iter(
                    {"accountId": "xiaodigu11"},
                    format="csv",
                )
            )
            self.assertTrue(csv_payload.startswith(b"\xef\xbb\xbf"))
            decoded = csv_payload.decode("utf-8-sig")
            self.assertIn("补仓等待：梦魇武器箱", decoded)
            self.assertIn("Dreams & Nightmares Case", decoded)
            self.assertNotIn("not selected", decoded)

    def test_export_only_includes_shared_scheduler_when_explicitly_requested(self) -> None:
        now = datetime(2026, 7, 16, 5, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = GuadaoEventLogger(temporary, now_provider=lambda: now)
            logger.emit(
                provider="local",
                component="executor_engine",
                operation="c5_rebuy_waiting",
                message="guadao business event",
            )
            logger.emit(
                provider="steam",
                component="shared_steam_request_scheduler",
                operation="request_failure",
                message="[profit_trade] shared Steam request failure",
                status_code=429,
                safe_context={"source": "profit_trade", "sharedScheduler": True},
            )

            default_jsonl = b"".join(logger.export_iter(format="jsonl")).decode("utf-8")
            included_jsonl = b"".join(
                logger.export_iter(
                    {"includeSteamScheduler": True},
                    format="jsonl",
                )
            ).decode("utf-8")
            default_csv = b"".join(logger.export_iter(format="csv")).decode("utf-8-sig")
            included_csv = b"".join(
                logger.export_iter(
                    {"includeSteamScheduler": "true"},
                    format="csv",
                )
            ).decode("utf-8-sig")

            self.assertIn("guadao business event", default_jsonl)
            self.assertNotIn("shared_steam_request_scheduler", default_jsonl)
            self.assertIn("shared_steam_request_scheduler", included_jsonl)
            self.assertNotIn("shared_steam_request_scheduler", default_csv)
            self.assertIn("shared_steam_request_scheduler", included_csv)

    def test_export_applies_market_name_and_keyword_as_independent_filters(self) -> None:
        now = datetime(2026, 7, 16, 5, 45, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = GuadaoEventLogger(temporary, now_provider=lambda: now)
            logger.emit(
                provider="steam",
                component="steam_market",
                operation="search_listings",
                message="HTTP 429",
                market_hash_name="Dreams & Nightmares Case",
            )
            logger.emit(
                provider="steam",
                component="steam_market",
                operation="search_listings",
                message="HTTP 429",
                market_hash_name="Kilowatt Case",
            )
            logger.emit(
                provider="steam",
                component="steam_market",
                operation="search_listings",
                message="HTTP 200",
                market_hash_name="Dreams & Nightmares Case",
            )

            exported = b"".join(
                logger.export_iter(
                    {
                        "marketHashName": "Dreams & Nightmares",
                        "keyword": "429",
                    },
                    format="jsonl",
                )
            ).decode("utf-8")

            self.assertIn("Dreams & Nightmares Case", exported)
            self.assertIn("HTTP 429", exported)
            self.assertNotIn("Kilowatt Case", exported)
            self.assertNotIn("HTTP 200", exported)

    def test_export_filters_by_operation_id_and_http_status(self) -> None:
        now = datetime(2026, 7, 16, 5, 50, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = GuadaoEventLogger(temporary, now_provider=lambda: now)
            logger.emit(
                provider="steam",
                component="steam_market",
                operation="remove_listing",
                message="target request was rate limited",
                trade_no="GD-42",
                status_code=429,
            )
            logger.emit(
                provider="steam",
                component="steam_market",
                operation="remove_listing",
                message="different operation was rate limited",
                trade_no="GD-99",
                status_code=429,
            )
            logger.emit(
                provider="steam",
                component="steam_market",
                operation="remove_listing",
                message="target request succeeded",
                trade_no="GD-42",
                status_code=200,
            )

            exact = b"".join(
                logger.export_iter(
                    {"operationId": "42", "httpStatus": "429"},
                    format="jsonl",
                )
            ).decode("utf-8")
            client_errors = b"".join(
                logger.export_iter(
                    {"operationId": "GD-42", "httpStatus": "4xx"},
                    format="jsonl",
                )
            ).decode("utf-8")
            server_errors = b"".join(
                logger.export_iter(
                    {"operationId": "42", "httpStatus": "5xx"},
                    format="jsonl",
                )
            ).decode("utf-8")

            self.assertIn("target request was rate limited", exact)
            self.assertNotIn("different operation", exact)
            self.assertNotIn("target request succeeded", exact)
            self.assertIn("target request was rate limited", client_errors)
            self.assertEqual("", server_errors)

    def test_closed_utc_day_is_gzipped_and_retention_is_90_days(self) -> None:
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old_event = {
                "event_id": "gdlog_old",
                "timestamp_utc": "2026-07-15T23:59:00.000Z",
                "source": "guadao",
                "provider": "local",
                "component": "scheduler",
                "operation": "tick",
                "message": "closed day",
            }
            (directory / "2026-07-15.jsonl").write_text(
                json.dumps(old_event) + "\n",
                encoding="utf-8",
            )
            (directory / "2026-01-01.jsonl").write_text("{}\n", encoding="utf-8")
            logger = GuadaoEventLogger(directory, now_provider=lambda: now)

            logger.run_maintenance(now=now)

            self.assertTrue((directory / "2026-07-15.jsonl.gz").exists())
            self.assertFalse((directory / "2026-01-01.jsonl").exists())
            with gzip.open(directory / "2026-07-15.jsonl.gz", "rt", encoding="utf-8") as handle:
                self.assertIn("closed day", handle.read())
            self.assertEqual(90, logger.storage_status()["retentionDays"])
            self.assertEqual("gdlog_old", logger.get_event("gdlog_old")["event_id"])  # type: ignore[index]

    def test_log_failures_never_change_business_result(self) -> None:
        now = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            invalid = Path(temporary) / "occupied"
            invalid.write_text("not a directory", encoding="utf-8")
            logger = GuadaoEventLogger(invalid, now_provider=lambda: now)

            event = logger.emit(
                provider="local",
                component="executor_engine",
                operation="settle",
                message="交易结果不能受日志影响",
            )

            self.assertIsNotNone(event)
            self.assertEqual(1, len(logger.broker.snapshot()))


if __name__ == "__main__":
    unittest.main()
