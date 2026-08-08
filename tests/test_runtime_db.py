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
from cs2_assistant.models import CatalogItem


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

    def test_catalog_search_ranks_normal_and_stattrak_together_and_pages_completely(self) -> None:
        items = [
            CatalogItem(
                "★ Flip Knife | Gamma Doppler (Factory New)",
                "折叠刀（★） | 伽玛多普勒 (崭新出厂)",
            ),
            CatalogItem(
                "★ StatTrak™ Flip Knife | Gamma Doppler (Factory New)",
                "折叠刀（★ StatTrak™） | 伽玛多普勒 (崭新出厂)",
            ),
        ]
        items.extend(
            CatalogItem(
                f"★ StatTrak™ Flip Knife | Test Finish {index:03d} (Factory New)",
                f"折叠刀（★ StatTrak™） | 测试涂装 {index:03d} (崭新出厂)",
            )
            for index in range(60)
        )
        self.db.upsert_items(items)

        first_page, total = self.db.search_items_page("折叠刀", limit=20, offset=0)
        first_names = [str(row["market_hash_name"]) for row in first_page]
        self.assertEqual(62, total)
        self.assertIn("★ Flip Knife | Gamma Doppler (Factory New)", first_names)
        self.assertIn("★ StatTrak™ Flip Knife | Gamma Doppler (Factory New)", first_names)

        normal_rows, normal_total = self.db.search_items_page(
            "折叠刀 普通版 伽马多普勒",
            limit=20,
            offset=0,
        )
        self.assertEqual(1, normal_total)
        self.assertEqual(
            ["★ Flip Knife | Gamma Doppler (Factory New)"],
            [str(row["market_hash_name"]) for row in normal_rows],
        )

        second_page, _ = self.db.search_items_page("折叠刀", limit=20, offset=20)
        self.assertFalse(
            {str(row["market_hash_name"]) for row in first_page}
            & {str(row["market_hash_name"]) for row in second_page}
        )

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

    def test_profit_trade_completed_at_is_only_automatic_for_completed_status(self) -> None:
        trade_ids: dict[str, int] = {}
        for status in ("completed", "failed", "manual_required", "cancelled"):
            trade_id = self.db.add_profit_trade(
                trade_no=f"PT-completed-at-{status}",
                market_hash_name="Dreams & Nightmares Case",
            )
            self.db.update_profit_trade(trade_id, status=status)
            trade_ids[status] = trade_id

        self.assertIsNotNone(
            self.db.get_profit_trade(trade_ids["completed"])["completed_at"]
        )
        for status in ("failed", "manual_required", "cancelled"):
            with self.subTest(status=status):
                self.assertIsNone(
                    self.db.get_profit_trade(trade_ids[status])["completed_at"]
                )

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

    def test_scheduled_task_starvation_guard_preserves_p0_and_runs_overdue_scan(self) -> None:
        self.db.upsert_scheduled_task(
            "old-background-scan",
            source="guadao",
            task_type="guadao_scan",
            next_attempt_at=T0,
            priority=3,
        )
        self.db.upsert_scheduled_task(
            "new-trade-retry",
            source="guadao",
            task_type="rebuy_attempt",
            next_attempt_at=T1,
            priority=1,
        )
        self.db.upsert_scheduled_task(
            "new-safety-check",
            source="guadao",
            task_type="terminal_check",
            next_attempt_at=T1,
            priority=0,
        )

        safety = self.db.claim_due_scheduled_tasks(
            "worker-a",
            limit=1,
            now=T2,
            starvation_guard_task_key="old-background-scan",
            starvation_guard_after_seconds=30,
        )
        self.assertEqual(["new-safety-check"], [row["task_key"] for row in safety])
        self.assertTrue(
            self.db.complete_scheduled_task("new-safety-check", "worker-a", now=T2)
        )

        guarded = self.db.claim_due_scheduled_tasks(
            "worker-a",
            limit=1,
            now=T2,
            starvation_guard_task_key="old-background-scan",
            starvation_guard_after_seconds=30,
        )
        self.assertEqual(["old-background-scan"], [row["task_key"] for row in guarded])

    def test_deadline_guard_runs_overdue_steam_sync_before_p1_but_after_p0(self) -> None:
        """An overdue Steam account sync owns the next fair slot, not every slot."""

        self.db.upsert_scheduled_task(
            "p0-maintenance",
            source="guadao",
            task_type="stale_listing_recheck",
            next_attempt_at=T0,
            priority=0,
        )
        self.db.upsert_scheduled_task(
            "steam-sync:overdue",
            source="guadao",
            task_type="steam_account_sync",
            next_attempt_at=T0,
            priority=2,
        )
        for index in range(107):
            self.db.upsert_scheduled_task(
                f"rebuy-{index}",
                source="guadao",
                task_type="rebuy_attempt",
                next_attempt_at=T1,
                priority=1,
            )

        first = self.db.claim_due_scheduled_tasks(
            "worker-a",
            limit=1,
            now=T2,
            deadline_guard_task_type="steam_account_sync",
            deadline_guard_after_seconds=60,
        )
        self.assertEqual(["p0-maintenance"], [row["task_key"] for row in first])
        self.assertTrue(self.db.complete_scheduled_task("p0-maintenance", "worker-a", now=T2))

        second = self.db.claim_due_scheduled_tasks(
            "worker-a",
            limit=1,
            now=T2,
            deadline_guard_task_type="steam_account_sync",
            deadline_guard_after_seconds=60,
        )
        self.assertEqual(["steam-sync:overdue"], [row["task_key"] for row in second])

    def test_deadline_guard_does_not_promote_a_steam_sync_inside_its_60_second_budget(self) -> None:
        self.db.upsert_scheduled_task(
            "steam-sync:not-overdue",
            source="guadao",
            task_type="steam_account_sync",
            next_attempt_at=T1,
            priority=2,
        )
        self.db.upsert_scheduled_task(
            "rebuy-due",
            source="guadao",
            task_type="rebuy_attempt",
            next_attempt_at=T1,
            priority=1,
        )

        claimed = self.db.claim_due_scheduled_tasks(
            "worker-a",
            limit=1,
            now=T2,
            deadline_guard_task_type="steam_account_sync",
            deadline_guard_after_seconds=120,
        )
        self.assertEqual(["rebuy-due"], [row["task_key"] for row in claimed])

    def test_scheduled_task_starvation_guard_waits_for_full_grace_period(self) -> None:
        self.db.upsert_scheduled_task(
            "background-scan",
            source="guadao",
            task_type="guadao_scan",
            next_attempt_at=T0,
            priority=3,
        )
        self.db.upsert_scheduled_task(
            "trade-retry",
            source="guadao",
            task_type="rebuy_attempt",
            next_attempt_at=T1,
            priority=1,
        )

        claimed = self.db.claim_due_scheduled_tasks(
            "worker-a",
            limit=1,
            now=T1,
            starvation_guard_task_key="background-scan",
            starvation_guard_after_seconds=120,
        )
        self.assertEqual(["trade-retry"], [row["task_key"] for row in claimed])

    def test_scheduled_task_starvation_guard_does_not_steal_valid_lease(self) -> None:
        self.db.upsert_scheduled_task(
            "leased-background-scan",
            source="guadao",
            task_type="guadao_scan",
            next_attempt_at=T0,
            priority=3,
        )
        leased = self.db.claim_due_scheduled_tasks(
            "worker-a",
            limit=1,
            lease_seconds=300,
            now=T0,
        )
        self.assertEqual(["leased-background-scan"], [row["task_key"] for row in leased])
        self.db.upsert_scheduled_task(
            "trade-while-scan-leased",
            source="guadao",
            task_type="rebuy_attempt",
            next_attempt_at=T1,
            priority=1,
        )

        claimed = self.db.claim_due_scheduled_tasks(
            "worker-b",
            limit=1,
            now=T2,
            starvation_guard_task_key="leased-background-scan",
            starvation_guard_after_seconds=30,
        )
        self.assertEqual(["trade-while-scan-leased"], [row["task_key"] for row in claimed])
        scan = self.db.get_scheduled_task("leased-background-scan")
        self.assertEqual("running", scan["status"])
        self.assertEqual("worker-a", scan["lease_owner"])
        self.assertEqual(1, scan["attempt_count"])

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
            DROP TABLE guadao_operation_audit_events;
            DROP TABLE guadao_issue_acknowledgements;
            DROP TABLE c5_api_circuits;
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
