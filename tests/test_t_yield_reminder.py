from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.reminders.t_yield import (
    INVENTORY_SCOPE_ALL,
    INVENTORY_SCOPE_ALL_COOLDOWN,
    INVENTORY_SCOPE_NOT_ALL_COOLDOWN,
    TYieldReminderConfig,
    TYieldReminderState,
    evaluate_reminder,
)
from cs2_assistant.services.t_yield_scan import (
    INVENTORY_FILTER_COOLDOWN_ONLY,
    INVENTORY_FILTER_MIXED_ONLY,
    INVENTORY_FILTER_TRADABLE_ONLY,
    TYieldAccountRef,
    TYieldCandidate,
    TYieldScanReport,
)


def _counts_for_status(status: str) -> tuple[int, int]:
    if status == INVENTORY_FILTER_TRADABLE_ONLY:
        return 1, 1
    if status == INVENTORY_FILTER_COOLDOWN_ONLY:
        return 1, 0
    if status == INVENTORY_FILTER_MIXED_ONLY:
        return 2, 1
    raise ValueError(f"Unsupported status: {status}")


def _candidate(
    *,
    name: str,
    pct: float,
    status: str,
) -> TYieldCandidate:
    inventory_count, tradable_count = _counts_for_status(status)
    return TYieldCandidate(
        name=name,
        market_hash_name=name,
        inventory_count=inventory_count,
        tradable_count=tradable_count,
        inventory_status=status,
        steam_accounts=[TYieldAccountRef(steam_id="7656119", nickname="115")],
        c5_lowest_sell_price=136.9,
        c5_price_source="inventory_price",
        steam_lowest_sell_price=166.78,
        steam_price_source="csqaq_batch",
        ratio=0.9446,
        t_yield_rate=pct / 100,
    )


def _report(
    *candidates: TYieldCandidate,
    generated_at: str = "2026-04-01T07:30:00+00:00",
) -> TYieldScanReport:
    return TYieldScanReport(
        generated_at=generated_at,
        inventory_source="live",
        inventory_cached_at=None,
        inventory_filter="all",
        accounts=[TYieldAccountRef(steam_id="7656119", nickname="115")],
        inventory_type_total_count=len(candidates),
        inventory_type_count=len(candidates),
        candidates=list(candidates),
        missing_steam_prices=[],
        missing_steam_price_path="data/c5_t_yield_missing_steam_prices.json",
    )


