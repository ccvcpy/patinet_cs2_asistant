from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from cs2_assistant.accounts import Account, AccountStore
from cs2_assistant.clients.steam_market import SteamMarketClient, SteamMarketError
from cs2_assistant.config import PROJECT_ROOT, load_settings
from cs2_assistant.diagnostics.steam_matching_experiment import (
    APP_ID,
    CONTEXT_ID,
    CURRENCY_ID,
    FALLBACK_CANDIDATES,
    Candidate,
    JsonlWriter,
    SteamMatchingExperiment,
    beijing_iso,
    orderbook_currency_context_matches,
    parse_orderbook,
    steam_fee_breakdown,
    utc_iso,
)
from cs2_assistant.services.steam_request_scheduler import (
    DirectSteamRequestScheduler,
    get_shared_steam_scheduler,
)


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "steam_sell_orderbook_lag_test"


def compact_order_levels(payload: dict[str, Any], key: str) -> dict[int, int]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = data.get(key) or []
    levels: dict[int, int] = {}
    if isinstance(rows, list) and rows and not isinstance(rows[0], list):
        iterator = (rows[index : index + 2] for index in range(0, len(rows) - 1, 2))
    elif isinstance(rows, list):
        iterator = rows
    else:
        return levels
    for row in iterator:
        if not isinstance(row, list) or len(row) < 2:
            continue
        try:
            price = int(row[0])
            count = int(row[1])
        except (TypeError, ValueError):
            continue
        levels[price] = levels.get(price, 0) + count
    return levels


@dataclass(frozen=True, slots=True)
class ListingPricePlan:
    floor_cents: int
    seller_net_cents: int
    fee_cents: int
    buyer_total_cents: int
    baseline_target_count: int


def choose_high_unique_price(
    *,
    floor_cents: int,
    existing_levels: dict[int, int],
    fee_minimum_cents: int,
    seller_minimum_cents: int,
    max_total_cents: int = 300,
) -> ListingPricePlan:
    desired_total = max(200, floor_cents + 100)
    for seller_net in range(max(1, seller_minimum_cents), max_total_cents + 1):
        steam_fee, publisher_fee, buyer_total = steam_fee_breakdown(
            seller_net,
            fee_minimum_cents=fee_minimum_cents,
        )
        if buyer_total < desired_total or buyer_total > max_total_cents:
            continue
        if existing_levels.get(buyer_total, 0) != 0:
            continue
        return ListingPricePlan(
            floor_cents=floor_cents,
            seller_net_cents=seller_net,
            fee_cents=steam_fee + publisher_fee,
            buyer_total_cents=buyer_total,
            baseline_target_count=0,
        )
    raise ValueError("no unique high sell level is available below the safety cap")


