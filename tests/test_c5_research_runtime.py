from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.services.runtime_controller import (
    RUNTIME_PROFIT_TRADE,
    TASK_C5_RESEARCH_SCAN,
    UnifiedRuntimeController,
)
from cs2_assistant.utils import utc_now_iso


class C5ResearchRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(
            db_path=Path(self.temporary.name) / "assistant.db",
        )
        self.controller = UnifiedRuntimeController(self.settings, poll_seconds=0.2)
        self.addCleanup(self.controller.stop, timeout=0)

    def _open_db(self) -> Database:
        db = Database(self.settings.db_path)
        db.initialize()
        return db

    def _claim_research_task(self, request_id: str):
        task_key = f"c5-research:{request_id}"
        db = self._open_db()
        try:
            db.upsert_scheduled_task(
                task_key,
                source=RUNTIME_PROFIT_TRADE,
                task_type=TASK_C5_RESEARCH_SCAN,
                next_attempt_at=utc_now_iso(),
                payload={"requestId": request_id},
                status="pending",
                priority=3,
            )
            claimed = db.claim_due_scheduled_tasks(
                self.controller.worker_id,
                source=RUNTIME_PROFIT_TRADE,
                limit=20,
            )
            return next(row for row in claimed if str(row["task_key"]) == task_key)
        finally:
            db.close()

    def test_queue_persists_a_p3_research_task_with_the_request_id(self) -> None:
        filters = {
            "categoryIds": ["crates"],
            "rarityIds": ["rarity_common"],
        }
        created = {
            "ok": True,
            "requestId": "C5RS-runtime-queue",
            "status": "queued",
            "researchOnly": True,
            "canExecute": False,
        }

        with (
            patch(
                "cs2_assistant.services.runtime_controller.create_c5_research_scan",
                return_value=created,
            ) as create_scan,
            patch.object(self.controller, "wake") as wake,
        ):
            result = self.controller.queue_c5_research_scan(filters)

        self.assertEqual(created, result)
        self.assertTrue(result["researchOnly"])
        self.assertFalse(result["canExecute"])
        create_scan.assert_called_once_with(self.settings, filters)
        wake.assert_called_once_with()

        db = self._open_db()
        try:
            task = db.get_scheduled_task("c5-research:C5RS-runtime-queue")
            self.assertIsNotNone(task)
            self.assertEqual(RUNTIME_PROFIT_TRADE, task["source"])
            self.assertEqual(TASK_C5_RESEARCH_SCAN, task["task_type"])
            self.assertEqual("pending", task["status"])
            self.assertEqual(3, int(task["priority"]))
            self.assertEqual(
                {"requestId": "C5RS-runtime-queue"},
                json.loads(str(task["payload_json"])),
            )
            self.assertLessEqual(
                datetime.fromisoformat(str(task["next_attempt_at"])),
                datetime.now(timezone.utc) + timedelta(seconds=2),
            )
        finally:
            db.close()

    def test_dispatch_calls_the_bounded_research_chunk_runner(self) -> None:
        expected = {
            "ok": True,
            "requestId": "C5RS-runtime-dispatch",
            "status": "running",
            "researchOnly": True,
            "canExecute": False,
        }
        task = {
            "task_key": "c5-research:C5RS-runtime-dispatch",
            "task_type": TASK_C5_RESEARCH_SCAN,
            "source": RUNTIME_PROFIT_TRADE,
            "account_id": None,
            "operation_id": None,
            "payload_json": json.dumps(
                {"requestId": "C5RS-runtime-dispatch"},
                ensure_ascii=False,
            ),
        }

        with patch(
            "cs2_assistant.services.runtime_controller.run_c5_research_scan_chunk",
            return_value=expected,
        ) as run_chunk:
            result = self.controller._dispatch_task(task, enabled=False)

        self.assertEqual(expected, result)
        run_chunk.assert_called_once_with(
            self.settings,
            "C5RS-runtime-dispatch",
        )

    def test_terminal_result_completes_the_scheduled_task(self) -> None:
        task = self._claim_research_task("C5RS-runtime-terminal")

        self.controller._reschedule_after_task(
            task,
            result={
                "ok": True,
                "requestId": "C5RS-runtime-terminal",
                "status": "completed_with_errors",
                "researchOnly": True,
                "canExecute": False,
            },
        )

        db = self._open_db()
        try:
            persisted = db.get_scheduled_task(
                "c5-research:C5RS-runtime-terminal"
            )
            self.assertEqual("completed", persisted["status"])
            self.assertIsNotNone(persisted["completed_at"])
            self.assertIsNone(persisted["lease_owner"])
            self.assertIsNone(persisted["lease_expires_at"])
        finally:
            db.close()

    def test_retry_result_uses_its_persisted_next_attempt_time(self) -> None:
        task = self._claim_research_task("C5RS-runtime-retry")
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=7)).replace(
            microsecond=0
        )

        self.controller._reschedule_after_task(
            task,
            result={
                "ok": True,
                "requestId": "C5RS-runtime-retry",
                "status": "retry",
                "nextAttemptAt": retry_at.isoformat(),
                "researchOnly": True,
                "canExecute": False,
            },
        )

        db = self._open_db()
        try:
            persisted = db.get_scheduled_task("c5-research:C5RS-runtime-retry")
            self.assertEqual("pending", persisted["status"])
            self.assertEqual(
                retry_at,
                datetime.fromisoformat(str(persisted["next_attempt_at"])),
            )
            self.assertIsNone(persisted["completed_at"])
            self.assertIsNone(persisted["lease_owner"])
            self.assertIsNone(persisted["lease_expires_at"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
