from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.services.c5_ip_circuit import (
    C5_IP_CIRCUIT_KEY,
    bind_c5_ip_circuit_telemetry,
    probe_c5_ip_circuit,
)
from cs2_assistant.services.runtime_controller import (
    C5_CIRCUIT_BLOCKED_TASKS,
    RUNTIME_GUADAO,
    TASK_REBUY_ATTEMPT,
    TASK_STEAM_ACCOUNT_SYNC,
    UnifiedRuntimeController,
)
from cs2_assistant.utils import utc_now_iso


class C5IpCircuitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.temp_dir.name) / "assistant.db",
            steamdt_base_path=Path(self.temp_dir.name) / "steamdt.json",
            c5_api_key="test-c5-key",
            serverchan_sendkey="test-sendkey",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _open_db(self) -> Database:
        db = Database(self.settings.db_path)
        db.initialize()
        return db

    @patch("cs2_assistant.services.c5_ip_circuit.ServerChanClient.send", return_value={"code": 0})
    def test_first_499100_opens_shared_circuit_and_alerts_only_once(self, send) -> None:
        downstream_events: list[dict[str, object]] = []
        profit_callback = bind_c5_ip_circuit_telemetry(
            self.settings,
            source="profit_trade",
            downstream=downstream_events.append,
        )
        guadao_callback = bind_c5_ip_circuit_telemetry(
            self.settings,
            source="guadao",
        )

        profit_callback(
            {
                "error_code": 499100,
                "request_ip": "223.65.10.116",
                "operation": "seller_order_list",
                "trade_no": "PT-1",
            }
        )
        guadao_callback(
            {
                "error_code": 499100,
                "request_ip": "223.65.10.116",
                "operation": "price_batch",
                "operation_id": 51,
            }
        )

        self.assertEqual(1, send.call_count)
        self.assertEqual(1, len(downstream_events))
        db = self._open_db()
        try:
            row = db.get_c5_api_circuit(C5_IP_CIRCUIT_KEY)
            self.assertIsNotNone(row)
            self.assertEqual("open", row["state"])
            self.assertEqual(499100, row["error_code"])
            self.assertEqual("223.65.10.116", row["request_ip"])
            self.assertIsNotNone(row["alert_sent_at"])
        finally:
            db.close()

    @patch("cs2_assistant.services.c5_ip_circuit.ServerChanClient.send", return_value={"code": 0})
    @patch("cs2_assistant.services.c5_ip_circuit.C5GameClient.steam_info", return_value={"balance": 1})
    def test_successful_leased_probe_recovers_and_alerts_once(self, _steam_info, send) -> None:
        callback = bind_c5_ip_circuit_telemetry(self.settings, source="guadao")
        callback(
            {
                "error_code": 499100,
                "request_ip": "223.65.10.116",
                "operation": "price_batch",
            }
        )
        db = self._open_db()
        try:
            with db.conn:
                db.conn.execute(
                    "UPDATE c5_api_circuits SET next_probe_at = ? WHERE circuit_key = ?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), C5_IP_CIRCUIT_KEY),
                )
            result = probe_c5_ip_circuit(self.settings, db, worker_id="worker-1")
            repeated = probe_c5_ip_circuit(self.settings, db, worker_id="worker-2")
            row = db.get_c5_api_circuit(C5_IP_CIRCUIT_KEY)
        finally:
            db.close()

        self.assertTrue(result["recovered"])
        self.assertFalse(repeated["probed"])
        self.assertEqual("closed", row["state"])
        self.assertEqual(2, send.call_count)

    @patch.object(UnifiedRuntimeController, "_dispatch_task")
    def test_open_circuit_reschedules_c5_task_without_dispatch(self, dispatch) -> None:
        controller = UnifiedRuntimeController(self.settings)
        db = self._open_db()
        try:
            state = db.get_executor_runtime_state(RUNTIME_GUADAO)
            db.upsert_executor_runtime_state(
                RUNTIME_GUADAO,
                enabled=True,
                runtime_status="running",
                migration_hold=False,
                payload={},
            )
            db.trip_c5_api_circuit(
                error_code=499100,
                request_ip="223.65.10.116",
                trigger_source="guadao",
                trigger_operation="price_batch",
                next_probe_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            db.upsert_scheduled_task(
                "rebuy:1",
                source=RUNTIME_GUADAO,
                task_type=TASK_REBUY_ATTEMPT,
                operation_id=1,
                next_attempt_at=utc_now_iso(),
            )
            task = db.claim_due_scheduled_tasks(controller.worker_id, limit=1)[0]
        finally:
            db.close()

        controller._execute_claimed_task(task, gate={"status": "ready"})
        dispatch.assert_not_called()
        db = self._open_db()
        try:
            waiting = db.get_scheduled_task("rebuy:1")
            self.assertEqual("pending", waiting["status"])
            self.assertEqual("c5_ip_whitelist_circuit_open", waiting["last_error"])
            self.assertFalse(controller._new_actions_enabled(RUNTIME_GUADAO))
        finally:
            db.close()

        self.assertIn(TASK_REBUY_ATTEMPT, C5_CIRCUIT_BLOCKED_TASKS)
        self.assertNotIn(TASK_STEAM_ACCOUNT_SYNC, C5_CIRCUIT_BLOCKED_TASKS)


if __name__ == "__main__":
    unittest.main()
