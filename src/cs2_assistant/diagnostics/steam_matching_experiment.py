from __future__ import annotations

import argparse
import json
import math
import queue
import socket
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from cs2_assistant.accounts import Account, AccountStore
from cs2_assistant.clients.steam_market import SteamMarketClient, SteamMarketError
from cs2_assistant.config import PROJECT_ROOT, load_settings
from cs2_assistant.services.steam_request_scheduler import (
    DirectSteamRequestScheduler,
    get_shared_steam_scheduler,
)


APP_ID = 730
CONTEXT_ID = "2"
CURRENCY_ID = 23
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "steam_market_matching_test"


@dataclass(frozen=True, slots=True)
class Candidate:
    market_hash_name: str
    asset_id: str
    seller_steam_id: str


@dataclass(frozen=True, slots=True)
class TrialSpec:
    key: str
    action: str
    candidate: Candidate


@dataclass(slots=True)
class AccountRole:
    seller: Account
    buyer_a: Account
    buyer_c: Account
    observer_d: Account
    observer_e: Account


@dataclass(slots=True)
class PricePlan:
    floor_cents: int
    previous_high_bid_cents: int | None
    a_bid_cents: int
    c_bid_cents: int
    seller_net_cents: int
    seller_fee_cents: int
    buyer_total_cents: int
    wallet_market_minimum_cents: int
    wallet_fee_minimum_cents: int


@dataclass(slots=True)
class TrialState:
    trial: TrialSpec
    roles: AccountRole
    price: PricePlan
    started_wall: float = field(default_factory=time.time)
    started_mono: float = field(default_factory=time.monotonic)
    a_order_created_wall: float | None = None
    a_order_mylistings_wall: float | None = None
    b_submitted_wall: float | None = None
    b_confirmation_checked_wall: float | None = None
    phase: str = "created"
    a_buy_order_id: str | None = None
    c_buy_order_id: str | None = None
    c_action_sent: bool = False
    b_listing_id: str | None = None
    b_listing_subtotal_cents: int | None = None
    b_listing_fee_cents: int | None = None
    a_receipt: dict[str, Any] | None = None
    c_receipt: dict[str, Any] | None = None
    b_sale_receipt: dict[str, Any] | None = None
    terminal_buyer: str | None = None
    terminal_wall: float | None = None
    first_cross_wall: float | None = None
    first_public_listing_wall: float | None = None
    first_a_bid_visible_wall: float | None = None
    last_low_ask_wall: float | None = None
    last_a_bid_visible_wall: float | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wallets_before: dict[str, dict[str, Any]] = field(default_factory=dict)
    wallets_after: dict[str, dict[str, Any]] = field(default_factory=dict)
    asset_verification: dict[str, Any] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    cross_event: threading.Event = field(default_factory=threading.Event, repr=False)
    monitor_stop: threading.Event = field(default_factory=threading.Event, repr=False)
    monitor_threads: list[threading.Thread] = field(default_factory=list, repr=False)


TRIALS = (
    TrialSpec(
        key="control",
        action="none",
        candidate=Candidate(
            "XM1014 | Hieroglyph (Well-Worn)",
            "48137863919",
            "76561199119018953",
        ),
    ),
    TrialSpec(
        key="late_higher_buy_order",
        action="createbuyorder",
        candidate=Candidate(
            "Five-SeveN | Desert Seal (Field-Tested)",
            "52745245990",
            "76561198279977505",
        ),
    ),
    TrialSpec(
        key="late_buylisting",
        action="buylisting",
        candidate=Candidate(
            "MAC-10 | Storm Camo (Factory New)",
            "48995006621",
            "76561199119018953",
        ),
    ),
)

FALLBACK_CANDIDATES = (
    Candidate("XM1014 | Hieroglyph (Well-Worn)", "48137863919", "76561199119018953"),
    Candidate("Five-SeveN | Desert Seal (Field-Tested)", "52745245990", "76561198279977505"),
    Candidate("MAC-10 | Storm Camo (Factory New)", "48995006621", "76561199119018953"),
    Candidate("XM1014 | Mockingbird (Battle-Scarred)", "48995006375", "76561199119018953"),
    Candidate("MAG-7 | Resupply (Well-Worn)", "48995006393", "76561199119018953"),
    Candidate("Negev | Wall Bang (Field-Tested)", "48989715399", "76561198279977505"),
    Candidate("Dual Berettas | Colony (Minimal Wear)", "50734175269", "76561198279977505"),
    Candidate("FAMAS | Vendetta (Field-Tested)", "50148902584", "76561198279977505"),
    Candidate("Five-SeveN | Coolant (Minimal Wear)", "48995006529", "76561199119018953"),
)


def utc_iso(timestamp: float | None = None) -> str:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def beijing_iso(timestamp: float | None = None) -> str:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, BEIJING).isoformat()


def steam_fee_breakdown(
    seller_net_cents: int,
    *,
    fee_minimum_cents: int,
) -> tuple[int, int, int]:
    """Return Steam fee, CS2 publisher fee, and buyer total in integer cents."""

    if seller_net_cents <= 0:
        raise ValueError("seller net cents must be positive")
    if fee_minimum_cents <= 0:
        raise ValueError("wallet fee minimum cents must be positive")
    steam_fee = max(fee_minimum_cents, math.floor(seller_net_cents * 0.05))
    publisher_fee = max(fee_minimum_cents, math.floor(seller_net_cents * 0.10))
    return steam_fee, publisher_fee, seller_net_cents + steam_fee + publisher_fee


def seller_net_for_exact_total(
    total_cents: int,
    *,
    fee_minimum_cents: int,
    seller_minimum_cents: int,
) -> tuple[int, int]:
    if total_cents <= 0:
        raise ValueError("buyer total cents must be positive")
    matches: list[tuple[int, int]] = []
    for seller_net in range(max(1, seller_minimum_cents), total_cents + 1):
        steam_fee, publisher_fee, total = steam_fee_breakdown(
            seller_net,
            fee_minimum_cents=fee_minimum_cents,
        )
        if total == total_cents:
            matches.append((seller_net, steam_fee + publisher_fee))
    if len(matches) != 1:
        raise ValueError(
            f"buyer total {total_cents} cents has {len(matches)} exact seller-net mappings"
        )
    return matches[0]


def parse_orderbook(payload: dict[str, Any]) -> dict[str, int | None]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    def value(name: str) -> int | None:
        raw = data.get(name)
        try:
            return int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def top(name: str) -> tuple[int | None, int | None]:
        rows = data.get(name) or []
        if isinstance(rows, list) and len(rows) >= 2 and not isinstance(rows[0], list):
            try:
                return int(rows[0]), int(rows[1])
            except (TypeError, ValueError):
                return None, None
        if isinstance(rows, list) and rows and isinstance(rows[0], list) and len(rows[0]) >= 2:
            try:
                return int(rows[0][0]), int(rows[0][1])
            except (TypeError, ValueError):
                return None, None
        return None, None

    min_sell, min_sell_count = top("rgCompactSellOrders")
    max_buy, max_buy_count = top("rgCompactBuyOrders")
    return {
        "minSellCents": min_sell if min_sell is not None else value("amtMinSellOrder"),
        "minSellCount": min_sell_count,
        "maxBuyCents": max_buy if max_buy is not None else value("amtMaxBuyOrder"),
        "maxBuyCount": max_buy_count,
        "sellOrderCount": value("cSellOrders"),
        "buyOrderCount": value("cBuyOrders"),
    }