class SellOrderbookLagExperiment:
    def __init__(
        self,
        *,
        output_dir: Path,
        execute: bool,
        baseline_seconds: float,
        active_seconds: float,
        max_appear_seconds: float,
        post_disappear_seconds: float,
        max_post_cancel_seconds: float,
    ) -> None:
        self.settings = load_settings()
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.execute = execute
        self.baseline_seconds = baseline_seconds
        self.active_seconds = active_seconds
        self.max_appear_seconds = max_appear_seconds
        self.post_disappear_seconds = post_disappear_seconds
        self.max_post_cancel_seconds = max_post_cancel_seconds
        self.accounts = AccountStore(PROJECT_ROOT / "config").list_accounts()
        self.account_by_steam = {
            str(account.steam_id64): account for account in self.accounts if account.steam_id64
        }
        self.clients: dict[str, SteamMarketClient] = {}
        self.observer_clients: dict[str, SteamMarketClient] = {}
        self.writer = JsonlWriter(output_dir / "events.jsonl")
        self.stop = threading.Event()
        self.abort = threading.Event()
        self.samples: list[dict[str, Any]] = []
        self.sample_lock = threading.RLock()
        self.threads: list[threading.Thread] = []
        self.errors: list[str] = []
        self.candidate: Candidate | None = None
        self.seller: Account | None = None
        self.observers: tuple[Account, Account] | None = None
        self.plan: ListingPricePlan | None = None
        self.listing_id: str | None = None
        self.submitted_wall: float | None = None
        self.active_wall: float | None = None
        self.first_target_visible_wall: float | None = None
        self.cancel_requested_wall: float | None = None
        self.remote_removed_wall: float | None = None
        self.first_target_absent_wall: float | None = None

    def client(self, account: Account) -> SteamMarketClient:
        client = self.clients.get(account.id)
        if client is not None:
            return client
        if not account.cookies or not account.steam_id64:
            raise RuntimeError(f"account {account.name} lacks Steam credentials")
        client = SteamMarketClient(
            cookies=account.cookies,
            steam_id64=account.steam_id64,
            identity_secret=account.identity_secret,
            device_id=account.device_id,
            account_id=account.id,
            base_url=self.settings.steam_market_base_url,
            request_source="steam_sell_orderbook_lag_experiment",
        )
        self.clients[account.id] = client
        return client

    def observer_client(self, account: Account) -> SteamMarketClient:
        client = self.observer_clients.get(account.id)
        if client is not None:
            return client
        if not account.cookies or not account.steam_id64:
            raise RuntimeError(f"observer account {account.name} lacks Steam credentials")
        client = SteamMarketClient(
            cookies=account.cookies,
            steam_id64=account.steam_id64,
            identity_secret=account.identity_secret,
            device_id=account.device_id,
            account_id=account.id,
            base_url=self.settings.steam_market_base_url,
            request_source="steam_sell_orderbook_lag_observer",
        )
        self.observer_clients[account.id] = client
        return client

    def event(self, kind: str, **fields: Any) -> None:
        self.writer.emit(
            {
                "utc": utc_iso(),
                "beijing": beijing_iso(),
                "monotonic": time.monotonic(),
                "event": kind,
                **fields,
            }
        )

    def _assert_environment_stopped(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", 8765)) == 0:
                raise RuntimeError("port 8765 is listening")
        if not isinstance(get_shared_steam_scheduler(), DirectSteamRequestScheduler):
            raise RuntimeError("shared Steam scheduler is active")
        con = sqlite3.connect(self.settings.db_path)
        try:
            rows = con.execute(
                "SELECT executor_key FROM executor_runtime_state WHERE enabled = 1"
            ).fetchall()
            if rows:
                raise RuntimeError("executor runtime flags are enabled")
        finally:
            con.close()

    def _local_candidate_free(self, candidate: Candidate) -> bool:
        con = sqlite3.connect(self.settings.db_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT tradable, status FROM inventory_assets WHERE asset_id = ?",
                (candidate.asset_id,),
            ).fetchone()
            if row is None or int(row["tradable"] or 0) != 1 or row["status"] != "available":
                return False
            if con.execute(
                "SELECT 1 FROM asset_reservations WHERE asset_id=? AND status IN ('active','consumed')",
                (candidate.asset_id,),
            ).fetchone():
                return False
            if con.execute(
                "SELECT 1 FROM pool_operations WHERE asset_id=? AND status NOT IN ('completed','canceled','failed')",
                (candidate.asset_id,),
            ).fetchone():
                return False
            return True
        finally:
            con.close()

    @staticmethod
    def _remote_candidate_exists(candidate: Candidate, seller: Account) -> bool:
        start_assetid: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"l": "english", "count": 2000}
            if start_assetid:
                params["start_assetid"] = start_assetid
            response = requests.get(
                f"https://steamcommunity.com/inventory/{seller.steam_id64}/{APP_ID}/{CONTEXT_ID}",
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    "public Steam inventory returned non-JSON content"
                ) from exc
            assets = payload.get("assets") or []
            if any(str(row.get("assetid") or "") == candidate.asset_id for row in assets):
                return True
            if not payload.get("more_items"):
                return False
            next_assetid = str(payload.get("last_assetid") or "").strip()
            if not next_assetid or next_assetid == start_assetid:
                raise RuntimeError("public Steam inventory pagination did not advance")
            start_assetid = next_assetid
        raise RuntimeError("public Steam inventory exceeded bounded pagination")

    def _select_candidate(self) -> tuple[Candidate, Account]:
        for candidate in FALLBACK_CANDIDATES:
            seller = self.account_by_steam.get(candidate.seller_steam_id)
            if seller is None or seller.name == "donkzymeng":
                continue
            if not self._local_candidate_free(candidate):
                continue
            if not self._remote_candidate_exists(candidate, seller):
                continue
            payload = self.client(seller).my_listings(count=100)
            if SteamMatchingExperiment._raw_listing_for_asset(payload, candidate.asset_id) is None:
                return candidate, seller
        raise RuntimeError("no safe ordinary low-value candidate is available")

    def _select_observers(self, seller: Account) -> tuple[Account, Account]:
        preferred = ("x6l1cg3cy5o", "ropzx55x", "xiaodigu11", seller.name)
        rows: list[Account] = []
        for name in preferred:
            account = next((row for row in self.accounts if row.name == name), None)
            if account is None or any(row.id == account.id for row in rows):
                continue
            try:
                wallet = self.observer_client(account).wallet_balance(safety_terminal=True)
            except SteamMarketError as exc:
                if exc.status_code == 429:
                    raise
                self.event(
                    "observer_skipped",
                    account=account.name,
                    reason="wallet_currency_unreadable",
                )
                continue
            if int(wallet.get("currency_id") or 0) != CURRENCY_ID:
                self.event(
                    "observer_skipped",
                    account=account.name,
                    reason="wallet_not_cny",
                )
                continue
            rows.append(account)
            if len(rows) == 2:
                break
        if len(rows) != 2:
            raise RuntimeError("two independent CNY observer accounts are unavailable")
        return rows[0], rows[1]

    def _request_orderbook(self, observer: Account) -> dict[str, Any]:
        assert self.candidate is not None
        try:
            return self.observer_client(observer).order_book(
                app_id=APP_ID,
                market_hash_name=self.candidate.market_hash_name,
            )
        except SteamMarketError as exc:
            self.errors.append(f"{observer.name}:{exc}"[:500])
            self.abort.set()
            raise

    def _preflight(self) -> None:
        self.candidate, self.seller = self._select_candidate()
        self.observers = self._select_observers(self.seller)
        wallet = self.client(self.seller).wallet_balance(safety_terminal=True)
        if int(wallet.get("currency_id") or 0) != CURRENCY_ID:
            raise RuntimeError("seller wallet is not CNY")
        raw = wallet.get("raw") if isinstance(wallet.get("raw"), dict) else {}
        market_minimum = int(raw.get("wallet_market_minimum") or 0)
        fee_minimum = int(raw.get("wallet_fee_minimum") or market_minimum or 0)
        left_payload = self._request_orderbook(self.observers[0])
        right_payload = self._request_orderbook(self.observers[1])
        left = parse_orderbook(left_payload)
        right = parse_orderbook(right_payload)
        if not orderbook_currency_context_matches(left, right):
            raise RuntimeError(f"observer currency mismatch: {left} != {right}")
        levels = compact_order_levels(left_payload, "rgCompactSellOrders")
        floor = int(left.get("minSellCents") or 0)
        self.plan = choose_high_unique_price(
            floor_cents=floor,
            existing_levels=levels,
            fee_minimum_cents=fee_minimum,
            seller_minimum_cents=market_minimum,
        )

    def _sample(self, observer: Account) -> None:
        payload = self._request_orderbook(observer)
        parsed = parse_orderbook(payload)
        levels = compact_order_levels(payload, "rgCompactSellOrders")
        assert self.plan is not None
        sample = {
            "observer": observer.name,
            "minSellCents": parsed.get("minSellCents"),
            "minSellCount": parsed.get("minSellCount"),
            "targetPriceCents": self.plan.buyer_total_cents,
            "targetCount": levels.get(self.plan.buyer_total_cents, 0),
        }
        with self.sample_lock:
            self.samples.append({"wall": time.time(), **sample})
            if sample["targetCount"] > self.plan.baseline_target_count:
                if self.first_target_visible_wall is None:
                    self.first_target_visible_wall = time.time()
            elif self.remote_removed_wall is not None and self.first_target_absent_wall is None:
                self.first_target_absent_wall = time.time()
        self.event("orderbook_sample", **sample)

    def _monitor(self, observer: Account, offset: float) -> None:
        if self.stop.wait(offset):
            return
        while not self.stop.is_set() and not self.abort.is_set():
            started = time.monotonic()
            try:
                self._sample(observer)
            except Exception as exc:
                self.errors.append(f"monitor:{observer.name}:{exc}"[:500])
                self.abort.set()
                return
            self.stop.wait(max(0.0, 1.0 - (time.monotonic() - started)))

    def _start_monitors(self) -> None:
        assert self.observers is not None
        self.threads = [
            threading.Thread(target=self._monitor, args=(self.observers[0], 0.0), daemon=True),
            threading.Thread(target=self._monitor, args=(self.observers[1], 0.5), daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def _stop_monitors(self) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=10)

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.abort.is_set():
                raise RuntimeError("monitor aborted")
            time.sleep(0.1)

    def _active_listing(self) -> tuple[str | None, int | None]:
        assert self.seller is not None and self.candidate is not None
        payload = self.client(self.seller).my_listings(count=100)
        row = SteamMatchingExperiment._raw_listing_for_asset(payload, self.candidate.asset_id)
        listing_id, subtotal, fee = SteamMatchingExperiment._listing_identity(row)
        total = subtotal + fee if subtotal is not None and fee is not None else None
        return listing_id, total

    def _submit(self) -> None:
        assert self.seller is not None and self.candidate is not None and self.plan is not None
        synthetic_gross = self.plan.seller_net_cents / (100.0 * 0.869)
        result = self.client(self.seller).sell_item(
            app_id=APP_ID,
            context_id=CONTEXT_ID,
            asset_id=self.candidate.asset_id,
            price=synthetic_gross,
            quantity=1,
            steam_net_factor=0.869,
        )
        self.listing_id = str(result.get("listingid") or "").strip() or None
        self.submitted_wall = time.time()
        self.event(
            "sell_submitted",
            seller=self.seller.name,
            assetId=self.candidate.asset_id,
            listingId=self.listing_id,
            sellerNetCents=self.plan.seller_net_cents,
            buyerTotalCents=self.plan.buyer_total_cents,
        )
        confirmed = self.client(self.seller).confirm_listing_assets(
            asset_ids=[self.candidate.asset_id],
            listing_ids=[self.listing_id] if self.listing_id else None,
        )
        self.event("listing_confirmation", confirmedCount=int(confirmed))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            listing_id, total = self._active_listing()
            if listing_id:
                if total != self.plan.buyer_total_cents:
                    raise RuntimeError(
                        f"active listing total {total} != target {self.plan.buyer_total_cents}"
                    )
                self.listing_id = listing_id
                self.active_wall = time.time()
                self.event("listing_active", listingId=listing_id, buyerTotalCents=total)
                return
            time.sleep(0.5)
        raise RuntimeError("listing did not become remotely active")

    def _cancel(self) -> None:
        assert self.seller is not None and self.listing_id
        self.cancel_requested_wall = time.time()
        self.event("cancel_requested", listingId=self.listing_id)
        if not self.client(self.seller).remove_listing(self.listing_id):
            raise RuntimeError("Steam remove listing returned false")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            listing_id, _ = self._active_listing()
            if not listing_id:
                self.remote_removed_wall = time.time()
                self.event("listing_remote_removed", listingId=self.listing_id)
                return
            time.sleep(0.5)
        raise RuntimeError("listing remains active after cancellation")

    def _wait_for_appearance(self) -> bool:
        deadline = time.monotonic() + self.max_appear_seconds
        while time.monotonic() < deadline:
            if self.abort.is_set():
                raise RuntimeError("monitor aborted before target appeared")
            if self.first_target_visible_wall is not None:
                return True
            time.sleep(0.1)
        return False

    def _observe_after_cancel(self) -> None:
        deadline = time.monotonic() + self.max_post_cancel_seconds
        absence_started: float | None = None
        assert self.plan is not None
        while time.monotonic() < deadline:
            if self.abort.is_set():
                raise RuntimeError("monitor aborted after cancellation")
            with self.sample_lock:
                latest = self.samples[-1] if self.samples else None
            absent = bool(latest and latest.get("targetCount") == self.plan.baseline_target_count)
            if absent:
                absence_started = absence_started or time.monotonic()
                if time.monotonic() - absence_started >= self.post_disappear_seconds:
                    return
            else:
                absence_started = None
            time.sleep(0.1)

    @staticmethod
    def _delta(later: float | None, earlier: float | None) -> float | None:
        if later is None or earlier is None:
            return None
        return round(max(0.0, later - earlier), 3)

    def _summary(self) -> dict[str, Any]:
        return {
            "generatedAt": utc_iso(),
            "execute": self.execute,
            "candidate": asdict(self.candidate) if self.candidate else None,
            "seller": self.seller.name if self.seller else None,
            "observers": [row.name for row in self.observers] if self.observers else [],
            "price": asdict(self.plan) if self.plan else None,
            "listingId": self.listing_id,
            "sampleCount": len(self.samples),
            "timingSeconds": {
                "submitToRemoteActive": self._delta(self.active_wall, self.submitted_wall),
                "submitToOrderbookVisible": self._delta(
                    self.first_target_visible_wall, self.submitted_wall
                ),
                "cancelToRemoteRemoved": self._delta(
                    self.remote_removed_wall, self.cancel_requested_wall
                ),
                "cancelToOrderbookAbsent": self._delta(
                    self.first_target_absent_wall, self.cancel_requested_wall
                ),
                "remoteRemovedToOrderbookAbsent": self._delta(
                    self.first_target_absent_wall, self.remote_removed_wall
                ),
            },
            "targetEverVisible": self.first_target_visible_wall is not None,
            "errors": self.errors,
            "aborted": self.abort.is_set() or bool(self.errors),
        }

    def _write_outputs(self) -> None:
        summary = self._summary()
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        timing = summary["timingSeconds"]
        lines = [
            "# Steam 卖盘档位传播与撤销实验",
            "",
            f"- 物品：{(summary.get('candidate') or {}).get('market_hash_name') or '-'}",
            f"- 目标卖价：¥{((summary.get('price') or {}).get('buyer_total_cents') or 0) / 100:.2f}",
            f"- listingId：{summary.get('listingId') or '-'}",
            f"- orderbook 是否出现目标档位：{'是' if summary.get('targetEverVisible') else '否'}",
            f"- 提交到远端活跃：{timing.get('submitToRemoteActive')} 秒",
            f"- 提交到档位出现：{timing.get('submitToOrderbookVisible')} 秒",
            f"- 撤单到远端消失：{timing.get('cancelToRemoteRemoved')} 秒",
            f"- 撤单到档位消失：{timing.get('cancelToOrderbookAbsent')} 秒",
            f"- 远端确认撤除到档位消失：{timing.get('remoteRemovedToOrderbookAbsent')} 秒",
            f"- 样本数：{summary.get('sampleCount')}",
            f"- 错误：{'; '.join(summary.get('errors') or []) or '-'}",
        ]
        (self.output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    def run(self) -> int:
        self.writer.start()
        try:
            self._assert_environment_stopped()
            self._preflight()
            assert self.candidate and self.seller and self.observers and self.plan
            print(
                f"[预检] {self.candidate.market_hash_name} | seller={self.seller.name} | "
                f"observers={self.observers[0].name},{self.observers[1].name} | "
                f"floor={self.plan.floor_cents}分 target={self.plan.buyer_total_cents}分"
            )
            if not self.execute:
                print("[预演完成] 未发送上架或撤单请求")
                return 0
            self._start_monitors()
            self._wait(self.baseline_seconds)
            with self.sample_lock:
                if any(row.get("targetCount") != 0 for row in self.samples):
                    raise RuntimeError("target sell level appeared during baseline")
            self._submit()
            appeared = self._wait_for_appearance()
            print(f"[卖盘档位] {'已出现' if appeared else '观察期内未出现'}")
            if appeared:
                self._wait(self.active_seconds)
            self._cancel()
            self._observe_after_cancel()
            print("[完成] 远端挂单已撤销，目标档位已完成撤销后观察")
            return 0
        except Exception as exc:
            self.errors.append(str(exc)[:500])
            self.abort.set()
            print(f"[中止] {exc}")
            return 2
        finally:
            if self.execute and self.seller and self.listing_id:
                try:
                    active_id, _ = self._active_listing()
                    if active_id:
                        if self.client(self.seller).remove_listing(active_id):
                            self.event("cleanup_removed_listing", listingId=active_id)
                except Exception as exc:
                    self.errors.append(f"cleanup:{exc}"[:500])
            self._stop_monitors()
            self._write_outputs()
            self.writer.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe a unique non-floor Steam sell level")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--baseline-seconds", type=float, default=15.0)
    parser.add_argument("--active-seconds", type=float, default=30.0)
    parser.add_argument("--max-appear-seconds", type=float, default=120.0)
    parser.add_argument("--post-disappear-seconds", type=float, default=60.0)
    parser.add_argument("--max-post-cancel-seconds", type=float, default=600.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()
    return SellOrderbookLagExperiment(
        output_dir=args.output_dir,
        execute=args.execute,
        baseline_seconds=max(1.0, args.baseline_seconds),
        active_seconds=max(0.0, args.active_seconds),
        max_appear_seconds=max(5.0, args.max_appear_seconds),
        post_disappear_seconds=max(5.0, args.post_disappear_seconds),
        max_post_cancel_seconds=max(30.0, args.max_post_cancel_seconds),
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
