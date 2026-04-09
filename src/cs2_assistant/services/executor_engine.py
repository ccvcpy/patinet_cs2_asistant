from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cs2_assistant.clients import C5GameClient, ServerChanClient, SteamMarketClient
from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import (
    OP_REBUY_C5,
    OP_SELL_STEAM,
    POOL_STATUS_HOLDING,
    POOL_STATUS_LISTED,
    POOL_STATUS_LISTING_PENDING,
    POOL_STATUS_PENDING_REBUY,
    POOL_STATUS_REBUY_FAILED,
    POOL_STATUS_SOLD,
    STRATEGY_GUADAO,
    STRATEGY_HOLD,
    STRATEGY_TRANSFER,
    StrategyCandidate,
    StrategyConfig,
)
from cs2_assistant.services.executor_buy import RebuyResult, execute_rebuy
from cs2_assistant.services.market import (
    calculate_listing_ratio,
    calculate_steam_after_tax,
    calculate_transfer_real_ratio,
)
from cs2_assistant.services.pricing import PricingDecision, fetch_listing_price
from cs2_assistant.services.strategy import load_strategy_config, scan_strategies
from cs2_assistant.services.t_yield_scan import fetch_all_c5_inventories
from cs2_assistant.utils import utc_now_iso


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _build_note(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _read_note(note: str | None) -> dict[str, Any]:
    if not note:
        return {}
    try:
        data = json.loads(note)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(slots=True)
class ListingDecision:
    list_price: float
    listing_ratio: float
    transfer_real_ratio: float
    pricing: PricingDecision | None


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        config: StrategyConfig | None = None,
        *,
        dry_run_override: bool | None = None,
    ) -> None:
        self.settings = settings
        self.config = config or load_strategy_config(settings)
        if dry_run_override is not None:
            self.config.dry_run = dry_run_override
        if not self.config.execution_enabled:
            self.config.dry_run = True

        self.db = Database(settings.db_path)
        self.db.initialize()
        if not settings.c5_api_key:
            raise RuntimeError("missing C5GAME_API_KEY / C5_API_KEY")
        self.c5_client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
        self.serverchan = (
            ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url)
            if settings.serverchan_sendkey
            else None
        )

        self.steam_client = None
        if self.config.auto_list_enabled:
            if not (settings.steam_cookies and settings.steam_id64):
                raise RuntimeError("missing STEAM_COOKIES / STEAM_ID64 for auto list")
            self.steam_client = SteamMarketClient(
                cookies=settings.steam_cookies,
                steam_id64=settings.steam_id64,
                identity_secret=settings.steam_identity_secret,
                device_id=settings.steam_device_id,
                base_url=settings.steam_market_base_url,
            )

    def close(self) -> None:
        self.db.close()

    def run(self, *, once: bool = False) -> None:
        while True:
            self.run_once()
            if once:
                return
            time.sleep(self.config.cycle_interval_minutes * 60)

    def run_once(self) -> None:
        pool_names = self.db.get_pool_market_hash_names()
        if not pool_names:
            print("底仓为空，跳过执行。")
            return

        self._sync_assets()
        report = scan_strategies(
            self.settings,
            self.config,
            allow_cached_fallback=True,
            cache_max_age_minutes=180,
            pool_market_hash_names=pool_names,
        )
        status_map = {row["market_hash_name"]: row["status"] for row in self.db.list_pool_items()}

        listed = self._execute_listings(report, status_map)
        sold = self._refresh_listings()
        rebought = self._execute_rebuys()

        print(
            f"[{utc_now_iso()}] 执行完成 | 上架 {listed} | 卖出 {sold} | 补仓 {rebought}"
        )

    def _sync_assets(self) -> None:
        inventory_payload = fetch_all_c5_inventories(
            self.c5_client,
            self.settings,
            allow_cached_fallback=True,
            cache_max_age_minutes=180,
        )
        items = list(inventory_payload.get("list") or [])
        self.db.upsert_inventory_assets(items)

    def _decide_listing(self, candidate: StrategyCandidate) -> ListingDecision | None:
        if not self.steam_client:
            return None
        pricing = fetch_listing_price(
            self.steam_client,
            app_id=self.settings.app_id,
            market_hash_name=candidate.market_hash_name,
            wall_min_count=self.config.listing_wall_min_count,
            price_offset=self.config.listing_price_offset,
            min_price=0.01,
            country=self.config.steam_country,
            language=self.config.steam_language,
            currency=self.config.steam_currency,
        )
        list_price = pricing.list_price if pricing else max(0.01, candidate.steam_sell_price)

        steam_after_tax = calculate_steam_after_tax(
            list_price, steam_net_factor=self.config.steam_net_factor
        )
        listing_ratio = calculate_listing_ratio(
            candidate.rebuy_price,
            list_price,
            steam_net_factor=self.config.steam_net_factor,
        )
        transfer_real_ratio = calculate_transfer_real_ratio(
            listing_ratio,
            c5_settlement_factor=self.config.c5_settlement_factor,
            balance_discount=self.config.balance_discount,
        )
        if steam_after_tax is None or listing_ratio is None or transfer_real_ratio is None:
            return None
        return ListingDecision(
            list_price=list_price,
            listing_ratio=listing_ratio,
            transfer_real_ratio=transfer_real_ratio,
            pricing=pricing,
        )

    def _execute_listings(
        self,
        report: Any,
        status_map: dict[str, str],
    ) -> int:
        if not self.config.auto_list_enabled:
            return 0
        if not self.steam_client:
            return 0

        list_count = 0
        candidates: list[StrategyCandidate] = []
        candidates.extend(report.guadao_candidates)
        candidates.extend(report.transfer_candidates)

        for candidate in candidates:
            if list_count >= self.config.max_list_per_cycle:
                break
            if candidate.primary_strategy == STRATEGY_HOLD:
                continue
            status = status_map.get(candidate.market_hash_name, POOL_STATUS_HOLDING)
            if status != POOL_STATUS_HOLDING:
                continue
            if candidate.tradable_count <= 0:
                continue

            asset_row = self.db.pick_tradable_asset(
                candidate.market_hash_name, steam_id=self.settings.steam_id64
            )
            if asset_row is None:
                continue

            decision = self._decide_listing(candidate)
            if decision is None:
                continue
            if candidate.primary_strategy == STRATEGY_GUADAO and decision.listing_ratio > self.config.guadao_max_listing_ratio:
                continue
            if candidate.primary_strategy == STRATEGY_TRANSFER and decision.transfer_real_ratio < self.config.transfer_min_real_ratio:
                continue

            asset_id = asset_row["asset_id"]
            if self.config.dry_run:
                print(
                    f"[dry-run] 上架 {candidate.market_hash_name} asset={asset_id} "
                    f"price={decision.list_price:.2f}"
                )
                list_count += 1
                continue

            payload = self.steam_client.sell_item(
                app_id=self.settings.app_id,
                context_id=self.config.steam_context_id,
                asset_id=asset_id,
                price=decision.list_price,
                quantity=1,
            )
            needs_conf = bool(payload.get("needs_confirmation"))
            listing_id = str(payload.get("listingid") or "")

            if needs_conf and self.settings.steam_identity_secret and self.settings.steam_device_id:
                try:
                    self.steam_client.confirm_all()
                except Exception:
                    pass

            status_after = POOL_STATUS_LISTED if not needs_conf else POOL_STATUS_LISTING_PENDING
            self.db.set_pool_status(candidate.market_hash_name, status_after)
            self.db.set_asset_status(asset_id, "listed")

            note = _build_note(
                {
                    "listingId": listing_id,
                    "rebuyPrice": candidate.rebuy_price,
                    "strategy": candidate.primary_strategy,
                }
            )
            op_id = self.db.add_pool_operation(
                market_hash_name=candidate.market_hash_name,
                strategy=candidate.primary_strategy,
                operation_type=OP_SELL_STEAM,
                expected_price=decision.list_price,
                asset_id=asset_id,
                note=note,
            )
            self.db.update_pool_operation(op_id, status="listed")

            list_count += 1
        return list_count

    def _refresh_listings(self) -> int:
        if not self.steam_client:
            return 0
        active = self.steam_client.list_active_listings()
        active_ids = {listing.listing_id for listing in active}
        now = _now_utc()
        sold_count = 0
        pending_rebuys = {
            row["market_hash_name"]
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=500)
        }

        listed_ops = self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=200)
        for op in listed_ops:
            note = _read_note(op["note"])
            listing_id = str(note.get("listingId") or "")
            if not listing_id:
                continue
            if listing_id in active_ids:
                continue
            created_at = _parse_iso(op["created_at"])
            if created_at and (now - created_at).total_seconds() < self.config.listing_check_interval_minutes * 60:
                continue

            self.db.update_pool_operation(op["id"], status="sold")
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
            if op["asset_id"]:
                self.db.set_asset_status(op["asset_id"], "sold")

            rebuy_price = note.get("rebuyPrice")
            if isinstance(rebuy_price, (int, float)) and rebuy_price > 0:
                if op["market_hash_name"] not in pending_rebuys:
                    self.db.add_pool_operation(
                        market_hash_name=op["market_hash_name"],
                        strategy=op["strategy"],
                        operation_type=OP_REBUY_C5,
                        expected_price=float(rebuy_price),
                        note=_build_note({"sourceListing": listing_id}),
                    )
            sold_count += 1

        return sold_count

    def _execute_rebuys(self) -> int:
        if not self.config.auto_rebuy_enabled:
            return 0

        pending = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=200)
        rebuy_count = 0
        for op in pending:
            if rebuy_count >= self.config.max_buy_per_cycle:
                break
            expected_price = op["expected_price"]
            if expected_price is None:
                continue
            result = execute_rebuy(
                client=self.c5_client,
                market_hash_name=op["market_hash_name"],
                expected_price=float(expected_price),
                app_id=self.settings.app_id,
                tolerance_pct=self.config.price_tolerance_pct,
                dry_run=self.config.dry_run,
            )
            if result.reason == "price_too_high":
                continue
            if result.success and not result.skipped:
                self.db.update_pool_operation(op["id"], status="completed", actual_price=result.actual_price)
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
                rebuy_count += 1
            elif result.skipped:
                self.db.update_pool_operation(op["id"], status="dry_run")
            else:
                self.db.update_pool_operation(op["id"], status="failed")
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_REBUY_FAILED)
        return rebuy_count
