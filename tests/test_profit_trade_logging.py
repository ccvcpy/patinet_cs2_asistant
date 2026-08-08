from __future__ import annotations

import gzip
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cs2_assistant.services.profit_trade_logging import (
    DEFAULT_PROFIT_TRADE_LOG_DIR,
    PROFIT_TRADE_LOG_DIR_ENV,
    ProfitTradeEventBroker,
    ProfitTradeEventLogger,
    get_profit_trade_event_logger,
    redact_sensitive_data,
    reset_profit_trade_event_loggers,
)


class ProfitTradeLoggingTests(unittest.TestCase):
    def test_market_hash_name_filter_applies_to_query_and_export(self) -> None:
        fixed_now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = ProfitTradeEventLogger(temporary, now_provider=lambda: fixed_now)
            logger.emit(
                provider="local",
                component="profit_trade_scan",
                operation="item_evaluated",
                message="first",
                market_hash_name="USP-S | Cyrex (Factory New)",
            )
            logger.emit(
                provider="local",
                component="profit_trade_scan",
                operation="item_evaluated",
                message="second",
                market_hash_name="AK-47 | Redline (Field-Tested)",
            )

            queried = logger.query({"marketHashName": "USP-S | Cyrex (Factory New)"})
            exported = b"".join(
                logger.export_iter(
                    {"marketHashName": "USP-S | Cyrex (Factory New)"},
                    format="jsonl",
                )
            ).decode("utf-8")

            self.assertEqual(1, len(queried["events"]))
            self.assertEqual("USP-S | Cyrex (Factory New)", queried["events"][0]["market_hash_name"])
            self.assertIn("USP-S | Cyrex (Factory New)", exported)
            self.assertNotIn("AK-47 | Redline (Field-Tested)", exported)

    def test_default_factory_honors_explicit_log_directory_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {PROFIT_TRADE_LOG_DIR_ENV: temporary},
            ):
                reset_profit_trade_event_loggers()
                logger = get_profit_trade_event_logger()

            self.assertEqual(Path(temporary).resolve(), logger.log_dir)
            reset_profit_trade_event_loggers()

    def test_production_default_remains_project_log_directory_without_override(self) -> None:
        with patch.dict(os.environ, {PROFIT_TRADE_LOG_DIR_ENV: ""}):
            reset_profit_trade_event_loggers()
            logger = get_profit_trade_event_logger()

        self.assertEqual(DEFAULT_PROFIT_TRADE_LOG_DIR.resolve(), logger.log_dir)
        reset_profit_trade_event_loggers()

    def test_factory_returns_one_logger_and_broker_per_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = get_profit_trade_event_logger(log_dir=temporary, retention_days=90)
            second = get_profit_trade_event_logger({"logDirectory": temporary, "retentionDays": 30})

            self.assertIs(first, second)
            self.assertIs(first.broker, second.broker)
            self.assertEqual(90, second.retention_days)

    def test_emit_redacts_secrets_and_supports_query_cursor_lookup_and_export(self) -> None:
        fixed_now = datetime(2026, 7, 13, 7, 3, 41, 123000, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = ProfitTradeEventLogger(
                temporary,
                now_provider=lambda: fixed_now,
            )
            first = logger.emit(
                level="INFO",
                provider="steam",
                component="steam_market",
                operation="search_listings",
                message="request sessionid=secret-session token=secret-token",
                trade_no="PT-1",
                request_id="req-1",
                safe_context={
                    "cookie": "sessionid=secret-session",
                    "apiKey": "secret-key",
                    "nested": {
                        "tradeUrl": (
                            "https://steamcommunity.com/tradeoffer/new/"
                            "?partner=1&token=trade-token"
                        ),
                        "safe": "kept",
                    },
                },
            )
            second = logger.emit(
                level="ERROR",
                provider="steam",
                component="steam_market",
                operation="search_listings",
                message="HTTP 429 Too Many Requests",
                trade_no="PT-2",
                request_id="req-2",
                status_code=429,
                retry_after="7",
            )
            logger.emit(
                level="INFO",
                provider="c5",
                component="c5game",
                operation="sale_create",
                message="C5 listing created",
                trade_no="PT-3",
            )

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None
            assert second is not None
            self.assertEqual("2026-07-13T07:03:41.123Z", first["timestamp_utc"])
            serialized = json.dumps(first, ensure_ascii=False)
            self.assertNotIn("secret-session", serialized)
            self.assertNotIn("secret-token", serialized)
            self.assertNotIn("secret-key", serialized)
            self.assertNotIn("trade-token", serialized)
            self.assertEqual("kept", first["safe_context"]["nested"]["safe"])

            page_one = logger.query({"provider": "steam", "pageSize": 1})
            self.assertTrue(page_one["hasMore"])
            self.assertEqual(["PT-2"], [event["trade_no"] for event in page_one["events"]])
            page_two = logger.query(
                {
                    "provider": "steam",
                    "pageSize": 1,
                    "cursor": page_one["nextCursor"],
                }
            )
            self.assertFalse(page_two["hasMore"])
            self.assertEqual(["PT-1"], [event["trade_no"] for event in page_two["events"]])
            self.assertEqual(second, logger.get_event(str(second["event_id"])))

            jsonl_export = b"".join(
                logger.export_iter({"tradeNo": "PT-2"}, format="jsonl")
            ).decode("utf-8")
            readable_export = b"".join(
                logger.export_iter({"tradeNo": "PT-2"}, format="log")
            ).decode("utf-8")
            self.assertIn('"status_code":429', jsonl_export)
            self.assertIn("HTTP 429 Too Many Requests", readable_export)

    def test_threaded_append_produces_complete_unique_json_lines(self) -> None:
        fixed_now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = ProfitTradeEventLogger(temporary, now_provider=lambda: fixed_now)
            threads = [
                threading.Thread(
                    target=logger.emit,
                    kwargs={
                        "provider": "local",
                        "component": "test",
                        "operation": "concurrent",
                        "message": f"event-{index}",
                    },
                )
                for index in range(60)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            path = Path(temporary) / "2026-07-13.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines]
            self.assertEqual(60, len(events))
            self.assertEqual(60, len({event["event_id"] for event in events}))
            self.assertEqual(set(range(1, 61)), {event["sequence"] for event in events})

    def test_broker_is_bounded_and_wait_after_recovers_from_old_id(self) -> None:
        broker = ProfitTradeEventBroker(max_events=2)
        broker.publish({"event_id": "one", "sequence": 1})
        broker.publish({"event_id": "two", "sequence": 2})
        broker.publish({"event_id": "three", "sequence": 3})

        self.assertEqual(["two", "three"], [row["event_id"] for row in broker.snapshot()])
        self.assertEqual(["three"], [row["event_id"] for row in broker.wait_after("two", timeout=0)])
        self.assertEqual(
            ["two", "three"],
            [row["event_id"] for row in broker.wait_after("expired-id", timeout=0)],
        )

    def test_maintenance_compresses_closed_days_and_deletes_expired_days(self) -> None:
        now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            log_dir = Path(temporary)
            recent_event = {
                "event_id": "recent",
                "timestamp_utc": "2026-07-12T12:00:00.000Z",
                "source": "profit_trade",
                "provider": "steam",
                "component": "steam_market",
                "operation": "order_book",
                "message": "recent",
            }
            (log_dir / "2026-07-12.jsonl").write_text(
                f"{json.dumps(recent_event)}\n",
                encoding="utf-8",
            )
            (log_dir / "2026-01-01.jsonl").write_text("{}\n", encoding="utf-8")
            (log_dir / "2026-07-13.jsonl").write_text("", encoding="utf-8")
            logger = ProfitTradeEventLogger(log_dir, retention_days=90, now_provider=lambda: now)

            logger.run_maintenance(now=now)

            self.assertFalse((log_dir / "2026-07-12.jsonl").exists())
            self.assertTrue((log_dir / "2026-07-12.jsonl.gz").exists())
            self.assertFalse((log_dir / "2026-01-01.jsonl").exists())
            self.assertTrue((log_dir / "2026-07-13.jsonl").exists())
            with gzip.open(log_dir / "2026-07-12.jsonl.gz", "rt", encoding="utf-8") as handle:
                self.assertIn("recent", handle.read())
            self.assertEqual("recent", logger.get_event("recent")["event_id"])  # type: ignore[index]
            status = logger.storage_status()
            self.assertEqual(2, status["fileCount"])
            self.assertEqual(1, status["compressedFileCount"])
            self.assertEqual(90, status["retentionDays"])

    def test_log_write_failure_does_not_raise_and_still_publishes(self) -> None:
        fixed_now = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            invalid_directory = Path(temporary) / "not-a-directory"
            invalid_directory.write_text("occupied", encoding="utf-8")
            logger = ProfitTradeEventLogger(
                invalid_directory,
                now_provider=lambda: fixed_now,
            )
            with redirect_stderr(StringIO()):
                event = logger.emit(
                    provider="local",
                    component="state_machine",
                    operation="transition",
                    message="must not affect trading",
                )

            self.assertIsNotNone(event)
            self.assertEqual(1, len(logger.broker.snapshot()))

    def test_only_explicit_profit_trade_client_events_are_ingested(self) -> None:
        fixed_now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = ProfitTradeEventLogger(temporary, now_provider=lambda: fixed_now)
            logger.telemetry_callback(
                {
                    "source": "guadao",
                    "provider": "steam",
                    "component": "steam_market",
                    "operation": "order_book",
                    "message": "must be isolated",
                }
            )
            logger.telemetry_callback(
                {
                    "provider": "steam",
                    "component": "steam_market",
                    "operation": "order_book",
                    "message": "missing source must not be inferred",
                }
            )
            callback = logger.bind_telemetry(trade_no="PT-BOUND")
            callback(
                {
                    "provider": "c5",
                    "component": "c5game",
                    "operation": "sale_search",
                    "message": "bound C5 activity",
                }
            )

            rows = logger.query()["events"]
            self.assertEqual(1, len(rows))
            self.assertEqual("PT-BOUND", rows[0]["trade_no"])
            self.assertEqual("c5", rows[0]["provider"])

    def test_steam_client_events_include_cross_client_request_frequency(self) -> None:
        fixed_now = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            logger = ProfitTradeEventLogger(temporary, now_provider=lambda: fixed_now)
            callback = logger.bind_telemetry(trade_no="PT-FREQUENCY")
            callback(
                {
                    "provider": "steam",
                    "component": "steam_market",
                    "operation": "order_book",
                    "message": "first start",
                    "request_id": "request-1",
                    "safe_context": {"phase": "start"},
                }
            )
            callback(
                {
                    "provider": "steam",
                    "component": "steam_market",
                    "operation": "search_listings",
                    "message": "second start",
                    "request_id": "request-2",
                    "safe_context": {"phase": "start"},
                }
            )
            callback(
                {
                    "provider": "steam",
                    "component": "steam_market",
                    "operation": "search_listings",
                    "message": "second failed",
                    "request_id": "request-2",
                    "status_code": 429,
                    "safe_context": {"phase": "failure"},
                }
            )

            failure = logger.query({"requestId": "request-2"})["events"][0]
            frequency = failure["safe_context"]["request_frequency"]
            self.assertEqual(2, frequency["last_10_seconds"])
            self.assertEqual(2, frequency["last_60_seconds"])
            self.assertEqual(2, frequency["last_5_minutes"])
            self.assertEqual(1, frequency["current_concurrent"])

    def test_recursive_redaction_handles_all_named_secret_classes(self) -> None:
        value = redact_sensitive_data(
            {
                "Cookie": "sessionid=one",
                "password": "two",
                "api_key": "three",
                "steamGuardSecret": "four",
                "identity_secret": "five",
                "deviceSecret": "six",
                "app-key": "seven",
                "tradeUrl": "eight",
                "token": "nine",
                "styleToken": "ten",
                "request_body": {"otherwiseSafeLooking": "eleven"},
                "headers": {"X-Debug": "twelve"},
                "safe": "visible",
            }
        )
        serialized = json.dumps(value)
        for secret in (
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual("visible", value["safe"])


if __name__ == "__main__":
    unittest.main()


def test_pytest_default_logger_uses_per_test_directory(
    isolate_default_profit_trade_logger: Path,
) -> None:
    logger = get_profit_trade_event_logger()

    event = logger.emit(
        provider="local",
        component="pytest_isolation",
        operation="verification",
        message="this event must stay in pytest tmp_path",
        trade_no="PT-pytest-isolation",
    )

    assert logger.log_dir == isolate_default_profit_trade_logger.resolve()
    assert logger.log_dir != DEFAULT_PROFIT_TRADE_LOG_DIR.resolve()
    assert event is not None
    assert event["sequence"] == 1
    assert list(isolate_default_profit_trade_logger.glob("*.jsonl"))
