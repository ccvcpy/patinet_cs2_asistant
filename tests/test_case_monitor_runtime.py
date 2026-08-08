from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.services.case_monitor_runtime import (
    CaseMonitorBusyError,
    CaseMonitorRuntimeController,
    ensure_case_monitor_runtime_state,
)


class CaseMonitorRuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.temp_dir.name) / "assistant.db")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            ensure_case_monitor_runtime_state(db)
        finally:
            db.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_backend_restart_interrupts_jobs_and_requires_manual_resume(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.upsert_case_monitor_runtime_state(
                enabled=True,
                interval_minutes=15,
                runtime_status="idle",
                next_run_at="2026-07-31T00:00:00+00:00",
                payload={"startedAt": "2026-07-30T23:00:00+00:00"},
            )
            job, busy = db.create_case_monitor_job_if_idle(
                job_type="collect",
                trigger_source="manual",
                parameters={"allCrateTypes": True},
            )
            self.assertIsNotNone(job)
            self.assertIsNone(busy)
            job_id = str(job["job_id"])
        finally:
            db.close()

        controller = CaseMonitorRuntimeController(self.settings, poll_seconds=60)
        controller.start()
        try:
            status = controller.status()
            self.assertFalse(status["runtime"]["enabled"])
            self.assertEqual("paused", status["runtime"]["status"])
            self.assertEqual(15, status["runtime"]["intervalMinutes"])
            self.assertIsNone(status["currentJob"])
        finally:
            controller.stop()

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            persisted = db.get_case_monitor_job(job_id)
            self.assertEqual("interrupted", persisted["status"])
        finally:
            db.close()

    def test_manual_start_validates_interval_and_pause_preserves_selection(self) -> None:
        controller = CaseMonitorRuntimeController(self.settings)
        with self.assertRaisesRegex(ValueError, "5、10、15、30"):
            controller.start_monitor(7)

        started = controller.start_monitor(30)
        self.assertTrue(started["runtime"]["enabled"])
        self.assertEqual(30, started["runtime"]["intervalMinutes"])

        paused = controller.pause_monitor()
        self.assertFalse(paused["runtime"]["enabled"])
        self.assertEqual("paused", paused["runtime"]["status"])
        self.assertEqual(30, paused["runtime"]["intervalMinutes"])

    def test_collect_and_report_share_single_flight_guard(self) -> None:
        controller = CaseMonitorRuntimeController(self.settings)
        collect = controller.request_collect()
        self.assertEqual("collect", collect["jobType"])
        self.assertEqual("queued", collect["status"])

        with self.assertRaises(CaseMonitorBusyError) as caught:
            controller.request_report({"hours": 24})
        self.assertEqual(collect["jobId"], caught.exception.job["jobId"])

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.finish_case_monitor_job(collect["jobId"], result={"savedCount": 0})
            state = db.get_case_monitor_runtime_state()
            db.upsert_case_monitor_runtime_state(
                enabled=False,
                interval_minutes=float(state["interval_minutes"]),
                runtime_status="paused",
                payload={},
            )
        finally:
            db.close()

        report = controller.request_report(
            {"hours": 168, "refreshLiquidity": False}
        )
        self.assertEqual("report", report["jobType"])
        self.assertEqual(168.0, report["parameters"]["hours"])
        self.assertFalse(report["parameters"]["refreshLiquidity"])
        self.assertTrue(report["parameters"]["allCrateTypes"])


if __name__ == "__main__":
    unittest.main()
