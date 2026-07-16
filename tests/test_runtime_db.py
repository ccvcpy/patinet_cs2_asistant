from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.db import Database, RUNTIME_COORDINATION_TABLES


T0 = "2026-07-16T00:00:00+00:00"
T1 = "2026-07-16T00:01:00+00:00"
T2 = "2026-07-16T00:02:00+00:00"


class RuntimeDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "assistant.db"
        self.db = Database(self.db_path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_runtime_tables_defaults_wal_and_memory_compatibility(self) -> None:
        tables = {
            str(row[0])
            for row in self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertTrue(RUNTIME_COORDINATION_TABLES.issubset(tables))
        states = {
            str(row["executor_key"]): row
            for row in self.db.list_executor_runtime_states()
        }
        self.assertEqual({"guadao", "profit_trade"}, set(states))
        self.assertEqual(0, states["guadao"]["enabled"])
        self.assertEqual("stopped", states["guadao"]["runtime_status"])
        self.assertEqual(1, states["guadao"]["migration_hold"])
        self.assertEqual("wal", self.db.conn.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(5000, self.db.conn.execute("PRAGMA busy_timeout").fetchone()[0])

        memory_db = Database(Path(":memory:"))
        try:
            memory_db.initialize()
            self.assertEqual(
                "memory",
                memory_db.conn.execute("PRAGMA journal_mode").fetchone()[0],
            )
            self.assertIsNotNone(memory_db.get_executor_runtime_state("profit_trade"))
        finally:
            memory_db.close()

    def test_runtime_state_upsert_is_idempotent(self) -> None:
        original = self.db.get_executor_runtime_state("guadao")
        row = self.db.upsert_executor_runtime_state(
            "guadao",
            enabled=True,
            runtime_status="preparing",
            migration_hold=True,
            gate_reason="cookie_gate",
            heartbeat_at=T0,
            payload={"validAccounts": 3},
        )
        self.assertEqual(1, row["enabled"])
        self.assertEqual("preparing", row["runtime_status"])
        self.assertEqual("cookie_gate", row["gate_reason"])
        self.assertEqual({"validAccounts": 3}, json.loads(row["payload_json"]))
        self.assertEqual(original["created_at"], row["created_at"])

        updated = self.db.upsert_executor_runtime_state(
            "guadao",
            enabled=True,
            runtime_status="running",
            migration_hold=False,
            gate_reason=None,
            payload={"validAccounts": 5},
        )
        self.assertEqual("running", updated["runtime_status"])
        self.assertEqual(0, updated["migration_hold"])
        self.assertIsNone(updated["gate_reason"])
        self.assertEqual(original["created_at"], updated["created_at"])

    def test_scheduled_task_claim_priority_lease_reschedule_and_completion(self) -> None:
        self.db.upsert_scheduled_task(
            "later-high",
            source="guadao",
            task_type="steam_sync",
            next_attempt_at=T1,
            priority=0,
        )
        self.db.upsert_scheduled_task(
            "due-low",
            source="guadao",
            task_type="scan",
            next_attempt_at=T0,
            priority=3,
            payload={"full": True},
        )
        self.db.upsert_scheduled_task(
            "due-high",
            source="profit_trade",
            task_type="settlement",
            next_attempt_at=T0,
            priority=0,
        )

        claimed = self.db.claim_due_scheduled_tasks(
            "worker-a",
            limit=2,
            lease_seconds=30,
            now=T0,
        )
        self.assertEqual(["due-high", "due-low"], [row["task_key"] for row in claimed])
        self.assertTrue(all(row["attempt_count"] == 1 for row in claimed))
        ensured = self.db.ensure_scheduled_task(
            "due-high",
            source="changed",
            task_type="changed",
            next_attempt_at=T2,
        )
        self.assertEqual("running", ensured["status"])
        self.assertEqual("worker-a", ensured["lease_owner"])
        self.assertEqual("profit_trade", ensured["source"])
        protected = self.db.upsert_scheduled_task(
            "due-high",
            source="profit_trade",
            task_type="settlement",
            next_attempt_at=T2,
            status="pending",
        )
        self.assertEqual("running", protected["status"])
        self.assertEqual("worker-a", protected["lease_owner"])
        self.assertEqual(T0, protected["next_attempt_at"])
        self.assertTrue(
            self.db.renew_scheduled_task_lease(
                "due-high", "worker-a", lease_seconds=60, now=T0
            )
        )
        self.assertFalse(
            self.db.complete_scheduled_task("due-high", "wrong-worker", now=T1)
        )
        self.assertTrue(
            self.db.complete_scheduled_task("due-high", "worker-a", now=T1)
        )
        self.assertTrue(
            self.db.reschedule_scheduled_task(
                "due-low",
                worker_id="worker-a",
                next_attempt_at=T2,
                error="temporary",
            )
        )
        row = self.db.get_scheduled_task("due-low")
        self.assertEqual("pending", row["status"])
        self.assertEqual(T2, row["next_attempt_at"])
        self.assertIsNone(row["lease_owner"])
        self.assertEqual("temporary", row["last_error"])

        replacement = self.db.upsert_scheduled_task(
            "due-low",
            source="guadao",
            task_type="scan",
            next_attempt_at=T1,
            priority=1,
            payload={"full": False},
        )
        self.assertEqual(1, replacement["attempt_count"])
        self.assertEqual({"full": False}, json.loads(replacement["payload_json"]))

    def test_scheduled_task_expired_lease_is_reclaimed_across_connections(self) -> None:
        self.db.upsert_scheduled_task(
            "lease-recovery",
            source="guadao",
            task_type="rebuy_attempt",
            next_attempt_at=T0,
        )
        first = self.db.claim_due_scheduled_tasks(
            "worker-a", lease_seconds=30, now=T0
        )
        self.assertEqual(1, len(first))

        other = Database(self.db_path)
        try:
            other.initialize()
            blocked = other.claim_due_scheduled_tasks(
                "worker-b", lease_seconds=30, now="2026-07-16T00:00:20+00:00"
            )
            self.assertEqual([], blocked)
            reclaimed = other.claim_due_scheduled_tasks(
                "worker-b", lease_seconds=30, now=T1
            )
            self.assertEqual("lease-recovery", reclaimed[0]["task_key"])
            self.assertEqual(2, reclaimed[0]["attempt_count"])
        finally:
            other.close()

    def test_cookie_health_persists_batch_failures_and_due_retries(self) -> None:
        self.db.upsert_steam_cookie_health(
            "account-1",
            account_name="vnuzl692",
            steam_id="7656",
            status="invalid",
            batch_id="batch-1",
            failure_count=2,
            last_error="401",
            next_retry_at=T1,
            retry_after_seconds=60,
            payload={"reason": "authentication"},
        )
        self.db.upsert_steam_cookie_health(
            "account-2",
            status="valid",
            batch_id="batch-1",
            last_validated_at=T0,
        )
        self.assertEqual([], self.db.list_due_steam_cookie_retries(now=T0))
        due = self.db.list_due_steam_cookie_retries(now=T1)
        self.assertEqual(["account-1"], [row["account_id"] for row in due])
        health = self.db.get_steam_cookie_health("account-1")
        self.assertEqual(2, health["failure_count"])
        self.assertEqual({"reason": "authentication"}, json.loads(health["payload_json"]))

    def test_steam_queue_specific_claim_enforces_global_single_channel(self) -> None:
        self.db.enqueue_steam_request(
            "p3-first",
            source="notify",
            route="market/orderbook",
            priority=3,
            available_at=T0,
        )
        self.db.enqueue_steam_request(
            "p0-second",
            source="guadao",
            route="market/removelisting",
            priority=0,
            available_at=T0,
            account_id="account-1",
            payload={"safe": True},
        )
        duplicate = self.db.enqueue_steam_request(
            "p0-second",
            source="changed-source",
            route="changed-route",
            priority=3,
        )
        self.assertEqual("guadao", duplicate["source"])
        self.assertEqual("market/removelisting", duplicate["route"])

        self.assertIsNone(
            self.db.claim_steam_request(
                "p3-first", "worker-low", lease_seconds=30, now=T0
            )
        )
        claimed = self.db.claim_steam_request(
            "p0-second", "worker-high", lease_seconds=30, now=T0
        )
        self.assertEqual("p0-second", claimed["request_id"])
        self.assertEqual(1, claimed["attempt_count"])
        self.assertIsNone(
            self.db.claim_steam_request(
                "p3-first", "worker-low", lease_seconds=30, now=T0
            )
        )
        self.assertIsNone(self.db.claim_next_steam_request("another", now=T0))
        self.assertTrue(
            self.db.renew_steam_request_lease(
                "p0-second", "worker-high", lease_seconds=60, now=T0
            )
        )
        completed = self.db.complete_steam_request(
            "p0-second",
            "worker-high",
            status="failed",
            http_status=429,
            error="Too Many Requests",
            now=T1,
        )
        self.assertEqual(429, completed["http_status"])

        next_row = self.db.claim_next_steam_request("worker-low", now=T1)
        self.assertEqual("p3-first", next_row["request_id"])
        snapshot = self.db.get_steam_queue_snapshot()
        self.assertEqual(1, snapshot["counts"]["failed"])
        self.assertEqual(1, snapshot["counts"]["running"])
        events = self.db.list_recent_steam_429_events(T0)
        self.assertEqual(["p0-second"], [row["request_id"] for row in events])

    def test_steam_queue_expired_lease_can_be_recovered(self) -> None:
        self.db.enqueue_steam_request(
            "expired", source="cli", route="market/root", priority=1, available_at=T0
        )
        self.assertIsNotNone(
            self.db.claim_steam_request("expired", "worker-a", lease_seconds=10, now=T0)
        )
        other = Database(self.db_path)
        try:
            other.initialize()
            self.assertIsNone(
                other.claim_steam_request(
                    "expired",
                    "worker-b",
                    lease_seconds=10,
                    now="2026-07-16T00:00:05+00:00",
                )
            )
            reclaimed = other.claim_steam_request(
                "expired",
                "worker-b",
                lease_seconds=10,
                now="2026-07-16T00:00:11+00:00",
            )
            self.assertEqual("worker-b", reclaimed["lease_owner"])
            self.assertEqual(2, reclaimed["attempt_count"])
        finally:
            other.close()

    def test_route_circuit_probe_has_a_single_lease(self) -> None:
        circuit = self.db.upsert_steam_route_circuit(
            "account-1:market/listings",
            scope="account_route",
            account_id="account-1",
            route="market/listings",
            state="open",
            consecutive_429=3,
            first_429_at=T0,
            last_429_at=T0,
            cooldown_until=T1,
            next_probe_at=T1,
            reason="429",
            payload={"retryAfter": None},
        )
        self.assertEqual("open", circuit["state"])
        self.assertIsNone(
            self.db.claim_steam_circuit_probe(
                circuit["circuit_key"], "worker-a", now=T0
            )
        )
        claimed = self.db.claim_steam_circuit_probe(
            circuit["circuit_key"], "worker-a", now=T1
        )
        self.assertEqual("half_open", claimed["state"])
        self.assertEqual("worker-a", claimed["probe_lease_owner"])

        other = Database(self.db_path)
        try:
            other.initialize()
            self.assertIsNone(
                other.claim_steam_circuit_probe(
                    circuit["circuit_key"], "worker-b", now=T1
                )
            )
        finally:
            other.close()
        self.assertTrue(
            self.db.release_steam_circuit_probe(
                circuit["circuit_key"], "worker-a", state="closed"
            )
        )
        self.assertEqual(
            "closed", self.db.get_steam_route_circuit(circuit["circuit_key"])["state"]
        )

    def test_issue_acknowledgement_and_strategy_audit_are_append_only(self) -> None:
        ack = self.db.set_guadao_issue_acknowledgement(
            "operation:42",
            acknowledged=True,
            reason="reviewed",
            actor="operator",
            payload={"operationId": 42},
        )
        self.assertEqual(1, ack["acknowledged"])
        restored = self.db.set_guadao_issue_acknowledgement(
            "operation:42",
            acknowledged=False,
            reason="restore",
            actor="operator",
        )
        self.assertEqual(0, restored["acknowledged"])
        self.assertIsNotNone(restored["restored_at"])
        self.assertEqual(
            1, len(self.db.list_guadao_issue_acknowledgements(acknowledged=False))
        )

        first_id = self.db.add_strategy_config_audit(
            config_scope="guadaoBalance",
            old_value={"max": 0.69},
            new_value={"max": 0.72},
            diff={"max": [0.69, 0.72]},
            actor="operator",
            created_at=T0,
        )
        second_id = self.db.add_strategy_config_audit(
            config_scope="guadaoBalance",
            old_value={"max": 0.72},
            new_value={"max": 0.73},
            created_at=T1,
        )
        self.assertGreater(second_id, first_id)
        audits = self.db.list_strategy_config_audit(config_scope="guadaoBalance")
        self.assertEqual([second_id, first_id], [row["id"] for row in audits])
        self.assertEqual({"max": [0.69, 0.72]}, json.loads(audits[1]["diff_json"]))

    def test_runtime_upgrade_backup_is_created_once(self) -> None:
        self.db.conn.executescript(
            """
            DROP TABLE strategy_config_audit;
            DROP TABLE guadao_issue_acknowledgements;
            DROP TABLE steam_route_circuits;
            DROP TABLE steam_request_queue;
            DROP TABLE steam_cookie_health;
            DROP TABLE scheduled_tasks;
            DROP TABLE executor_runtime_state;
            """
        )
        self.db.conn.commit()
        self.db.close()

        upgraded = Database(self.db_path)
        try:
            upgraded.initialize()
            upgraded.initialize()
        finally:
            upgraded.close()
        backups = list(
            self.db_path.parent.glob(
                f"{self.db_path.stem}.pre-runtime-coordination-*{self.db_path.suffix}"
            )
        )
        self.assertEqual(1, len(backups))
        backup = sqlite3.connect(backups[0])
        try:
            backup_tables = {
                str(row[0])
                for row in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            backup.close()
        self.assertFalse(RUNTIME_COORDINATION_TABLES.intersection(backup_tables))

        # tearDown must not close the already closed original connection twice.
        self.db = Database(self.db_path)


if __name__ == "__main__":
    unittest.main()
