from __future__ import annotations

import gc
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.services.guadao_audit import (
    cancel_guadao_audit_run as cancel_audit_service,
    create_guadao_audit_run as create_audit_service,
    get_guadao_audit_run,
    initialize_guadao_audit_schema,
)
import cs2_assistant.services.runtime_controller as runtime_module


START_AT = "2026-07-19T15:20:00+08:00"
END_AT = "2026-07-28T23:50:00+08:00"
TRADING_TABLES = (
    "inventory_pool",
    "inventory_assets",
    "pool_operations",
    "profit_trades",
)


class EmptyAccountStore:
    def list_accounts(self) -> list[Any]:
        return []

    def get_account(self, _account_id_or_name: str) -> None:
        return None


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
            ) VALUES ('AUDIT-SENTINEL-POOL', 2, 'holding', 'must-not-change', ?, ?)
            """,
            (timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO inventory_assets (
                asset_id, market_hash_name, steam_id, tradable, status,
                last_seen_at, created_at
            ) VALUES (
                'AUDIT-SENTINEL-ASSET', 'AUDIT-SENTINEL-POOL', 'steam-sentinel',
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
                'AUDIT-SENTINEL-POOL', 'guadao', 'sentinel', 'completed', 1,
                1.23, 1.23, 'AUDIT-SENTINEL-ASSET', 'must-not-change', ?
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
                'AUDIT-SENTINEL-TRADE', 'AUDIT-SENTINEL-POOL', 'completed',
                'completed', 99, 'must-not-change', ?, ?
            )
            """,
            (timestamp, timestamp),
        )
        conn.commit()


class GuadaoAuditRuntimeIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            db_path=root / "assistant.db",
            steamdt_base_path=root / "steamdt.json",
            c5_api_key="audit-test-only-c5-key",
        )
        self.controller = runtime_module.UnifiedRuntimeController(
            self.settings,
            poll_seconds=0.2,
        )
        self.controller.account_store = EmptyAccountStore()
        self.controller._initialize()
        self.controller.confirm_migration()
        initialize_guadao_audit_schema(self.settings)
        self._move_seeded_tasks_out_of_the_way()
        seed_trading_sentinels(self.settings.db_path)

    def tearDown(self) -> None:
        self.controller.stop(timeout=0)
        # sqlite3 connections can participate in short-lived reference cycles
        # on Windows/Python 3.14.  Force finalizers before removing the WAL DB
        # so teardown noise never masks the real contract assertion.
        gc.collect()
        self.temp_dir.cleanup()

    def _open_db(self) -> Database:
        db = Database(self.settings.db_path)
        db.initialize()
        return db

    def _move_seeded_tasks_out_of_the_way(self) -> None:
        db = self._open_db()
        try:
            future = datetime.now(timezone.utc) + timedelta(days=1)
            for row in db.list_scheduled_tasks(limit=10_000):
                db.reschedule_scheduled_task(
                    str(row["task_key"]),
                    next_attempt_at=future,
                )
        finally:
            db.close()

    def _require_controller_method(self, name: str) -> Callable[..., dict[str, Any]]:
        method = getattr(self.controller, name, None)
        self.assertTrue(
            callable(method),
            f"production gap: UnifiedRuntimeController.{name} is required",
        )
        return method

    def _audit_task_type(self) -> str:
        task_type = getattr(runtime_module, "TASK_GUADAO_AUDIT", None)
        self.assertEqual(
            "guadao_audit",
            task_type,
            "production gap: TASK_GUADAO_AUDIT must be the stable guadao_audit task type",
        )
        return str(task_type)

    def _assert_trading_tables_unchanged(
        self,
        before: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.assertEqual(before, snapshot_trading_tables(self.settings.db_path))

    @staticmethod
    def _task_request_id(row: sqlite3.Row) -> str:
        payload = json.loads(str(row["payload_json"] or "{}"))
        return str(payload.get("requestId") or "")

    def _find_audit_task(self, request_id: str) -> sqlite3.Row:
        task_type = self._audit_task_type()
        db = self._open_db()
        try:
            matches = [
                row
                for row in db.list_scheduled_tasks(task_type=task_type, limit=10_000)
                if self._task_request_id(row) == request_id
            ]
        finally:
            db.close()
        self.assertEqual(
            1,
            len(matches),
            f"exactly one persistent audit task must reference {request_id}",
        )
        return matches[0]

    def _create_pending_run(self) -> dict[str, Any]:
        return create_audit_service(
            self.settings,
            start_at=START_AT,
            end_at=END_AT,
            account_ids=["steam-1"],
            expected_account_count=1,
            reported_comprehensive_ratio="0.700000",
        )

    def _forbid_non_audit_dispatch(self, stack: ExitStack) -> dict[str, Mock]:
        forbidden: dict[str, Mock] = {}
        for name in (
            "ExecutionEngine",
            "run_profit_trade_once",
            "execute_manual_profit_trade_request",
            "refresh_profit_trade_selection_watch",
            "run_c5_research_scan_chunk",
        ):
            mock = Mock(side_effect=AssertionError(f"audit dispatch called forbidden path: {name}"))
            stack.enter_context(patch.object(runtime_module, name, mock))
            forbidden[name] = mock
        return forbidden

    def test_queue_persists_p3_task_with_pollable_request_id(self) -> None:
        task_type = self._audit_task_type()
        queue = self._require_controller_method("queue_guadao_audit_run")
        before = snapshot_trading_tables(self.settings.db_path)

        result = queue(
            {
                "startAt": START_AT,
                "endAt": END_AT,
                "accountIds": ["steam-1"],
                "expectedAccountCount": 1,
                "reportedComprehensiveRatio": "0.700000",
            }
        )

        request_id = str(result.get("requestId") or "")
        self.assertTrue(request_id.startswith("GDA-"), result)
        self.assertEqual("pending", result.get("status"))
        self.assertEqual(request_id, get_guadao_audit_run(self.settings, request_id)["requestId"])
        task = self._find_audit_task(request_id)
        self.assertEqual(task_type, task["task_type"])
        self.assertEqual(3, int(task["priority"]))
        self.assertEqual("pending", task["status"])
        self.assertEqual(f"guadao-audit:{request_id}", task["task_key"])
        self._assert_trading_tables_unchanged(before)

    def test_dispatch_calls_only_guadao_audit_service(self) -> None:
        task_type = self._audit_task_type()
        request_id = str(self._create_pending_run()["requestId"])
        task = {
            "task_key": f"guadao-audit:{request_id}",
            "source": runtime_module.RUNTIME_GUADAO,
            "task_type": task_type,
            "account_id": None,
            "operation_id": None,
            "payload_json": json.dumps({"requestId": request_id}),
            "priority": 3,
        }
        before = snapshot_trading_tables(self.settings.db_path)
        audit_call = Mock(
            return_value={
                "ok": True,
                "requestId": request_id,
                "status": "passed",
            }
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(runtime_module, "run_guadao_audit", audit_call, create=True)
            )
            forbidden = self._forbid_non_audit_dispatch(stack)
            result = self.controller._dispatch_task(task, enabled=False)

        self.assertEqual("passed", result["status"])
        self.assertEqual(1, audit_call.call_count)
        args, _kwargs = audit_call.call_args
        self.assertIs(self.settings, args[0])
        self.assertEqual(request_id, args[1])
        for name, mock in forbidden.items():
            self.assertFalse(mock.called, f"forbidden path was called: {name}")
        self._assert_trading_tables_unchanged(before)

    def test_dispatch_renews_lease_and_maps_all_domain_terminal_states(self) -> None:
        task_type = self._audit_task_type()
        # The queue records whether the read-only task itself ran.  A completed
        # audit can legitimately conclude failed (a verified discrepancy) or
        # inconclusive (missing official evidence); those are business
        # conclusions in guadao_audit_runs, not scheduler failures.
        expected_task_status = {
            "passed": "completed",
            "failed": "completed",
            "inconclusive": "completed",
            "cancelled": "cancelled",
        }
        observed_task_status: dict[str, str] = {}
        observed_run_status: dict[str, str] = {}

        for domain_status in expected_task_status:
            with self.subTest(domain_status=domain_status):
                run = self._create_pending_run()
                request_id = str(run["requestId"])
                task_key = f"guadao-audit:{request_id}"
                db = self._open_db()
                try:
                    db.upsert_scheduled_task(
                        task_key,
                        source=runtime_module.RUNTIME_GUADAO,
                        task_type=task_type,
                        next_attempt_at=datetime.now(timezone.utc),
                        payload={"requestId": request_id},
                        status="pending",
                        priority=3,
                    )
                    claimed = db.claim_due_scheduled_tasks(
                        self.controller.worker_id,
                        limit=1,
                        lease_seconds=0.01,
                        source=runtime_module.RUNTIME_GUADAO,
                    )
                finally:
                    db.close()
                self.assertEqual([task_key], [row["task_key"] for row in claimed])
                claimed_task = claimed[0]
                before = snapshot_trading_tables(self.settings.db_path)
                service_started = threading.Event()
                release_service = threading.Event()
                worker_errors: list[BaseException] = []

                def audit_service(
                    settings: Settings,
                    observed_request_id: str,
                    **_kwargs: Any,
                ) -> dict[str, Any]:
                    self.assertIs(self.settings, settings)
                    self.assertEqual(request_id, observed_request_id)
                    service_started.set()
                    if not release_service.wait(timeout=3):
                        raise TimeoutError("test did not release the audit service")
                    now = datetime.now(timezone.utc).isoformat()
                    with sqlite3.connect(self.settings.db_path) as conn:
                        conn.execute(
                            """
                            UPDATE guadao_audit_runs
                            SET status = ?, stage = 'finished', cancel_requested = ?,
                                finished_at = ?, updated_at = ?
                            WHERE request_id = ?
                            """,
                            (
                                domain_status,
                                1 if domain_status == "cancelled" else 0,
                                now,
                                now,
                                request_id,
                            ),
                        )
                        conn.commit()
                    return {
                        "ok": True,
                        "requestId": request_id,
                        "status": domain_status,
                    }

                audit_call = Mock(side_effect=audit_service)

                def execute() -> None:
                    try:
                        self.controller._execute_claimed_task(
                            claimed_task,
                            gate={
                                "status": "ready",
                                "validCount": 0,
                                "totalCount": 0,
                            },
                        )
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        worker_errors.append(exc)

                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            runtime_module,
                            "run_guadao_audit",
                            audit_call,
                            create=True,
                        )
                    )
                    forbidden = self._forbid_non_audit_dispatch(stack)
                    worker = threading.Thread(target=execute, daemon=True)
                    worker.start()
                    started = service_started.wait(timeout=2)
                    if not started:
                        release_service.set()
                        worker.join(timeout=2)
                        self.fail("audit dispatch never reached run_guadao_audit")

                    # The originally claimed 10 ms lease is deliberately too
                    # short.  A safe long-running audit must refresh it before
                    # entering the service, so another worker looking one
                    # second ahead still cannot reclaim the in-flight run.
                    competing_db = self._open_db()
                    try:
                        duplicate_claim = competing_db.claim_due_scheduled_tasks(
                            "competing-audit-worker",
                            limit=1,
                            source=runtime_module.RUNTIME_GUADAO,
                            now=datetime.now(timezone.utc) + timedelta(seconds=1),
                        )
                    finally:
                        competing_db.close()
                    self.assertEqual(
                        [],
                        duplicate_claim,
                        "in-flight audit lease expired and was reclaimed by a second worker",
                    )

                    release_service.set()
                    worker.join(timeout=3)

                self.assertFalse(worker.is_alive(), "audit worker did not terminate")
                self.assertEqual([], worker_errors)
                self.assertEqual(1, audit_call.call_count)
                for name, mock in forbidden.items():
                    self.assertFalse(mock.called, f"forbidden path was called: {name}")

                db = self._open_db()
                try:
                    persisted_task = db.get_scheduled_task(task_key)
                finally:
                    db.close()
                self.assertIsNotNone(persisted_task)
                observed_task_status[domain_status] = str(persisted_task["status"])
                self.assertIsNone(persisted_task["lease_owner"])
                self.assertIsNone(persisted_task["lease_expires_at"])
                persisted_run = get_guadao_audit_run(self.settings, request_id)
                self.assertIsNotNone(persisted_run)
                observed_run_status[domain_status] = str(persisted_run["status"])
                self._assert_trading_tables_unchanged(before)

        task_status_mismatches = {
            status: {
                "expected": expected_task_status[status],
                "actual": observed_task_status.get(status),
            }
            for status in expected_task_status
            if observed_task_status.get(status) != expected_task_status[status]
        }
        self.assertEqual(
            {},
            task_status_mismatches,
            "an audit's business conclusion must not turn a successfully run read-only task into a scheduler failure",
        )
        self.assertEqual(
            list(expected_task_status),
            list(observed_run_status),
        )
        self.assertEqual(
            observed_run_status,
            {status: status for status in expected_task_status},
        )

    def test_retry_creates_new_request_id_and_preserves_old_run(self) -> None:
        task_type = self._audit_task_type()
        retry = self._require_controller_method("retry_guadao_audit_run")
        old_run = self._create_pending_run()
        old_request_id = str(old_run["requestId"])
        cancel_audit_service(self.settings, old_request_id)
        old_before = get_guadao_audit_run(self.settings, old_request_id)
        before = snapshot_trading_tables(self.settings.db_path)

        retried = retry(old_request_id)

        new_request_id = str(retried.get("requestId") or "")
        self.assertTrue(new_request_id.startswith("GDA-"), retried)
        self.assertNotEqual(old_request_id, new_request_id)
        self.assertEqual(old_request_id, retried.get("retryOfRequestId"))
        self.assertEqual("pending", retried.get("status"))
        self.assertEqual(old_before, get_guadao_audit_run(self.settings, old_request_id))
        persisted_new = get_guadao_audit_run(self.settings, new_request_id)
        self.assertIsNotNone(persisted_new)
        self.assertEqual(old_request_id, persisted_new["retryOfRequestId"])
        task = self._find_audit_task(new_request_id)
        self.assertEqual(task_type, task["task_type"])
        self.assertEqual(3, int(task["priority"]))
        self._assert_trading_tables_unchanged(before)

    def test_cancel_is_read_only_and_terminates_only_the_audit_task(self) -> None:
        task_type = self._audit_task_type()
        cancel = self._require_controller_method("cancel_guadao_audit_run")
        run = self._create_pending_run()
        request_id = str(run["requestId"])
        task_key = f"guadao-audit:{request_id}"
        db = self._open_db()
        try:
            db.upsert_scheduled_task(
                task_key,
                source=runtime_module.RUNTIME_GUADAO,
                task_type=task_type,
                next_attempt_at=datetime.now(timezone.utc),
                payload={"requestId": request_id},
                status="pending",
                priority=3,
            )
        finally:
            db.close()
        before = snapshot_trading_tables(self.settings.db_path)

        run_audit = Mock(side_effect=AssertionError("cancel must not execute an audit"))
        engine = Mock(side_effect=AssertionError("cancel must not instantiate ExecutionEngine"))
        with (
            patch.object(runtime_module, "run_guadao_audit", run_audit, create=True),
            patch.object(runtime_module, "ExecutionEngine", engine),
        ):
            cancelled = cancel(request_id)

        self.assertEqual(request_id, cancelled.get("requestId"))
        self.assertEqual("cancelled", cancelled.get("status"))
        self.assertTrue(cancelled.get("cancelRequested"))
        self.assertFalse(run_audit.called)
        self.assertFalse(engine.called)
        db = self._open_db()
        try:
            persisted_task = db.get_scheduled_task(task_key)
        finally:
            db.close()
        self.assertIsNotNone(persisted_task)
        self.assertEqual("cancelled", persisted_task["status"])
        self._assert_trading_tables_unchanged(before)

    def test_invalid_queue_input_creates_neither_run_nor_task(self) -> None:
        task_type = self._audit_task_type()
        queue = self._require_controller_method("queue_guadao_audit_run")
        before = snapshot_trading_tables(self.settings.db_path)
        with sqlite3.connect(self.settings.db_path) as conn:
            run_count_before = int(
                conn.execute("SELECT COUNT(*) FROM guadao_audit_runs").fetchone()[0]
            )

        with self.assertRaises((TypeError, ValueError)):
            queue(
                {
                    "startAt": END_AT,
                    "endAt": START_AT,
                    "accountIds": ["steam-1"],
                    "expectedAccountCount": 1,
                }
            )

        with sqlite3.connect(self.settings.db_path) as conn:
            run_count_after = int(
                conn.execute("SELECT COUNT(*) FROM guadao_audit_runs").fetchone()[0]
            )
        db = self._open_db()
        try:
            audit_tasks = db.list_scheduled_tasks(task_type=task_type, limit=10_000)
        finally:
            db.close()
        self.assertEqual(run_count_before, run_count_after)
        self.assertEqual([], audit_tasks)
        self._assert_trading_tables_unchanged(before)


if __name__ == "__main__":
    unittest.main()
