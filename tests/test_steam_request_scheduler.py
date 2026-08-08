from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cs2_assistant.db import Database
from cs2_assistant.services.steam_request_scheduler import (
    GLOBAL_CIRCUIT_KEY,
    QUIET_WINDOW_CIRCUIT_KEY,
    SteamRequestPriority,
    SteamRequestGuardRejected,
    SteamRequestScheduler,
    SteamRequestTimeout,
    configure_shared_steam_scheduler,
    get_shared_steam_scheduler,
    normalize_steam_route,
    parse_retry_after_seconds,
    reset_shared_steam_scheduler,
)


class FakeResponse:
    def __init__(self, status_code: int, *, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}


class SteamRequestSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "scheduler.db"
        self.db = Database(self.db_path)
        self.db.initialize()

    def tearDown(self) -> None:
        reset_shared_steam_scheduler()
        self.db.close()
        self.temporary.cleanup()

    def test_unconfigured_global_facade_is_safe_direct_mode(self) -> None:
        reset_shared_steam_scheduler()
        calls: list[str] = []

        result = get_shared_steam_scheduler().call(
            method="GET",
            url="https://steamcommunity.com/market/",
            callback=lambda: calls.append("called") or "ok",
            source="account_balance",
        )

        self.assertEqual("ok", result)
        self.assertEqual(["called"], calls)
        self.assertFalse(get_shared_steam_scheduler().configured)
        with get_shared_steam_scheduler().acquire(source="test") as permit:
            self.assertEqual("direct", permit.complete("direct"))

    def test_configure_by_path_uses_shared_sqlite_queue(self) -> None:
        scheduler = configure_shared_steam_scheduler(
            self.db_path,
            quiet_window_seconds=0,
        )

        result = scheduler.call(
            method="GET",
            url="https://steamcommunity.com/market/mylistings?start=0",
            account="account-a",
            priority=SteamRequestPriority.P2_SYNC,
            source="guadao",
            operation="sync-listings",
            callback=lambda: FakeResponse(200),
        )

        self.assertEqual(200, result.status_code)
        verifier = Database(self.db_path)
        try:
            row = verifier.list_steam_requests(limit=1)[0]
            self.assertEqual("completed", row["status"])
            self.assertEqual("market/mylistings", row["route"])
            self.assertEqual(2, row["priority"])
        finally:
            verifier.close()

    def test_old_owner_cannot_reset_a_newer_shared_scheduler(self) -> None:
        first = configure_shared_steam_scheduler(self.db_path, quiet_window_seconds=0)
        second = configure_shared_steam_scheduler(self.db_path, quiet_window_seconds=0)

        self.assertIsNot(first, second)
        self.assertFalse(reset_shared_steam_scheduler(expected=first))
        self.assertIs(second, get_shared_steam_scheduler())
        self.assertTrue(reset_shared_steam_scheduler(expected=second))
        self.assertFalse(get_shared_steam_scheduler().configured)

    def test_configured_facade_serializes_callbacks_across_threads(self) -> None:
        scheduler = configure_shared_steam_scheduler(
            self.db_path,
            quiet_window_seconds=0,
            poll_seconds=0.01,
        )
        lock = threading.Lock()
        active = 0
        max_active = 0
        completed: list[str] = []

        def run(name: str) -> None:
            def callback() -> FakeResponse:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                    completed.append(name)
                return FakeResponse(200)

            scheduler.call(
                method="GET",
                url=f"https://steamcommunity.com/market/{name}",
                callback=callback,
                source="test",
                priority=2,
                timeout_seconds=2,
            )

        threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(1, max_active)
        self.assertCountEqual(["a", "b"], completed)

    def test_priority_queue_position_and_high_priority_claim(self) -> None:
        self.db.enqueue_steam_request(
            "low",
            source="notify",
            route="market/orderbook",
            priority=3,
        )
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        position = scheduler.queue_position("low")
        self.assertEqual(1, position.position)
        self.assertEqual(1, position.pending_count)
        self.assertEqual(0, position.running_count)

        response = scheduler.execute(
            lambda: FakeResponse(200),
            source="guadao",
            route="market/removelisting/123",
            priority=SteamRequestPriority.P0_SAFETY,
            method="POST",
            timeout_seconds=1,
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("pending", self.db.get_steam_request("low")["status"])  # type: ignore[index]
        rows = self.db.list_steam_requests(limit=10)
        high = next(row for row in rows if row["request_id"] != "low")
        self.assertEqual("completed", high["status"])

    def test_queue_timeout_does_not_steal_another_process_callback(self) -> None:
        self.db.enqueue_steam_request(
            "owner-a",
            source="guadao",
            route="market/mylistings",
            priority=0,
        )
        self.assertIsNotNone(
            self.db.claim_steam_request("owner-a", "worker-a", lease_seconds=30)
        )
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("callback must not run"),
                source="profit_trade",
                route="market/listings",
                priority=1,
                timeout_seconds=0,
                quiet_before=False,
            )

        active = self.db.get_steam_request("owner-a")
        self.assertEqual("running", active["status"])  # type: ignore[index]
        self.assertEqual("worker-a", active["lease_owner"])  # type: ignore[index]

    def test_p1_default_queue_wait_survives_one_running_p0_login(self) -> None:
        self.db.enqueue_steam_request(
            "cookie-login",
            source="cookie_refresh",
            route="login",
            priority=SteamRequestPriority.P0_SAFETY,
        )
        self.assertIsNotNone(
            self.db.claim_steam_request("cookie-login", "login-worker", lease_seconds=30)
        )
        started_at = datetime.now(timezone.utc)
        elapsed_seconds = 0.0

        def now_provider() -> datetime:
            return started_at + timedelta(seconds=elapsed_seconds)

        def advance_clock(seconds: float) -> None:
            nonlocal elapsed_seconds
            elapsed_seconds += seconds
            if elapsed_seconds >= 8.0:
                self.db.complete_steam_request(
                    "cookie-login",
                    "login-worker",
                    status="completed",
                    http_status=200,
                    now=now_provider().isoformat(),
                )

        scheduler = SteamRequestScheduler(
            self.db,
            now_provider=now_provider,
            sleep=advance_clock,
            quiet_window_seconds=0,
            poll_seconds=1,
        )

        response = scheduler.execute(
            lambda: FakeResponse(200),
            source="profit_trade",
            route="market/orderbook",
            priority=SteamRequestPriority.P1_EXECUTION,
            account_id="account-a",
        )

        self.assertEqual(200, response.status_code)
        self.assertGreaterEqual(elapsed_seconds, 8.0)

    def test_retry_after_route_cooldown_and_multi_account_global_circuit(self) -> None:
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)
        combinations = (
            ("a", "market/listings"),
            ("b", "market/orderbook"),
        )
        for account, route in combinations:
            scheduler.execute(
                lambda: FakeResponse(429, retry_after="7"),
                source="guadao",
                route=route,
                priority=1,
                account_id=account,
                timeout_seconds=1,
            )

        route_circuit = self.db.get_steam_route_circuit(
            "steam:account:a:route:market/listings"
        )
        self.assertEqual("open", route_circuit["state"])  # type: ignore[index]
        route_delay = (
            datetime.fromisoformat(route_circuit["cooldown_until"])
            - datetime.fromisoformat(route_circuit["last_429_at"])
        ).total_seconds()  # type: ignore[index]
        self.assertEqual(7, route_delay)
        global_circuit = self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)
        self.assertIsNotNone(global_circuit)
        self.assertEqual("open", global_circuit["state"])  # type: ignore[index]
        self.assertLessEqual(
            datetime.fromisoformat(global_circuit["first_429_at"]),  # type: ignore[index]
            datetime.fromisoformat(route_circuit["first_429_at"]),  # type: ignore[index]
        )
        payload = global_circuit["payload_json"]  # type: ignore[index]
        self.assertIn('"eventCount":2', payload)
        self.assertIn('"accountCount":2', payload)

    def test_bounded_profit_listings_retry_never_bypasses_explicit_retry_after(self) -> None:
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)
        scheduler.record_429(account_id="a", route="market/listings", retry_after="30")

        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("explicit Retry-After must remain authoritative"),
                source="profit_trade",
                route="market/listings/730/Item",
                priority=SteamRequestPriority.P1_EXECUTION,
                account_id="a",
                bounded_retry=True,
                timeout_seconds=0,
                quiet_before=False,
            )

        scheduler.record_429(account_id="b", route="market/listings", retry_after=None)
        recovered = scheduler.execute(
            lambda: FakeResponse(200),
            source="profit_trade",
            route="market/listings/730/Item",
            priority=SteamRequestPriority.P1_EXECUTION,
            account_id="b",
            bounded_retry=True,
            timeout_seconds=1,
            quiet_before=False,
        )
        self.assertEqual(200, recovered.status_code)

        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("unbounded Profit listings must remain cooled"),
                source="profit_trade",
                route="market/listings/730/Item",
                priority=SteamRequestPriority.P1_EXECUTION,
                account_id="a",
                bounded_retry=False,
                timeout_seconds=0,
                quiet_before=False,
            )
        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("CLI cannot claim Profit's bounded retry exemption"),
                source="cli",
                route="market/listings/730/Item",
                priority=SteamRequestPriority.P1_EXECUTION,
                account_id="a",
                bounded_retry=True,
                timeout_seconds=0,
                quiet_before=False,
            )

    def test_other_p1_routes_respect_account_route_cooldown(self) -> None:
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)
        scheduler.record_429(account_id="a", route="market/buylisting/123", retry_after="30")

        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("buylisting P1 must not bypass its cooldown"),
                source="profit_trade",
                route="market/buylisting/456",
                priority=SteamRequestPriority.P1_EXECUTION,
                account_id="a",
                timeout_seconds=0,
            )

    def test_auth_throttled_result_is_recorded_as_429(self) -> None:
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        result = scheduler.execute(
            lambda: (False, "steam_auth_throttled", {}),
            source="cookie_refresh",
            route="steam/auth/login",
            priority=SteamRequestPriority.P0_SAFETY,
            account_id="a",
            timeout_seconds=1,
        )

        self.assertEqual((False, "steam_auth_throttled", {}), result)
        row = self.db.list_steam_requests(limit=1)[0]
        self.assertEqual("failed", row["status"])
        self.assertEqual(429, row["http_status"])
        circuit = self.db.get_steam_route_circuit("steam:account:a:route:steam/auth/login")
        self.assertEqual("open", circuit["state"])  # type: ignore[index]

    def test_exception_response_status_is_not_lost(self) -> None:
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        class HttpFailure(RuntimeError):
            def __init__(self) -> None:
                super().__init__("HTTP 429")
                self.response = FakeResponse(429, retry_after="9")

        def fail() -> None:
            raise HttpFailure()

        with self.assertRaises(HttpFailure):
            scheduler.execute(
                fail,
                source="guadao",
                route="market/orderbook",
                priority=3,
                account_id="a",
                timeout_seconds=1,
            )

        row = self.db.list_steam_requests(limit=1)[0]
        self.assertEqual(429, row["http_status"])
        circuit = self.db.get_steam_route_circuit("steam:account:a:route:market/orderbook")
        self.assertEqual("open", circuit["state"])  # type: ignore[index]

    def test_half_open_allows_only_one_probe(self) -> None:
        now = datetime.now(timezone.utc)
        self.db.upsert_steam_route_circuit(
            GLOBAL_CIRCUIT_KEY,
            scope="global",
            state="open",
            cooldown_until=now - timedelta(seconds=1),
            next_probe_at=now - timedelta(seconds=1),
        )
        first = SteamRequestScheduler(self.db, worker_id="first", quiet_window_seconds=0)
        second = SteamRequestScheduler(self.db, worker_id="second", quiet_window_seconds=0)
        probes = first._claim_required_probes(
            priority=SteamRequestPriority.P1_EXECUTION,
            account_id="a",
            route="market/listings",
        )
        self.assertEqual([GLOBAL_CIRCUIT_KEY], probes)

        with self.assertRaisesRegex(Exception, "half-open probe"):
            second._claim_required_probes(
                priority=SteamRequestPriority.P1_EXECUTION,
                account_id="b",
                route="market/orderbook",
            )
        first._release_probes(probes, success=True)
        recovered = self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)
        self.assertEqual("closed", recovered["state"])  # type: ignore[index]
        self.assertEqual(0, recovered["consecutive_429"])  # type: ignore[index]
        self.assertIsNone(recovered["first_429_at"])  # type: ignore[index]

    def test_failed_half_open_probe_reopens_with_authoritative_cooldown(self) -> None:
        now = datetime.now(timezone.utc)
        self.db.upsert_steam_route_circuit(
            GLOBAL_CIRCUIT_KEY,
            scope="global",
            state="open",
            first_429_at=now - timedelta(minutes=5),
            last_429_at=now - timedelta(minutes=1),
            cooldown_until=now - timedelta(seconds=1),
            next_probe_at=now - timedelta(seconds=1),
        )
        scheduler = SteamRequestScheduler(self.db, worker_id="probe", quiet_window_seconds=0)

        scheduler.execute(
            lambda: FakeResponse(429, retry_after="7"),
            source="profit_trade",
            route="market/listings",
            priority=1,
            account_id="a",
            timeout_seconds=1,
            quiet_before=False,
        )

        global_circuit = self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)
        self.assertEqual("open", global_circuit["state"])  # type: ignore[index]
        self.assertIsNone(global_circuit["probe_lease_owner"])  # type: ignore[index]
        global_delay = (
            datetime.fromisoformat(global_circuit["next_probe_at"])
            - datetime.fromisoformat(global_circuit["last_429_at"])
        ).total_seconds()  # type: ignore[index]
        self.assertEqual(600, global_delay)

    def test_half_open_probe_requires_explicit_2xx_to_recover(self) -> None:
        now = datetime.now(timezone.utc)
        self.db.upsert_steam_route_circuit(
            GLOBAL_CIRCUIT_KEY,
            scope="global",
            state="open",
            cooldown_until=now - timedelta(seconds=1),
            next_probe_at=now - timedelta(seconds=1),
        )
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        response = scheduler.execute(
            lambda: FakeResponse(500),
            source="profit_trade",
            route="market/listings",
            priority=SteamRequestPriority.P1_EXECUTION,
            account_id="a",
            timeout_seconds=1,
            quiet_before=False,
        )

        self.assertEqual(500, response.status_code)
        circuit = self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)
        self.assertEqual("open", circuit["state"])  # type: ignore[index]
        self.assertIsNone(circuit["probe_lease_owner"])  # type: ignore[index]

    def test_default_admission_wait_is_bounded(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.db.upsert_steam_route_circuit(
            GLOBAL_CIRCUIT_KEY,
            scope="global",
            state="open",
            cooldown_until=future,
            next_probe_at=future,
        )
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        started = time.monotonic()
        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("callback must not run while circuit is open"),
                source="guadao",
                route="market/mylistings",
                priority=SteamRequestPriority.P2_SYNC,
            )
        self.assertLess(time.monotonic() - started, 1.0)

    def test_orphaned_tickets_are_cancelled_before_they_can_block_queue(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.db.enqueue_steam_request(
            "orphan-running",
            source="old-process",
            route="market/removelisting",
            priority=0,
            available_at=old,
        )
        self.assertIsNotNone(
            self.db.claim_steam_request(
                "orphan-running",
                "dead-worker",
                lease_seconds=5,
                now=old,
            )
        )
        self.db.enqueue_steam_request(
            "orphan-pending",
            source="old-process",
            route="market/removelisting",
            priority=0,
        )
        self.db.conn.execute(
            "UPDATE steam_request_queue SET updated_at = ? WHERE request_id = ?",
            (old.isoformat(), "orphan-pending"),
        )
        self.db.conn.commit()

        scheduler = configure_shared_steam_scheduler(
            self.db_path,
            quiet_window_seconds=0,
        )
        response = scheduler.execute(
            lambda: FakeResponse(200),
            source="guadao",
            route="market/removelisting/9",
            priority=SteamRequestPriority.P0_SAFETY,
            timeout_seconds=1,
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("cancelled", self.db.get_steam_request("orphan-pending")["status"])  # type: ignore[index]
        self.assertEqual("cancelled", self.db.get_steam_request("orphan-running")["status"])  # type: ignore[index]

    def test_execution_guard_rejects_after_permit_before_callback(self) -> None:
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)
        callbacks: list[str] = []

        with self.assertRaises(SteamRequestGuardRejected):
            scheduler.execute(
                lambda: callbacks.append("called") or FakeResponse(200),
                source="profit_trade",
                route="market/buylisting/1",
                priority=SteamRequestPriority.P1_EXECUTION,
                execution_guard=lambda: False,
                timeout_seconds=1,
            )

        self.assertEqual([], callbacks)
        row = self.db.list_steam_requests(limit=1)[0]
        self.assertEqual("failed", row["status"])

    def test_global_circuit_uses_30_minute_probe_after_one_hour(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.db.upsert_steam_route_circuit(
            GLOBAL_CIRCUIT_KEY,
            scope="global",
            state="open",
            first_429_at=now - timedelta(minutes=61),
            last_429_at=now - timedelta(minutes=31),
            cooldown_until=now - timedelta(seconds=1),
            next_probe_at=now - timedelta(seconds=1),
        )
        scheduler = SteamRequestScheduler(
            self.db,
            now_provider=lambda: now,
            quiet_window_seconds=0,
        )

        summary = scheduler.record_429(account_id="a", route="market/listings")

        self.assertTrue(summary["globalCircuitOpened"])
        circuit = self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)
        self.assertEqual(
            1800,
            (
                datetime.fromisoformat(circuit["next_probe_at"])
                - datetime.fromisoformat(circuit["last_429_at"])
            ).total_seconds(),  # type: ignore[index]
        )

    def test_closed_historical_global_circuit_does_not_reopen_for_one_route_429(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.db.upsert_steam_route_circuit(
            GLOBAL_CIRCUIT_KEY,
            scope="global",
            state="closed",
            first_429_at=now - timedelta(hours=3),
            last_429_at=now - timedelta(hours=2),
            cooldown_until=None,
            next_probe_at=None,
        )
        scheduler = SteamRequestScheduler(
            self.db,
            now_provider=lambda: now,
            quiet_window_seconds=0,
        )

        summary = scheduler.record_429(account_id="a", route="market/listings")

        self.assertFalse(summary["globalCircuitOpened"])
        global_circuit = self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)
        self.assertEqual("closed", global_circuit["state"])  # type: ignore[index]
        route_circuit = self.db.get_steam_route_circuit(
            "steam:account:a:route:market/listings"
        )
        self.assertEqual("open", route_circuit["state"])  # type: ignore[index]

    def test_single_listings_429_only_blocks_listings_route(self) -> None:
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        summary = scheduler.record_429(account_id="account-a", route="market/listings")

        self.assertFalse(summary["globalCircuitOpened"])
        global_circuit = self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)
        self.assertTrue(
            global_circuit is None or global_circuit["state"] == "closed"  # type: ignore[index]
        )
        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("cooled listings callback must not run"),
                source="profit_trade",
                route="market/listings/730/Glock-18%20%7C%20Candy%20Apple",
                account_id="account-a",
                priority=SteamRequestPriority.P1_EXECUTION,
                timeout_seconds=0.01,
            )

        completed_routes: list[str] = []
        for route, priority in (
            ("market/orderbook", SteamRequestPriority.P2_SYNC),
            ("market/createbuyorder", SteamRequestPriority.P1_EXECUTION),
            ("market/mylistings", SteamRequestPriority.P2_SYNC),
        ):
            response = scheduler.execute(
                lambda route=route: completed_routes.append(route) or FakeResponse(200),
                source="profit_trade",
                route=route,
                account_id="account-a",
                priority=priority,
                timeout_seconds=1,
            )
            self.assertEqual(200, response.status_code)

        self.assertEqual(
            ["market/orderbook", "market/createbuyorder", "market/mylistings"],
            completed_routes,
        )

    def test_listings_only_429s_across_accounts_never_open_global_circuit(self) -> None:
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        for account_id in ("account-a", "account-b", "account-c"):
            response = scheduler.execute(
                lambda: FakeResponse(429),
                source="profit_trade",
                route="market/listings/730/Glock-18%20%7C%20Candy%20Apple",
                account_id=account_id,
                priority=SteamRequestPriority.P1_EXECUTION,
                timeout_seconds=1,
                quiet_before=False,
            )
            self.assertEqual(429, response.status_code)

        global_circuit = self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)
        self.assertTrue(
            global_circuit is None or global_circuit["state"] == "closed"  # type: ignore[index]
        )
        response = scheduler.execute(
            lambda: FakeResponse(200),
            source="profit_trade",
            route="market/createbuyorder",
            account_id="account-a",
            priority=SteamRequestPriority.P1_EXECUTION,
            timeout_seconds=1,
        )
        self.assertEqual(200, response.status_code)

    def test_scheduler_telemetry_is_metadata_only_and_redacted(self) -> None:
        events: list[dict[str, object]] = []
        scheduler = SteamRequestScheduler(
            self.db,
            telemetry=events.append,
            quiet_window_seconds=0,
        )

        scheduler.execute(
            lambda: FakeResponse(200),
            source="guadao",
            route="market/mylistings",
            priority=2,
            metadata={"cookie": "secret-cookie", "purpose": "status_sync"},
            timeout_seconds=1,
        )

        self.assertEqual(["queued", "start", "success"], [row["phase"] for row in events])
        serialized = str(events)
        self.assertNotIn("secret-cookie", serialized)
        self.assertNotIn("request_body", serialized)

    def test_quiet_window_blocks_p2_but_not_p0(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.db.upsert_steam_route_circuit(
            QUIET_WINDOW_CIRCUIT_KEY,
            scope="quiet",
            state="open",
            route="market/listings",
            cooldown_until=future,
            next_probe_at=future,
        )
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)
        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("P2 must remain paused"),
                source="guadao",
                route="market/mylistings",
                priority=SteamRequestPriority.P2_SYNC,
                timeout_seconds=0,
            )

        response = scheduler.execute(
            lambda: FakeResponse(200),
            source="guadao",
            route="market/removelisting/9",
            priority=SteamRequestPriority.P0_SAFETY,
            timeout_seconds=1,
        )
        self.assertEqual(200, response.status_code)

    def test_already_queued_p2_rechecks_quiet_window_before_global_lease(self) -> None:
        self.db.enqueue_steam_request(
            "running-blocker",
            source="test",
            route="market/blocker",
            priority=SteamRequestPriority.P0_SAFETY,
        )
        self.assertIsNotNone(
            self.db.claim_steam_request(
                "running-blocker",
                "blocker-worker",
                lease_seconds=30,
            )
        )
        callback_started = threading.Event()
        callback_times: list[datetime] = []
        errors: list[BaseException] = []

        def callback() -> FakeResponse:
            callback_times.append(datetime.now(timezone.utc))
            callback_started.set()
            return FakeResponse(200)

        def run_p2() -> None:
            thread_db = Database(self.db_path)
            thread_db.initialize()
            scheduler = SteamRequestScheduler(
                thread_db,
                worker_id="p2-worker",
                quiet_window_seconds=0,
                poll_seconds=0.01,
            )
            try:
                scheduler.execute(
                    callback,
                    source="guadao",
                    route="market/mylistings",
                    priority=SteamRequestPriority.P2_SYNC,
                    account_id="account-a",
                    timeout_seconds=2,
                )
            except BaseException as exc:  # pragma: no cover - assertion captures it
                errors.append(exc)
            finally:
                thread_db.close()

        thread = threading.Thread(target=run_p2)
        thread.start()
        queued_deadline = time.monotonic() + 1.0
        while time.monotonic() < queued_deadline:
            if any(
                row["source"] == "guadao" and row["status"] == "pending"
                for row in self.db.list_steam_requests(limit=20)
            ):
                break
            time.sleep(0.01)
        else:
            self.fail(
                "P2 request did not enter the queue: "
                f"errors={errors!r}, rows={[dict(row) for row in self.db.list_steam_requests(limit=20)]!r}"
            )

        quiet_until = datetime.now(timezone.utc) + timedelta(seconds=1.0)
        self.db.upsert_steam_route_circuit(
            QUIET_WINDOW_CIRCUIT_KEY,
            scope="quiet",
            state="open",
            route="market/listings",
            cooldown_until=quiet_until,
            next_probe_at=quiet_until,
        )
        persisted_quiet_until = datetime.fromisoformat(
            self.db.get_steam_route_circuit(QUIET_WINDOW_CIRCUIT_KEY)["cooldown_until"]  # type: ignore[index]
        )
        self.db.complete_steam_request(
            "running-blocker",
            "blocker-worker",
            status="completed",
            http_status=200,
        )

        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(callback_started.is_set())
        self.assertGreaterEqual(
            callback_times[0],
            persisted_quiet_until,
            "queued P2 callback must not run inside a newly opened quiet window",
        )

    def test_p2_requeues_if_quiet_window_opens_during_lease_claim_race(self) -> None:
        scheduler = SteamRequestScheduler(
            self.db,
            worker_id="race-worker",
            quiet_window_seconds=0,
            poll_seconds=0.01,
        )
        original_claim = self.db.claim_steam_request
        opened_quiet = False
        callback_times: list[datetime] = []
        persisted_quiet_until: datetime | None = None

        def claim_and_open_quiet(
            request_id: str,
            worker_id: str,
            **kwargs: object,
        ):
            nonlocal opened_quiet, persisted_quiet_until
            claimed = original_claim(request_id, worker_id, **kwargs)
            if claimed is not None and not opened_quiet:
                opened_quiet = True
                quiet_until = datetime.now(timezone.utc) + timedelta(seconds=1.0)
                self.db.upsert_steam_route_circuit(
                    QUIET_WINDOW_CIRCUIT_KEY,
                    scope="quiet",
                    state="open",
                    route="market/listings",
                    cooldown_until=quiet_until,
                    next_probe_at=quiet_until,
                )
                persisted_quiet_until = datetime.fromisoformat(
                    self.db.get_steam_route_circuit(QUIET_WINDOW_CIRCUIT_KEY)["cooldown_until"]  # type: ignore[index]
                )
            return claimed

        self.db.claim_steam_request = claim_and_open_quiet  # type: ignore[method-assign]
        try:
            result = scheduler.execute(
                lambda: callback_times.append(datetime.now(timezone.utc)) or FakeResponse(200),
                source="guadao",
                route="market/mylistings",
                priority=SteamRequestPriority.P2_SYNC,
                account_id="account-a",
                timeout_seconds=3,
            )
        finally:
            self.db.claim_steam_request = original_claim  # type: ignore[method-assign]

        self.assertEqual(200, result.status_code)
        self.assertIsNotNone(persisted_quiet_until)
        self.assertGreaterEqual(callback_times[0], persisted_quiet_until)  # type: ignore[arg-type]
        rows = self.db.list_steam_requests(limit=10)
        self.assertEqual(1, sum(row["status"] == "cancelled" for row in rows))
        self.assertEqual(1, sum(row["status"] == "completed" for row in rows))
        cancelled = next(row for row in rows if row["status"] == "cancelled")
        self.assertIn("admission_changed", cancelled["last_error"])

    def test_p0_bypasses_unrelated_global_circuit_but_p1_does_not(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.db.upsert_steam_route_circuit(
            GLOBAL_CIRCUIT_KEY,
            scope="global",
            state="open",
            cooldown_until=future,
            next_probe_at=future,
        )
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        response = scheduler.execute(
            lambda: FakeResponse(200),
            source="guadao",
            route="market/removelisting/9",
            priority=SteamRequestPriority.P0_SAFETY,
            account_id="account-a",
            timeout_seconds=1,
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "open",
            self.db.get_steam_route_circuit(GLOBAL_CIRCUIT_KEY)["state"],  # type: ignore[index]
        )
        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("P1 must remain blocked by the global circuit"),
                source="profit_trade",
                route="market/buylisting/10",
                priority=SteamRequestPriority.P1_EXECUTION,
                account_id="account-a",
                timeout_seconds=0,
                quiet_before=False,
            )

    def test_p0_still_respects_its_own_account_route_retry_after(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(minutes=2)
        own_route_key = "steam:account:account-a:route:market/removelisting"
        self.db.upsert_steam_route_circuit(
            own_route_key,
            scope="account_route",
            state="open",
            account_id="account-a",
            route="market/removelisting",
            cooldown_until=future,
            next_probe_at=future,
            payload={"retryAfter": 120},
        )
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("P0 must not bypass its own Retry-After"),
                source="guadao",
                route="market/removelisting/9",
                priority=SteamRequestPriority.P0_SAFETY,
                account_id="account-a",
                timeout_seconds=0,
            )

    def test_bounded_profit_listings_retry_still_respects_global_circuit(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.db.upsert_steam_route_circuit(
            GLOBAL_CIRCUIT_KEY,
            scope="global",
            state="open",
            cooldown_until=future,
            next_probe_at=future,
        )
        self.db.upsert_steam_route_circuit(
            "steam:account:account-a:route:market/listings",
            scope="account_route",
            state="open",
            account_id="account-a",
            route="market/listings",
            cooldown_until=future,
            next_probe_at=future,
        )
        scheduler = SteamRequestScheduler(self.db, quiet_window_seconds=0)

        with self.assertRaises(SteamRequestTimeout):
            scheduler.execute(
                lambda: self.fail("bounded retry only bypasses its route cooldown"),
                source="profit_trade",
                route="market/listings/730/Item",
                priority=SteamRequestPriority.P1_EXECUTION,
                account_id="account-a",
                bounded_retry=True,
                timeout_seconds=0,
                quiet_before=False,
            )

    def test_route_normalization_and_retry_after_http_date(self) -> None:
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            "market/listings",
            normalize_steam_route(
                "https://steamcommunity.com/market/listings/730/MOUZ?start=0"
            ),
        )
        self.assertEqual(
            "market/removelisting",
            normalize_steam_route("/market/removelisting/517500112410499413"),
        )
        self.assertEqual(
            120,
            parse_retry_after_seconds("Thu, 16 Jul 2026 12:02:00 GMT", now=now),
        )


if __name__ == "__main__":
    unittest.main()