class TYieldReminderTestCase(unittest.TestCase):
    def test_hot_candidate_triggers_notification(self) -> None:
        config = TYieldReminderConfig()
        state = TYieldReminderState()
        decision = evaluate_reminder(
            _report(_candidate(name="Tradable Knife", pct=10.5, status=INVENTORY_FILTER_TRADABLE_ONLY)),
            config,
            state,
            now=datetime(2026, 4, 1, 14, 0),
        )
        self.assertTrue(decision.should_notify)
        self.assertEqual("hot", decision.reason)
        self.assertIsNotNone(decision.notification)
        assert decision.notification is not None
        self.assertIn("账号 115", decision.notification.body)

    def test_duplicate_hot_candidate_does_not_notify_again(self) -> None:
        config = TYieldReminderConfig()
        state = TYieldReminderState()
        report = _report(_candidate(name="Tradable Knife", pct=10.5, status=INVENTORY_FILTER_TRADABLE_ONLY))
        first = evaluate_reminder(report, config, state, now=datetime(2026, 4, 1, 14, 0))
        second = evaluate_reminder(report, config, state, now=datetime(2026, 4, 1, 14, 15))
        self.assertTrue(first.should_notify)
        self.assertFalse(second.should_notify)
        self.assertEqual("hot_duplicate", second.reason)

    def test_exists_tradable_scope_excludes_all_cooldown(self) -> None:
        config = TYieldReminderConfig(inventory_scope=INVENTORY_SCOPE_NOT_ALL_COOLDOWN)
        state = TYieldReminderState()

        tradable = evaluate_reminder(
            _report(_candidate(name="Tradable Knife", pct=10.5, status=INVENTORY_FILTER_TRADABLE_ONLY)),
            config,
            state,
            now=datetime(2026, 4, 1, 14, 0),
        )
        self.assertTrue(tradable.should_notify)

        cooldown = evaluate_reminder(
            _report(_candidate(name="Cooldown Knife", pct=10.5, status=INVENTORY_FILTER_COOLDOWN_ONLY)),
            TYieldReminderConfig(inventory_scope=INVENTORY_SCOPE_NOT_ALL_COOLDOWN),
            TYieldReminderState(),
            now=datetime(2026, 4, 1, 14, 0),
        )
        self.assertFalse(cooldown.should_notify)
        self.assertEqual("local_only", cooldown.reason)

    def test_all_cooldown_scope_matches_only_all_cooldown(self) -> None:
        config = TYieldReminderConfig(inventory_scope=INVENTORY_SCOPE_ALL_COOLDOWN)
        decision = evaluate_reminder(
            _report(_candidate(name="Cooldown Knife", pct=10.5, status=INVENTORY_FILTER_COOLDOWN_ONLY)),
            config,
            TYieldReminderState(),
            now=datetime(2026, 4, 1, 14, 0),
        )
        self.assertTrue(decision.should_notify)
        self.assertEqual("hot", decision.reason)

    def test_daily_summary_after_1530_uses_same_inventory_scope(self) -> None:
        config = TYieldReminderConfig(
            inventory_scope=INVENTORY_SCOPE_ALL,
            daily_summary_time="15:30",
        )
        state = TYieldReminderState()
        decision = evaluate_reminder(
            _report(
                _candidate(name="Mixed Candidate", pct=9.5, status=INVENTORY_FILTER_MIXED_ONLY),
                _candidate(name="Tradable Hot Candidate", pct=12.0, status=INVENTORY_FILTER_TRADABLE_ONLY),
            ),
            config,
            state,
            now=datetime(2026, 4, 1, 15, 30),
        )
        self.assertTrue(decision.should_notify)
        self.assertEqual("daily", decision.reason)
        self.assertIsNotNone(decision.notification)
        assert decision.notification is not None
        self.assertIn("Mixed Candidate", decision.notification.body)
        self.assertIn("Tradable Hot Candidate", decision.notification.body)
        self.assertIn("范围: 全部", decision.notification.body)

    def test_daily_summary_triggers_after_target_time_if_not_yet_sent(self) -> None:
        config = TYieldReminderConfig(daily_summary_time="15:30")
        state = TYieldReminderState()
        decision = evaluate_reminder(
            _report(_candidate(name="Mixed Candidate", pct=9.5, status=INVENTORY_FILTER_MIXED_ONLY)),
            config,
            state,
            now=datetime(2026, 4, 1, 15, 47),
        )
        self.assertTrue(decision.should_notify)
        self.assertEqual("daily", decision.reason)

    def test_daily_summary_only_sends_once_per_day_after_target_time(self) -> None:
        config = TYieldReminderConfig(daily_summary_time="15:30")
        state = TYieldReminderState(last_daily_summary_date="2026-04-01")
        decision = evaluate_reminder(
            _report(_candidate(name="Mixed Candidate", pct=9.5, status=INVENTORY_FILTER_MIXED_ONLY)),
            config,
            state,
            now=datetime(2026, 4, 1, 16, 10),
        )
        self.assertFalse(decision.should_notify)
        self.assertEqual("local_only", decision.reason)

    def test_local_only_when_no_hot_and_not_summary_time(self) -> None:
        config = TYieldReminderConfig(daily_summary_time="15:30")
        state = TYieldReminderState()
        decision = evaluate_reminder(
            _report(_candidate(name="Tradable Candidate", pct=9.5, status=INVENTORY_FILTER_TRADABLE_ONLY)),
            config,
            state,
            now=datetime(2026, 4, 1, 14, 0),
        )
        self.assertFalse(decision.should_notify)
        self.assertEqual("local_only", decision.reason)


if __name__ == "__main__":
    unittest.main()