def compact_orderbook_levels(payload: dict[str, Any]) -> dict[str, list[Any]]:
    """Return the public compact levels without retaining any authenticated payload."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    sell = data.get("rgCompactSellOrders") or []
    buy = data.get("rgCompactBuyOrders") or []
    return {
        "compactSellOrders": list(sell) if isinstance(sell, list) else [],
        "compactBuyOrders": list(buy) if isinstance(buy, list) else [],
    }


def orderbook_currency_context_matches(
    left: dict[str, int | None],
    right: dict[str, int | None],
    *,
    tolerance_cents: int = 2,
) -> bool:
    """Reject observer sessions that clearly render the same book in another currency."""
    compared = 0
    for key in ("minSellCents", "maxBuyCents"):
        left_value = left.get(key)
        right_value = right.get(key)
        if left_value is None or right_value is None:
            continue
        compared += 1
        if abs(int(left_value) - int(right_value)) > tolerance_cents:
            return False
    return compared > 0


def build_price_plan(
    orderbook: dict[str, int | None],
    *,
    wallet_market_minimum_cents: int,
    wallet_fee_minimum_cents: int,
    max_total_cents: int = 100,
) -> PricePlan:
    floor_cents = int(orderbook.get("minSellCents") or 0)
    if floor_cents > max_total_cents:
        raise ValueError(f"Steam floor {floor_cents} cents exceeds the {max_total_cents}-cent cap")
    minimum_buyer_total = 3 * int(wallet_market_minimum_cents)
    buyer_total = minimum_buyer_total
    high_bid = orderbook.get("maxBuyCents")
    a_bid = max(
        buyer_total + 1,
        (int(high_bid) + 1) if high_bid is not None else buyer_total + 1,
    )
    c_bid = a_bid + 1
    if floor_cents <= c_bid:
        raise ValueError(
            f"Steam floor {floor_cents} cents does not leave room above minimum-price "
            f"B={buyer_total}, A={a_bid}, C={c_bid} cents"
        )
    seller_net, seller_fee = seller_net_for_exact_total(
        buyer_total,
        fee_minimum_cents=wallet_fee_minimum_cents,
        seller_minimum_cents=wallet_market_minimum_cents,
    )
    return PricePlan(
        floor_cents=floor_cents,
        previous_high_bid_cents=int(high_bid) if high_bid is not None else None,
        a_bid_cents=a_bid,
        c_bid_cents=c_bid,
        seller_net_cents=seller_net,
        seller_fee_cents=seller_fee,
        buyer_total_cents=buyer_total,
        wallet_market_minimum_cents=wallet_market_minimum_cents,
        wallet_fee_minimum_cents=wallet_fee_minimum_cents,
    )


def stable_unique_a_bid(samples: Iterable[dict[str, Any]], a_bid_cents: int) -> bool:
    tail = list(samples)[-3:]
    return len(tail) == 3 and all(
        row.get("maxBuyCents") == a_bid_cents and row.get("maxBuyCount") == 1
        for row in tail
    )


def classify_trial(state: TrialState) -> str:
    if state.errors:
        return "evidence_conflict_or_request_failure"
    if state.terminal_buyer == state.roles.buyer_a.name:
        if state.first_public_listing_wall is None:
            return "existing_high_bid_matched_before_public_listing"
        if state.trial.action == "none":
            return "existing_high_bid_won_control"
        return "existing_high_bid_won_and_later_action_did_not_overtake"
    if state.terminal_buyer == state.roles.buyer_c.name:
        if state.trial.action == "createbuyorder":
            return "later_higher_buy_order_overtook"
        if state.trial.action == "buylisting":
            return "later_buylisting_overtook"
    return "insufficient_evidence"


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="jsonl-writer", daemon=True)

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def emit(self, event: dict[str, Any]) -> None:
        self.queue.put(dict(event))

    def close(self) -> None:
        self.queue.put(None)
        self.thread.join(timeout=10)

    def _run(self) -> None:
        with self.path.open("a", encoding="utf-8", buffering=1) as handle:
            while True:
                event = self.queue.get()
                if event is None:
                    return
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


class SteamMatchingExperiment:
    def __init__(
        self,
        *,
        output_dir: Path,
        baseline_seconds: float = 60.0,
        post_terminal_seconds: float = 600.0,
        max_terminal_wait_seconds: float = 180.0,
        execute: bool = False,
        trial_keys: tuple[str, ...] | None = None,
    ) -> None:
        self.settings = load_settings()
        self.db_path = self.settings.db_path
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.writer = JsonlWriter(self.output_dir / "events.jsonl")
        self.baseline_seconds = max(1.0, float(baseline_seconds))
        self.post_terminal_seconds = max(0.0, float(post_terminal_seconds))
        self.max_terminal_wait_seconds = max(30.0, float(max_terminal_wait_seconds))
        self.execute = execute
        selected = set(trial_keys or tuple(row.key for row in TRIALS))
        unknown = selected.difference(row.key for row in TRIALS)
        if unknown:
            raise ValueError(f"unknown trial keys: {', '.join(sorted(unknown))}")
        self.trials = tuple(row for row in TRIALS if row.key in selected)
        if not self.trials:
            raise ValueError("at least one trial is required")
        self.accounts = AccountStore(PROJECT_ROOT / "config").list_accounts()
        self.account_by_steam = {
            str(account.steam_id64): account
            for account in self.accounts
            if account.steam_id64
        }
        self.clients: dict[str, SteamMarketClient] = {}
        self.observer_clients: dict[str, SteamMarketClient] = {}
        self.account_locks: dict[str, threading.RLock] = {}
        self.results: list[dict[str, Any]] = []
        self.global_errors: list[str] = []
        self.abort_all = threading.Event()

    def event(self, trial: str, kind: str, **fields: Any) -> None:
        now = time.time()
        safe = {
            "utc": utc_iso(now),
            "beijing": beijing_iso(now),
            "monotonic": time.monotonic(),
            "trial": trial,
            "event": kind,
            **fields,
        }
        forbidden = {
            "cookie",
            "cookies",
            "password",
            "secret",
            "token",
            "sessionid",
            "trade_url",
            "raw",
        }
        if any(str(key).lower() in forbidden for key in safe):
            raise ValueError("sensitive event field refused")
        self.writer.emit(safe)

    def client(self, account: Account) -> SteamMarketClient:
        cached = self.clients.get(account.id)
        if cached is not None:
            return cached
        if not account.cookies or not account.steam_id64:
            raise RuntimeError(f"account {account.name} lacks Steam cookies or SteamID")
        client = SteamMarketClient(
            cookies=account.cookies,
            steam_id64=account.steam_id64,
            identity_secret=account.identity_secret,
            device_id=account.device_id,
            account_id=account.id,
            base_url=self.settings.steam_market_base_url,
            request_source="steam_matching_experiment",
        )
        self.clients[account.id] = client
        self.account_locks.setdefault(account.id, threading.RLock())
        return client

    def observer_client(self, account: Account) -> SteamMarketClient:
        cached = self.observer_clients.get(account.id)
        if cached is not None:
            return cached
        if not account.cookies or not account.steam_id64:
            raise RuntimeError(f"observer account {account.name} lacks Steam cookies or SteamID")
        client = SteamMarketClient(
            cookies=account.cookies,
            steam_id64=account.steam_id64,
            identity_secret=account.identity_secret,
            device_id=account.device_id,
            account_id=account.id,
            base_url=self.settings.steam_market_base_url,
            request_source="steam_matching_experiment_observer",
        )
        self.observer_clients[account.id] = client
        return client

    def _select_cny_observers(self) -> tuple[Account, Account]:
        reference = next((row for row in self.accounts if row.name == "x6l1cg3cy5o"), None)
        candidate = next((row for row in self.accounts if row.name == "ropzx55x"), None)
        if reference is None or candidate is None:
            raise RuntimeError("the fixed CNY observer accounts are unavailable")
        wallet = self.observer_client(reference).wallet_balance(safety_terminal=True)
        if int(wallet.get("currency_id") or 0) != CURRENCY_ID:
            raise RuntimeError("reference observer wallet is not CNY")
        second_wallet = self.observer_client(candidate).wallet_balance(safety_terminal=True)
        if int(second_wallet.get("currency_id") or 0) != CURRENCY_ID:
            raise RuntimeError("second observer wallet is not CNY")
        return reference, candidate

    def _assert_environment_stopped(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", 8765)) == 0:
                raise RuntimeError("port 8765 is listening; stop the backend before this experiment")
        scheduler = get_shared_steam_scheduler()
        if not isinstance(scheduler, DirectSteamRequestScheduler):
            raise RuntimeError("shared Steam scheduler is configured; run the experiment standalone")
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            enabled = con.execute(
                "SELECT executor_key FROM executor_runtime_state WHERE enabled = 1"
            ).fetchall()
            if enabled:
                raise RuntimeError(
                    "executor runtime flags are enabled: "
                    + ", ".join(str(row["executor_key"]) for row in enabled)
                )
        finally:
            con.close()

    def _assert_local_candidate_free(self, candidate: Candidate) -> None:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            asset = con.execute(
                "SELECT * FROM inventory_assets WHERE asset_id = ?",
                (candidate.asset_id,),
            ).fetchone()
            if asset is None:
                raise RuntimeError(f"local asset missing: {candidate.asset_id}")
            if int(asset["tradable"] or 0) != 1 or str(asset["status"]) != "available":
                raise RuntimeError(
                    f"local asset unavailable: {candidate.asset_id} tradable={asset['tradable']} status={asset['status']}"
                )
            reservation = con.execute(
                "SELECT id FROM asset_reservations WHERE asset_id = ? AND status IN ('active','consumed')",
                (candidate.asset_id,),
            ).fetchone()
            if reservation is not None:
                raise RuntimeError(f"asset has a live reservation: {candidate.asset_id}")
            trade = con.execute(
                "SELECT id FROM profit_trades WHERE a_asset_id = ? AND status NOT IN ('completed','cancelled','c5_sold')",
                (candidate.asset_id,),
            ).fetchone()
            if trade is not None:
                raise RuntimeError(f"asset has a live Profit Trade: {candidate.asset_id}")
            operation = con.execute(
                "SELECT id FROM pool_operations WHERE asset_id = ? AND status NOT IN ('completed','canceled','failed')",
                (candidate.asset_id,),
            ).fetchone()
            if operation is not None:
                raise RuntimeError(f"asset has a live guadao operation: {candidate.asset_id}")
        finally:
            con.close()

    def _assert_remote_asset(self, candidate: Candidate, seller: Account) -> None:
        response = self.client(seller)._request(  # noqa: SLF001 - isolated diagnostic
            "GET",
            f"/profiles/{seller.steam_id64}/inventory/json/{APP_ID}/{CONTEXT_ID}",
            params={"l": "english"},
        )
        payload = response.json()
        inventory = payload.get("rgInventory") or {}
        descriptions = payload.get("rgDescriptions") or {}
        if payload.get("success") not in (1, True) or not isinstance(inventory, dict):
            raise SteamMarketError("authenticated Steam inventory is unreadable")
        asset = inventory.get(candidate.asset_id)
        if asset is None:
            raise RuntimeError(f"remote Steam inventory lacks asset {candidate.asset_id}")
        description_key = f"{asset.get('classid')}_{asset.get('instanceid')}"
        description = descriptions.get(description_key) or {}
        if int(description.get("tradable") or 0) != 1:
            raise RuntimeError(f"remote asset is not tradable: {candidate.asset_id}")
        if str(description.get("market_hash_name") or "") != candidate.market_hash_name:
            raise RuntimeError(f"remote asset name mismatch: {candidate.asset_id}")

    @staticmethod
    def _public_inventory_asset(
        steam_id: str,
        asset_id: str,
    ) -> dict[str, Any] | None:
        url = f"https://steamcommunity.com/inventory/{steam_id}/{APP_ID}/{CONTEXT_ID}"
        response = requests.get(url, params={"l": "english", "count": 2000}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        return next(
            (
                row
                for row in payload.get("assets") or []
                if str(row.get("assetid") or "") == str(asset_id)
            ),
            None,
        )

    @staticmethod
    def _buy_order_id(payload: dict[str, Any]) -> str:
        for key in ("buy_orderid", "buy_order_id", "buyOrderId", "id"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _buy_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("buy_orders") or []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _row_buy_order_id(row: dict[str, Any]) -> str:
        for key in ("buy_orderid", "buy_order_id", "buyOrderId", "orderid", "id"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _raw_listing_for_asset(payload: dict[str, Any], asset_id: str) -> dict[str, Any] | None:
        listings = payload.get("listings") or []
        iterable = listings.values() if isinstance(listings, dict) else listings
        for row in iterable:
            if not isinstance(row, dict):
                continue
            row_asset = row.get("asset") if isinstance(row.get("asset"), dict) else {}
            row_asset_id = str(
                row_asset.get("id") or row.get("assetid") or row.get("asset_id") or ""
            )
            if row_asset_id == str(asset_id):
                return row
        return None

    @staticmethod
    def _listing_identity(row: dict[str, Any] | None) -> tuple[str | None, int | None, int | None]:
        if not row:
            return None, None, None
        listing_id = str(
            row.get("listingid") or row.get("listing_id") or row.get("id") or ""
        ).strip() or None
        try:
            subtotal = int(row.get("price"))
            fee = int(row.get("fee"))
        except (TypeError, ValueError):
            subtotal = fee = None
        return listing_id, subtotal, fee

    def _roles(self, trial_index: int, candidate: Candidate) -> AccountRole:
        seller = self.account_by_steam.get(candidate.seller_steam_id)
        if seller is None:
            raise RuntimeError(f"seller account not configured: {candidate.seller_steam_id}")
        others = [account for account in self.accounts if account.id != seller.id]
        if len(others) < 4:
            raise RuntimeError("five configured Steam accounts are required")
        shift = trial_index % len(others)
        rotated = others[shift:] + others[:shift]
        return AccountRole(
            seller=seller,
            buyer_a=rotated[0],
            buyer_c=rotated[1],
            observer_d=rotated[2],
            observer_e=rotated[3],
        )

    def _safe_call(self, state: TrialState, account: Account, operation: str, fn: Any) -> Any:
        started = time.monotonic()
        try:
            lock_key = f"observer:{account.id}" if operation.startswith("orderbook") else account.id
            with self.account_locks.setdefault(lock_key, threading.RLock()):
                value = fn()
        except SteamMarketError as exc:
            elapsed = round((time.monotonic() - started) * 1000, 2)
            message = str(exc)[:500]
            self.event(
                state.trial.key,
                "steam_request_error",
                account=account.name,
                operation=operation,
                elapsedMs=elapsed,
                statusCode=exc.status_code,
                retryAfter=exc.retry_after,
                message=message,
            )
            state.errors.append(f"{account.name}:{operation}:{message}")
            self.abort_all.set()
            raise
        elapsed = round((time.monotonic() - started) * 1000, 2)
        self.event(
            state.trial.key,
            "steam_request_ok",
            account=account.name,
            operation=operation,
            elapsedMs=elapsed,
        )
        return value

    def _preflight_trial(self, trial_index: int, trial: TrialSpec) -> TrialState:
        roles = self._roles(trial_index, trial.candidate)
        roles.observer_d, roles.observer_e = self._select_cny_observers()
        buyers = [
            account
            for account in self.accounts
            if account.id not in {roles.seller.id, roles.observer_d.id, roles.observer_e.id}
        ]
        if len(buyers) < 2:
            raise RuntimeError("two isolated CNY action buyers are unavailable")
        roles.buyer_a, roles.buyer_c = buyers[0], buyers[1]
        buyer_wallet = self.client(roles.buyer_a).wallet_balance(safety_terminal=True)
        c_wallet = self.client(roles.buyer_c).wallet_balance(safety_terminal=True)
        if int(buyer_wallet.get("currency_id") or 0) != CURRENCY_ID:
            raise RuntimeError(f"{roles.buyer_a.name} wallet is not CNY")
        if int(c_wallet.get("currency_id") or 0) != CURRENCY_ID:
            raise RuntimeError(f"{roles.buyer_c.name} wallet is not CNY")
        wallet_raw = buyer_wallet.get("raw") if isinstance(buyer_wallet.get("raw"), dict) else {}
        market_minimum = int(wallet_raw.get("wallet_market_minimum") or 0)
        fee_minimum = int(wallet_raw.get("wallet_fee_minimum") or market_minimum or 0)
        if market_minimum <= 0 or fee_minimum <= 0:
            raise RuntimeError("Steam wallet did not return market/fee minimums")
        observer_payload = self.observer_client(roles.observer_d).order_book(
            app_id=APP_ID,
            market_hash_name=trial.candidate.market_hash_name,
        )
        second_observer_payload = self.observer_client(roles.observer_e).order_book(
            app_id=APP_ID,
            market_hash_name=trial.candidate.market_hash_name,
        )
        observer_book = parse_orderbook(observer_payload)
        second_observer_book = parse_orderbook(second_observer_payload)
        if not orderbook_currency_context_matches(observer_book, second_observer_book):
            raise RuntimeError(
                "observer orderbook currency mismatch: "
                f"{roles.observer_d.name}={observer_book} "
                f"{roles.observer_e.name}={second_observer_book}"
            )
        price = build_price_plan(
            observer_book,
            wallet_market_minimum_cents=market_minimum,
            wallet_fee_minimum_cents=fee_minimum,
        )
        self._assert_local_candidate_free(trial.candidate)
        self._assert_remote_asset(trial.candidate, roles.seller)
        owner_payload = self.client(roles.seller).my_listings(count=100)
        if self._raw_listing_for_asset(owner_payload, trial.candidate.asset_id) is not None:
            raise RuntimeError(f"candidate asset is already listed: {trial.candidate.asset_id}")
        for account in (roles.buyer_a, roles.buyer_c):
            payload = self.client(account).my_listings(count=100)
            for row in self._buy_orders(payload):
                description = row.get("description") if isinstance(row.get("description"), dict) else {}
                name = str(
                    row.get("market_hash_name")
                    or row.get("marketHashName")
                    or row.get("hash_name")
                    or row.get("name")
                    or description.get("market_hash_name")
                    or ""
                )
                if name == trial.candidate.market_hash_name:
                    raise RuntimeError(
                        f"{account.name} already has a buy order for {trial.candidate.market_hash_name}"
                    )
        state = TrialState(trial=trial, roles=roles, price=price)
        state.wallets_before[roles.buyer_a.name] = self._wallet_public_snapshot(buyer_wallet)
        state.wallets_before[roles.buyer_c.name] = self._wallet_public_snapshot(c_wallet)
        return state

    def _select_trial_state(
        self,
        trial_index: int,
        template: TrialSpec,
        used_market_names: set[str],
    ) -> TrialState:
        ordered = (template.candidate,) + tuple(
            candidate
            for candidate in FALLBACK_CANDIDATES
            if candidate.market_hash_name != template.candidate.market_hash_name
        )
        price_errors: list[str] = []
        for candidate in ordered:
            if candidate.market_hash_name in used_market_names:
                continue
            trial = replace(template, candidate=candidate)
            try:
                return self._preflight_trial(trial_index, trial)
            except ValueError as exc:
                message = str(exc)
                price_errors.append(f"{candidate.market_hash_name}: {message}")
                print(f"[候选跳过] {candidate.market_hash_name} | {message}")
            except RuntimeError as exc:
                message = str(exc)
                unavailable_markers = (
                    "local asset missing",
                    "local asset unavailable",
                    "remote Steam inventory lacks asset",
                    "remote asset is not tradable",
                    "remote asset name mismatch",
                    "candidate asset is already listed",
                    "asset has a live",
                )
                if not any(marker in message for marker in unavailable_markers):
                    raise
                price_errors.append(f"{candidate.market_hash_name}: {message}")
                print(f"[候选跳过] {candidate.market_hash_name} | {message}")
        raise RuntimeError("no safe candidate remains: " + "; ".join(price_errors))

    def _create_a_order(self, state: TrialState) -> None:
        latest_orderbook = parse_orderbook(
            self._safe_call(
                state,
                state.roles.observer_d,
                "orderbook_before_a",
                lambda: self.observer_client(state.roles.observer_d).order_book(
                    app_id=APP_ID,
                    market_hash_name=state.trial.candidate.market_hash_name,
                ),
            )
        )
        if latest_orderbook.get("minSellCents") != state.price.floor_cents:
            raise RuntimeError("Steam floor changed before A order; refusing to use the stale plan")
        latest_bid = latest_orderbook.get("maxBuyCents")
        if latest_bid is not None and int(latest_bid) >= state.price.a_bid_cents:
            raise RuntimeError("another bid reached the planned A price before submission")
        payload = self._safe_call(
            state,
            state.roles.buyer_a,
            "createbuyorder_a",
            lambda: self.client(state.roles.buyer_a).create_buy_order(
                app_id=APP_ID,
                market_hash_name=state.trial.candidate.market_hash_name,
                price_total=state.price.a_bid_cents,
                quantity=1,
                currency=CURRENCY_ID,
            ),
        )
        state.a_buy_order_id = self._buy_order_id(payload)
        state.a_order_created_wall = time.time()
        if not state.a_buy_order_id:
            listings = self._safe_call(
                state,
                state.roles.buyer_a,
                "mylistings_a_recover_id",
                lambda: self.client(state.roles.buyer_a).my_listings(
                    count=100,
                    safety_terminal=True,
                ),
            )
            candidates = []
            for row in self._buy_orders(listings):
                name = str(
                    row.get("market_hash_name")
                    or row.get("marketHashName")
                    or row.get("hash_name")
                    or row.get("name")
                    or ""
                ).strip()
                order_id = self._row_buy_order_id(row)
                if name == state.trial.candidate.market_hash_name and order_id:
                    candidates.append(order_id)
            if len(candidates) == 1:
                state.a_buy_order_id = candidates[0]
            else:
                raise RuntimeError(
                    "A createbuyorder returned no ID and no unique active order could be recovered"
                )
        self.event(
            state.trial.key,
            "a_buy_order_created",
            account=state.roles.buyer_a.name,
            buyOrderId=state.a_buy_order_id,
            priceCents=state.price.a_bid_cents,
        )

    def _confirm_a_order_visible(self, state: TrialState) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            payload = self._safe_call(
                state,
                state.roles.buyer_a,
                "mylistings_a_verify",
                lambda: self.client(state.roles.buyer_a).my_listings(count=100),
            )
            if any(
                self._row_buy_order_id(row) == state.a_buy_order_id
                for row in self._buy_orders(payload)
            ):
                self.event(
                    state.trial.key,
                    "a_buy_order_visible_in_mylistings",
                    account=state.roles.buyer_a.name,
                    buyOrderId=state.a_buy_order_id,
                )
                state.a_order_mylistings_wall = time.time()
                return
            time.sleep(1)
        raise RuntimeError("A buy order did not appear in mylistings")

    def _observe_once(self, state: TrialState, observer: Account) -> dict[str, Any]:
        started_wall = time.time()
        payload = self._safe_call(
            state,
            observer,
            "orderbook",
            lambda: self.observer_client(observer).order_book(
                app_id=APP_ID,
                market_hash_name=state.trial.candidate.market_hash_name,
            ),
        )
        parsed = parse_orderbook(payload)
        sample = {
            "observer": observer.name,
            "requestStartedWall": started_wall,
            "requestFinishedWall": time.time(),
            **parsed,
            **compact_orderbook_levels(payload),
        }
        with state.lock:
            state.samples.append(sample)
            now = sample["requestFinishedWall"]
            if parsed.get("maxBuyCents") == state.price.a_bid_cents:
                if state.first_a_bid_visible_wall is None:
                    state.first_a_bid_visible_wall = now
                state.last_a_bid_visible_wall = now
            if parsed.get("minSellCents") == state.price.buyer_total_cents:
                if state.first_public_listing_wall is None:
                    state.first_public_listing_wall = now
                state.last_low_ask_wall = now
            if (
                state.b_submitted_wall is not None
                and state.terminal_wall is None
                and parsed.get("minSellCents") is not None
                and int(parsed["minSellCents"] or 0) <= state.price.a_bid_cents
                and state.first_cross_wall is None
            ):
                state.first_cross_wall = now
                state.cross_event.set()
        self.event(state.trial.key, "orderbook_sample", **sample)
        return sample

    def _wait_for_stable_a_bid(self, state: TrialState) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with state.lock:
                stable = stable_unique_a_bid(state.samples, state.price.a_bid_cents)
            if stable:
                return
            if self.abort_all.wait(0.2):
                break
        raise RuntimeError("A did not become the unique highest bid in three samples")

    def _baseline(self, state: TrialState) -> None:
        state.phase = "baseline"
        with state.lock:
            start_index = len(state.samples)
        deadline = time.monotonic() + self.baseline_seconds
        original_floor = state.price.floor_cents
        while time.monotonic() < deadline:
            with state.lock:
                samples = list(state.samples[start_index:])
            if any(
                sample.get("maxBuyCents") != state.price.a_bid_cents
                or sample.get("maxBuyCount") != 1
                for sample in samples
            ):
                raise RuntimeError("A stopped being the unique highest bid during baseline")
            if any(sample.get("minSellCents") != original_floor for sample in samples):
                raise RuntimeError("minimum sell price changed during the 60-second baseline")
            if self.abort_all.wait(0.2):
                raise RuntimeError("monitoring stopped during baseline")

    def _submit_b_listing(self, state: TrialState) -> None:
        state.phase = "listing"
        synthetic_gross = state.price.seller_net_cents / (100.0 * 0.869)
        payload = self._safe_call(
            state,
            state.roles.seller,
            "sellitem_b",
            lambda: self.client(state.roles.seller).sell_item(
                app_id=APP_ID,
                context_id=CONTEXT_ID,
                asset_id=state.trial.candidate.asset_id,
                price=synthetic_gross,
                quantity=1,
                steam_net_factor=0.869,
            ),
        )
        state.b_listing_id = str(payload.get("listingid") or "").strip() or None
        state.b_submitted_wall = time.time()
        self.event(
            state.trial.key,
            "b_sell_submitted",
            account=state.roles.seller.name,
            assetId=state.trial.candidate.asset_id,
            listingId=state.b_listing_id,
            sellerNetCents=state.price.seller_net_cents,
            buyerTotalCents=state.price.buyer_total_cents,
        )
        pending_rows = self._safe_call(
            state,
            state.roles.seller,
            "pending_listing_identity_b",
            lambda: self.client(state.roles.seller).list_confirmation_pending_listings(),
        )
        pending_ids = {
            str(getattr(row, "listing_id", "") or "").strip()
            for row in pending_rows
            if str(getattr(row, "asset_id", "") or "").strip()
            == state.trial.candidate.asset_id
            and str(getattr(row, "listing_id", "") or "").strip()
        }
        if len(pending_ids) != 1:
            raise RuntimeError("B pending listing ID is missing or ambiguous")
        pending_listing_id = next(iter(pending_ids))
        if state.b_listing_id and state.b_listing_id != pending_listing_id:
            raise RuntimeError("B sell response and confirmation listing IDs conflict")
        state.b_listing_id = pending_listing_id
        self.event(
            state.trial.key,
            "b_pending_listing_identified",
            account=state.roles.seller.name,
            assetId=state.trial.candidate.asset_id,
            listingId=state.b_listing_id,
        )
        confirmed = self._safe_call(
            state,
            state.roles.seller,
            "confirm_listing_asset_b",
            lambda: self.client(state.roles.seller).confirm_listing_assets(
                asset_ids=[state.trial.candidate.asset_id],
                listing_ids=[state.b_listing_id] if state.b_listing_id else None,
            ),
        )
        state.b_confirmation_checked_wall = time.time()
        self.event(
            state.trial.key,
            "b_listing_confirmation_attempt",
            account=state.roles.seller.name,
            assetId=state.trial.candidate.asset_id,
            listingId=state.b_listing_id,
            confirmedCount=int(confirmed),
        )
        state.phase = "active"
        if state.trial.action == "buylisting":
            # The experiment owns B and can prove this exact pending listing ID.
            # Trigger C as soon as B's scoped mobile confirmation completes;
            # waiting for aggregate orderbook publication would defeat the test.
            state.cross_event.set()

    def _refresh_b_listing_identity(self, state: TrialState) -> dict[str, Any] | None:
        payload = self._safe_call(
            state,
            state.roles.seller,
            "mylistings_b",
            lambda: self.client(state.roles.seller).my_listings(count=100),
        )
        row = self._raw_listing_for_asset(payload, state.trial.candidate.asset_id)
        listing_id, subtotal, fee = self._listing_identity(row)
        if listing_id:
            if state.b_listing_id and state.b_listing_id != listing_id:
                raise RuntimeError("B listing ID conflict")
            with state.lock:
                state.b_listing_id = listing_id
                state.b_listing_subtotal_cents = subtotal
                state.b_listing_fee_cents = fee
            if subtotal is None or fee is None or subtotal + fee != state.price.buyer_total_cents:
                raise RuntimeError("B listing amount does not equal the planned A bid")
        return row

    def _later_action(self, state: TrialState) -> None:
        if state.trial.action == "none":
            return
        if state.trial.action == "createbuyorder" and state.first_cross_wall is None:
            return
        if state.trial.action == "createbuyorder":
            payload = self._safe_call(
                state,
                state.roles.buyer_c,
                "createbuyorder_c",
                lambda: self.client(state.roles.buyer_c).create_buy_order(
                    app_id=APP_ID,
                    market_hash_name=state.trial.candidate.market_hash_name,
                    price_total=state.price.c_bid_cents,
                    quantity=1,
                    currency=CURRENCY_ID,
                ),
            )
            state.c_buy_order_id = self._buy_order_id(payload)
            if not state.c_buy_order_id:
                raise RuntimeError("C createbuyorder returned no buy order ID")
            state.c_action_sent = True
            self.event(
                state.trial.key,
                "c_buy_order_created",
                account=state.roles.buyer_c.name,
                buyOrderId=state.c_buy_order_id,
                priceCents=state.price.c_bid_cents,
            )
            return
        if state.trial.action == "buylisting":
            listing_id = state.b_listing_id
            subtotal = state.price.seller_net_cents
            fee = state.price.seller_fee_cents
            if not listing_id:
                self.event(
                    state.trial.key,
                    "c_buylisting_not_sent",
                    reason="exact_b_listing_not_available",
                    assetId=state.trial.candidate.asset_id,
                )
                return
            self._safe_call(
                state,
                state.roles.buyer_c,
                "buylisting_c",
                lambda: self.client(state.roles.buyer_c).buy_listing(
                    listing_id=listing_id,
                    app_id=APP_ID,
                    subtotal=subtotal,
                    fee=fee,
                    total=subtotal + fee,
                    currency=CURRENCY_ID,
                    market_hash_name=state.trial.candidate.market_hash_name,
                ),
            )
            state.c_action_sent = True
            self.event(
                state.trial.key,
                "c_buylisting_sent",
                account=state.roles.buyer_c.name,
                listingId=listing_id,
                assetId=state.trial.candidate.asset_id,
                totalCents=subtotal + fee,
            )

    def _observer_loop(
        self,
        state: TrialState,
        observer: Account,
        initial_delay: float,
    ) -> None:
        if state.monitor_stop.wait(initial_delay):
            return
        while not state.monitor_stop.is_set() and not self.abort_all.is_set():
            try:
                self._observe_once(state, observer)
            except Exception as exc:
                self._thread_failure(state, "orderbook_monitor", exc)
                return
            if state.phase in {"created", "baseline"}:
                interval = 1.0
            elif state.phase == "post_terminal":
                terminal_elapsed = max(0.0, time.time() - float(state.terminal_wall or time.time()))
                interval = 1.0 if terminal_elapsed < 60 else 3.0
            else:
                active_elapsed = max(0.0, time.time() - float(state.b_submitted_wall or time.time()))
                interval = 0.5 if active_elapsed < 30 else 1.0
            state.monitor_stop.wait(interval)

    def _a_status_loop(self, state: TrialState) -> None:
        last_present: bool | None = None
        while not state.monitor_stop.is_set() and not self.abort_all.is_set():
            try:
                payload = self._safe_call(
                    state,
                    state.roles.buyer_a,
                    "mylistings_a_monitor",
                    lambda: self.client(state.roles.buyer_a).my_listings(count=100),
                )
            except Exception as exc:
                self._thread_failure(state, "a_status_monitor", exc)
                return
            present = any(
                self._row_buy_order_id(row) == state.a_buy_order_id
                for row in self._buy_orders(payload)
            )
            if present != last_present:
                self.event(
                    state.trial.key,
                    "a_buy_order_state",
                    account=state.roles.buyer_a.name,
                    buyOrderId=state.a_buy_order_id,
                    active=present,
                )
                last_present = present
            interval = 1.0 if state.phase == "active" else 2.0
            state.monitor_stop.wait(interval)

    def _b_status_loop(self, state: TrialState) -> None:
        last_listing_id: str | None = None
        while not state.monitor_stop.is_set() and not self.abort_all.is_set():
            try:
                row = self._refresh_b_listing_identity(state)
            except Exception as exc:
                self._thread_failure(state, "b_status_monitor", exc)
                return
            listing_id, subtotal, fee = self._listing_identity(row)
            if listing_id != last_listing_id:
                self.event(
                    state.trial.key,
                    "b_listing_state",
                    account=state.roles.seller.name,
                    assetId=state.trial.candidate.asset_id,
                    listingId=listing_id,
                    subtotalCents=subtotal,
                    feeCents=fee,
                    active=bool(listing_id),
                )
                last_listing_id = listing_id
            interval = 1.0 if state.phase == "active" else 2.0
            state.monitor_stop.wait(interval)

    def _c_action_loop(self, state: TrialState) -> None:
        while not state.monitor_stop.is_set() and not self.abort_all.is_set():
            if not state.cross_event.wait(0.1):
                continue
            try:
                self._later_action(state)
            except Exception as exc:
                self._thread_failure(state, "c_action", exc)
                return
            return

    def _thread_failure(self, state: TrialState, worker: str, exc: BaseException) -> None:
        message = str(exc)[:500]
        with state.lock:
            marker = f"{worker}:{message}"
            if marker not in state.errors:
                state.errors.append(marker)
        self.abort_all.set()
        self.event(state.trial.key, "monitor_failed", worker=worker, message=message)

    def _start_monitors(self, state: TrialState) -> None:
        state.monitor_stop.clear()
        threads = [
            threading.Thread(
                target=self._observer_loop,
                args=(state, state.roles.observer_d, 0.0),
                name=f"{state.trial.key}-orderbook-d",
                daemon=True,
            ),
            threading.Thread(
                target=self._observer_loop,
                args=(state, state.roles.observer_e, 0.25),
                name=f"{state.trial.key}-orderbook-e",
                daemon=True,
            ),
            threading.Thread(
                target=self._a_status_loop,
                args=(state,),
                name=f"{state.trial.key}-a-status",
                daemon=True,
            ),
            threading.Thread(
                target=self._b_status_loop,
                args=(state,),
                name=f"{state.trial.key}-b-status",
                daemon=True,
            ),
            threading.Thread(
                target=self._c_action_loop,
                args=(state,),
                name=f"{state.trial.key}-c-action",
                daemon=True,
            ),
        ]
        state.monitor_threads = threads
        for thread in threads:
            thread.start()

    @staticmethod
    def _stop_monitors(state: TrialState) -> None:
        state.monitor_stop.set()
        state.cross_event.set()
        for thread in state.monitor_threads:
            thread.join(timeout=10)
        state.monitor_threads.clear()

    def _find_terminal(self, state: TrialState) -> bool:
        earliest = int(state.started_wall) - 2
        if state.b_sale_receipt is None:
            state.b_sale_receipt = self._safe_call(
                state,
                state.roles.seller,
                "history_b",
                lambda: self.client(state.roles.seller).find_sale_receipt_by_asset(
                    state.trial.candidate.asset_id,
                    max_pages=1,
                ),
            )
        if state.b_sale_receipt is None:
            return False
        if state.a_receipt is None:
            state.a_receipt = self._safe_call(
                state,
                state.roles.buyer_a,
                "history_a",
                lambda: self.client(state.roles.buyer_a).find_purchase_receipt(
                    market_hash_name=state.trial.candidate.market_hash_name,
                    expected_total=state.price.buyer_total_cents / 100.0,
                    earliest_time=earliest,
                    total_tolerance=0.02,
                    max_pages=1,
                    safety_terminal=True,
                ),
            )
        if state.c_action_sent and state.c_receipt is None:
            state.c_receipt = self._safe_call(
                state,
                state.roles.buyer_c,
                "history_c",
                lambda: self.client(state.roles.buyer_c).find_purchase_receipt(
                    market_hash_name=state.trial.candidate.market_hash_name,
                    expected_total=state.price.buyer_total_cents / 100.0,
                    earliest_time=earliest,
                    total_tolerance=0.02,
                    max_pages=1,
                    safety_terminal=True,
                ),
            )
        buyers = []
        if state.a_receipt:
            buyers.append(state.roles.buyer_a.name)
        if state.c_receipt:
            buyers.append(state.roles.buyer_c.name)
        if len(buyers) > 1:
            raise RuntimeError("both A and C have matching purchase receipts")
        if buyers and state.b_sale_receipt:
            state.terminal_buyer = buyers[0]
            state.terminal_wall = time.time()
            receipt_listing_id = str(
                (state.a_receipt or state.c_receipt or {}).get("listingId") or ""
            ).strip()
            if receipt_listing_id:
                state.b_listing_id = state.b_listing_id or receipt_listing_id
            self.event(
                state.trial.key,
                "terminal_confirmed",
                buyer=state.terminal_buyer,
                aReceipt=state.a_receipt,
                cReceipt=state.c_receipt,
                bSaleReceipt=state.b_sale_receipt,
            )
            return True
        return False

    def _observe_until_terminal(self, state: TrialState) -> None:
        deadline = time.monotonic() + self.max_terminal_wait_seconds
        next_history = time.monotonic()
        while time.monotonic() < deadline and not self.abort_all.is_set():
            if time.monotonic() >= next_history:
                if self._find_terminal(state):
                    return
                next_history = time.monotonic() + 10
            time.sleep(0.1)
        if not self.abort_all.is_set() and self._find_terminal(state):
            return
        raise RuntimeError("no unambiguous terminal state within the bounded wait")

    def _post_terminal_observation(self, state: TrialState) -> None:
        state.phase = "post_terminal"
        deadline = time.monotonic() + self.post_terminal_seconds
        while time.monotonic() < deadline and not self.abort_all.is_set():
            time.sleep(0.2)

    def _wallet_snapshot(self, state: TrialState, account: Account, label: str) -> dict[str, Any]:
        payload = self._safe_call(
            state,
            account,
            f"wallet_{label}",
            lambda: self.client(account).wallet_balance(safety_terminal=True),
        )
        snapshot = self._wallet_public_snapshot(payload)
        self.event(
            state.trial.key,
            "wallet_snapshot",
            account=account.name,
            label=label,
            **snapshot,
        )
        return snapshot

    @staticmethod
    def _wallet_public_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
        return {
            "balance": payload.get("balance"),
            "delayedBalance": payload.get("delayed_balance"),
            "currency": payload.get("currency"),
            "currencyId": payload.get("currency_id"),
            "marketMinimumCents": int(raw.get("wallet_market_minimum") or 0) or None,
            "feeMinimumCents": int(raw.get("wallet_fee_minimum") or 0) or None,
        }

    def _capture_wallets(self, state: TrialState, *, after: bool) -> None:
        target = state.wallets_after if after else state.wallets_before
        label = "after" if after else "before"
        for account in (state.roles.seller, state.roles.buyer_a, state.roles.buyer_c):
            if not after and account.name in target:
                snapshot = target[account.name]
                self.event(
                    state.trial.key,
                    "wallet_snapshot",
                    account=account.name,
                    label=label,
                    **snapshot,
                )
                continue
            if account.id == state.roles.seller.id:
                try:
                    started = time.monotonic()
                    with self.account_locks.setdefault(account.id, threading.RLock()):
                        payload = self.client(account).wallet_balance(safety_terminal=True)
                    target[account.name] = self._wallet_public_snapshot(payload)
                    self.event(
                        state.trial.key,
                        "wallet_snapshot",
                        account=account.name,
                        label=label,
                        elapsedMs=round((time.monotonic() - started) * 1000, 2),
                        **target[account.name],
                    )
                except SteamMarketError as exc:
                    if exc.status_code == 429:
                        raise
                    target[account.name] = {
                        "available": False,
                        "reason": "wallet_info_unavailable",
                    }
                    self.event(
                        state.trial.key,
                        "wallet_snapshot_unavailable",
                        account=account.name,
                        label=label,
                        message=str(exc)[:180],
                    )
            else:
                target[account.name] = self._wallet_snapshot(state, account, label)
        if not after:
            for account in (state.roles.buyer_a, state.roles.buyer_c):
                wallet = target[account.name]
                if int(wallet.get("currencyId") or 0) != CURRENCY_ID:
                    raise RuntimeError(f"{account.name} wallet is not CNY")
            for buyer in (state.roles.buyer_a, state.roles.buyer_c):
                if float(target[buyer.name].get("balance") or 0) < 1.0:
                    raise RuntimeError(f"{buyer.name} wallet balance is below CNY 1")
            seller_wallet = target[state.roles.seller.name]
            if seller_wallet.get("available", True):
                if int(seller_wallet.get("marketMinimumCents") or 0) != state.price.wallet_market_minimum_cents:
                    raise RuntimeError("seller and buyer wallet market minimums differ")
                if int(seller_wallet.get("feeMinimumCents") or 0) != state.price.wallet_fee_minimum_cents:
                    raise RuntimeError("seller and buyer wallet fee minimums differ")

    def _verify_terminal_asset(self, state: TrialState) -> None:
        receipt = state.a_receipt if state.terminal_buyer == state.roles.buyer_a.name else state.c_receipt
        winner = state.roles.buyer_a if state.terminal_buyer == state.roles.buyer_a.name else state.roles.buyer_c
        new_asset_id = str((receipt or {}).get("newAssetId") or "").strip()
        if not new_asset_id:
            state.asset_verification = {
                "winner": winner.name,
                "newAssetId": None,
                "present": None,
                "reason": "purchase_receipt_has_no_new_asset_id",
            }
            return
        def inventory_ids(account: Account) -> set[str]:
            response = self.client(account)._request(  # noqa: SLF001 - isolated diagnostic
                "GET",
                f"/profiles/{account.steam_id64}/inventory/json/{APP_ID}/{CONTEXT_ID}",
                params={"l": "english"},
            )
            payload = response.json()
            inventory = payload.get("rgInventory") or {}
            if payload.get("success") not in (1, True) or not isinstance(inventory, dict):
                raise SteamMarketError("authenticated Steam inventory is unreadable")
            return {str(asset_id) for asset_id in inventory}

        deadline = time.monotonic() + 30
        present = False
        seller_old_present: bool | None = None
        while time.monotonic() < deadline and not self.abort_all.is_set():
            winner_ids = self._safe_call(
                state,
                winner,
                "inventory_winner_verify",
                lambda: inventory_ids(winner),
            )
            seller_ids = self._safe_call(
                state,
                state.roles.seller,
                "inventory_seller_verify",
                lambda: inventory_ids(state.roles.seller),
            )
            present = new_asset_id in winner_ids
            seller_old_present = state.trial.candidate.asset_id in seller_ids
            if present and not seller_old_present:
                break
            time.sleep(2)
        state.asset_verification = {
            "winner": winner.name,
            "newAssetId": new_asset_id,
            "present": present,
            "sellerOldAssetId": state.trial.candidate.asset_id,
            "sellerOldAssetPresent": seller_old_present,
            "source": "authenticated_inventory_json",
        }
        if not present or seller_old_present:
            raise RuntimeError(
                "official receipts exist but authenticated inventory ownership did not converge"
            )

    def _cancel_buy_order_if_active(
        self,
        state: TrialState,
        account: Account,
        buy_order_id: str | None,
    ) -> None:
        if not buy_order_id:
            return
        payload = self._safe_call(
            state,
            account,
            "mylistings_cleanup_check",
            lambda: self.client(account).my_listings(count=100, safety_terminal=True),
        )
        active = any(
            self._row_buy_order_id(row) == buy_order_id for row in self._buy_orders(payload)
        )
        if not active:
            return
        self._safe_call(
            state,
            account,
            "cancelbuyorder_cleanup",
            lambda: self.client(account).cancel_buy_order(buy_order_id=buy_order_id),
        )
        payload = self._safe_call(
            state,
            account,
            "mylistings_cleanup_verify",
            lambda: self.client(account).my_listings(count=100, safety_terminal=True),
        )
        if any(self._row_buy_order_id(row) == buy_order_id for row in self._buy_orders(payload)):
            raise RuntimeError(f"buy order still active after cleanup: {buy_order_id}")

    def _cleanup(self, state: TrialState) -> None:
        self._cancel_buy_order_if_active(state, state.roles.buyer_a, state.a_buy_order_id)
        self._cancel_buy_order_if_active(state, state.roles.buyer_c, state.c_buy_order_id)
        payload = self._safe_call(
            state,
            state.roles.seller,
            "mylistings_b_cleanup",
            lambda: self.client(state.roles.seller).my_listings(count=100, safety_terminal=True),
        )
        row = self._raw_listing_for_asset(payload, state.trial.candidate.asset_id)
        listing_id, _, _ = self._listing_identity(row)
        if listing_id:
            removed = self._safe_call(
                state,
                state.roles.seller,
                "removelisting_b_cleanup",
                lambda: self.client(state.roles.seller).remove_listing(listing_id),
            )
            if not removed:
                raise RuntimeError(f"failed to remove B listing {listing_id}")
            verify = self._safe_call(
                state,
                state.roles.seller,
                "mylistings_b_cleanup_verify",
                lambda: self.client(state.roles.seller).my_listings(
                    count=100,
                    safety_terminal=True,
                ),
            )
            if self._raw_listing_for_asset(verify, state.trial.candidate.asset_id):
                raise RuntimeError(f"B listing still active after cleanup: {listing_id}")

    @staticmethod
    def _delta_seconds(later: float | None, earlier: float | None) -> float | None:
        if later is None or earlier is None:
            return None
        return round(later - earlier, 3)

    @staticmethod
    def _stale_seconds(last_visible: float | None, official_terminal: float | None) -> float | None:
        if last_visible is None or official_terminal is None:
            return None
        return round(max(0.0, last_visible - official_terminal), 3)

    def _summarize(self, state: TrialState) -> dict[str, Any]:
        receipt = state.a_receipt or state.c_receipt or {}
        official_terminal = receipt.get("timePurchased")
        try:
            official_terminal_wall = float(official_terminal) if official_terminal is not None else None
        except (TypeError, ValueError):
            official_terminal_wall = None
        return {
            "trial": state.trial.key,
            "action": state.trial.action,
            "marketHashName": state.trial.candidate.market_hash_name,
            "assetId": state.trial.candidate.asset_id,
            "roles": {
                "seller": state.roles.seller.name,
                "buyerA": state.roles.buyer_a.name,
                "buyerC": state.roles.buyer_c.name,
                "observerD": state.roles.observer_d.name,
                "observerE": state.roles.observer_e.name,
            },
            "price": asdict(state.price),
            "cActionPriceCents": (
                state.price.buyer_total_cents
                if state.trial.action == "buylisting"
                else state.price.c_bid_cents
            ),
            "aBuyOrderId": state.a_buy_order_id,
            "cBuyOrderId": state.c_buy_order_id,
            "listingId": state.b_listing_id,
            "terminalBuyer": state.terminal_buyer,
            "aReceipt": state.a_receipt,
            "cReceipt": state.c_receipt,
            "bSaleReceipt": state.b_sale_receipt,
            "walletsBefore": state.wallets_before,
            "walletsAfter": state.wallets_after,
            "assetVerification": state.asset_verification,
            "classification": classify_trial(state),
            "timingSeconds": {
                "aOrderToOrderbookBid": self._delta_seconds(
                    state.first_a_bid_visible_wall,
                    state.a_order_created_wall,
                ),
                "aMylistingsToOrderbookBid": self._delta_seconds(
                    state.first_a_bid_visible_wall,
                    state.a_order_mylistings_wall,
                ),
                "bSubmitToOfficialPurchase": self._delta_seconds(
                    official_terminal_wall,
                    state.b_submitted_wall,
                ),
                "firstCrossToTerminal": self._delta_seconds(
                    official_terminal_wall,
                    state.first_cross_wall,
                ),
                "officialPurchaseToLastLowAsk": self._stale_seconds(
                    state.last_low_ask_wall,
                    official_terminal_wall,
                ),
                "officialPurchaseToLastABid": self._stale_seconds(
                    state.last_a_bid_visible_wall,
                    official_terminal_wall,
                ),
            },
            "errors": list(state.errors),
        }

    def _write_report(self) -> None:
        summary = {
            "generatedAt": utc_iso(),
            "execute": self.execute,
            "aborted": self.abort_all.is_set(),
            "globalErrors": list(self.global_errors),
            "trials": self.results,
        }
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        lines = [
            "# Steam 最高求购撮合与 Orderbook 延迟实验",
            "",
            f"- 生成时间：{beijing_iso()}",
            f"- 模式：{'真实执行' if self.execute else '只读预演'}",
            f"- 是否中止：{'是' if self.abort_all.is_set() else '否'}",
            "",
        ]
        for row in self.results:
            lines.extend(
                [
                    f"## {row['trial']} — {row['marketHashName']}",
                    "",
                    f"- 最终买家：{row.get('terminalBuyer') or '-'}",
                    f"- 结论分类：{row.get('classification')}",
                    f"- A 求购单：{row.get('aBuyOrderId') or '-'}",
                    f"- C 求购单：{row.get('cBuyOrderId') or '-'}",
                    f"- B listing：{row.get('listingId') or '-'}",
                    f"- 关键延迟：`{json.dumps(row.get('timingSeconds'), ensure_ascii=False)}`",
                    f"- 错误：{'; '.join(row.get('errors') or []) or '-'}",
                    "",
                ]
            )
        (self.output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    def run(self) -> int:
        self.writer.start()
        try:
            self._assert_environment_stopped()
            if not self.execute:
                used_market_names: set[str] = set()
                for index, trial in enumerate(self.trials):
                    state = self._select_trial_state(index, trial, used_market_names)
                    used_market_names.add(state.trial.candidate.market_hash_name)
                    print(
                        f"[预检] {state.trial.key} | {state.trial.candidate.market_hash_name} | "
                        f"B={state.roles.seller.name} A={state.roles.buyer_a.name} "
                        f"C={state.roles.buyer_c.name} D={state.roles.observer_d.name} "
                        f"E={state.roles.observer_e.name} | floor={state.price.floor_cents}分 "
                        f"A={state.price.a_bid_cents}分 C={state.price.c_bid_cents}分 "
                        f"sellerNet={state.price.seller_net_cents}分"
                    )
                    self.results.append(self._summarize(state))
                print(f"[预演完成] 未发送任何买卖请求，输出目录: {self.output_dir}")
                return 0
            used_market_names = set()
            for index, trial in enumerate(self.trials):
                if self.abort_all.is_set():
                    break
                try:
                    state = self._select_trial_state(index, trial, used_market_names)
                except Exception as exc:
                    message = str(exc)[:500]
                    self.global_errors.append(message)
                    self.abort_all.set()
                    self.event(trial.key, "preflight_aborted", message=message)
                    print(f"[preflight aborted] {trial.key} | {message}")
                    self._write_report()
                    break
                used_market_names.add(state.trial.candidate.market_hash_name)
                print(
                    f"[实时预检] {state.trial.key} | {state.trial.candidate.market_hash_name} | "
                    f"B={state.roles.seller.name} A={state.roles.buyer_a.name} "
                    f"C={state.roles.buyer_c.name} D={state.roles.observer_d.name} "
                    f"E={state.roles.observer_e.name} | floor={state.price.floor_cents}分 "
                    f"A={state.price.a_bid_cents}分 "
                    f"C动作价={state.price.buyer_total_cents if state.trial.action == 'buylisting' else state.price.c_bid_cents}分"
                )
                print(f"[开始] {state.trial.key} | {state.trial.candidate.market_hash_name}")
                try:
                    state.started_wall = time.time()
                    state.started_mono = time.monotonic()
                    self._capture_wallets(state, after=False)
                    self._create_a_order(state)
                    self._confirm_a_order_visible(state)
                    self._start_monitors(state)
                    self._wait_for_stable_a_bid(state)
                    print(f"[基线] {state.trial.key} | 稳定观察 {self.baseline_seconds:.0f}s")
                    self._baseline(state)
                    self._submit_b_listing(state)
                    print(f"[已上架] {state.trial.key} | 等待唯一成交终态")
                    self._observe_until_terminal(state)
                    self._verify_terminal_asset(state)
                    print(
                        f"[终态] {state.trial.key} | buyer={state.terminal_buyer} | "
                        f"继续观察 {self.post_terminal_seconds:.0f}s"
                    )
                    self._post_terminal_observation(state)
                    self._capture_wallets(state, after=True)
                except Exception as exc:
                    state.errors.append(str(exc)[:500])
                    self.abort_all.set()
                    self.event(state.trial.key, "trial_aborted", message=str(exc)[:500])
                    print(f"[中止] {state.trial.key} | {exc}")
                finally:
                    self._stop_monitors(state)
                    try:
                        self._cleanup(state)
                    except Exception as cleanup_exc:
                        state.errors.append(f"cleanup:{cleanup_exc}"[:500])
                        self.abort_all.set()
                        self.event(
                            state.trial.key,
                            "cleanup_failed",
                            message=str(cleanup_exc)[:500],
                        )
                        print(f"[清理失败] {state.trial.key} | {cleanup_exc}")
                    self.results.append(self._summarize(state))
                    self._write_report()
                if self.abort_all.is_set():
                    break
            return 2 if self.abort_all.is_set() else 0
        finally:
            self._write_report()
            self.writer.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolated Steam highest-bid matching and orderbook-lag experiment"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="send the three explicitly bounded real Steam experiments",
    )
    parser.add_argument("--baseline-seconds", type=float, default=60.0)
    parser.add_argument("--post-terminal-seconds", type=float, default=600.0)
    parser.add_argument("--max-terminal-wait-seconds", type=float, default=180.0)
    parser.add_argument(
        "--trials",
        default="late_higher_buy_order,late_buylisting",
        help="comma-separated trial keys; defaults to the two remaining experiments",
    )
    parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stamp = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / stamp
    experiment = SteamMatchingExperiment(
        output_dir=output_dir,
        baseline_seconds=args.baseline_seconds,
        post_terminal_seconds=args.post_terminal_seconds,
        max_terminal_wait_seconds=args.max_terminal_wait_seconds,
        execute=bool(args.execute),
        trial_keys=tuple(key.strip() for key in args.trials.split(",") if key.strip()),
    )
    return experiment.run()


if __name__ == "__main__":
    raise SystemExit(main())
