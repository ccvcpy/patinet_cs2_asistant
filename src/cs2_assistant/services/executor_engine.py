from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cs2_assistant.clients import (
    C5GameClient,
    C5GameError,
    ServerChanClient,
    SteamMarketClient,
    SteamMarketError,
)
from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import (
    OP_REBUY_C5,
    OP_SELL_STEAM,
    OP_TRANSFER_BUY,
    OP_TRANSFER_SELL,
    POOL_STATUS_HOLDING,
    POOL_STATUS_LISTED,
    POOL_STATUS_LISTING_PENDING,
    POOL_STATUS_PENDING_REBUY,
    POOL_STATUS_REBUY_FAILED,
    POOL_STATUS_TRANSFER_BUYING,
    POOL_STATUS_TRANSFER_HOLDING,
    POOL_STATUS_TRANSFER_LISTED_C5,
    POOL_STATUS_TRANSFER_SOLD,
    STRATEGY_GUADAO,
    STRATEGY_TRANSFER,
    StrategyCandidate,
    StrategyConfig,
)
from cs2_assistant.services.executor_buy import execute_rebuy
from cs2_assistant.services.market import calculate_listing_ratio, calculate_transfer_real_ratio
from cs2_assistant.services.pricing import PricingDecision, fetch_listing_price
from cs2_assistant.services.strategy import load_strategy_config, scan_strategies
from cs2_assistant.services.t_yield_scan import fetch_all_c5_inventories
from cs2_assistant.utils import safe_float, safe_int, utc_now_iso


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


@dataclass(slots=True)
class SteamBuyTarget:
    listing_id: str
    subtotal: int
    fee: int
    total: int


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        config: StrategyConfig | None = None,
        *,
        dry_run_override: bool | None = None,
        force_refresh_override: bool | None = None,
    ) -> None:
        self.settings = settings
        self.config = config or load_strategy_config(settings)
        if dry_run_override is not None:
            self.config.dry_run = dry_run_override
        if force_refresh_override is not None:
            self.config.force_refresh_before_execution = force_refresh_override
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
        if self.config.execution_enabled or self.config.auto_list_enabled:
            if not settings.steam_cookies:
                raise RuntimeError("missing STEAM_COOKIES for auto execution")
            self.steam_client = SteamMarketClient(
                cookies=settings.steam_cookies,
                steam_id64=None,
                identity_secret=settings.steam_identity_secret,
                device_id=settings.steam_device_id,
                base_url=settings.steam_market_base_url,
            )

        self._last_inventory_payload: dict[str, Any] = {}
        self._inventory_items_by_asset_id: dict[str, dict[str, Any]] = {}
        self._pending_confirmation_count = 0

        if (
            self.config.execution_enabled
            and not self.config.dry_run
            and self.config.auto_list_enabled
            and (not settings.steam_identity_secret or not settings.steam_device_id)
        ):
            print(
                "[提醒] 未配置 `STEAM_IDENTITY_SECRET` 或 `STEAM_DEVICE_ID`，"
                "需要 Steam Guard 确认的挂单将保持待确认状态。"
            )

    def close(self) -> None:
        self.db.close()

    def run(self, *, once: bool = False) -> None:
        while True:
            self.run_once(wait_for_cycle=True)
            if once:
                return
            time.sleep(self.config.cycle_interval_minutes * 60)

    def run_once(self, *, wait_for_cycle: bool = True) -> None:
        self._pending_confirmation_count = 0
        pool_names = self.db.get_pool_market_hash_names()
        if not pool_names:
            print("底仓为空，跳过执行。")
            return

        self._sync_assets()
        self._refresh_transfer_holdings()
        report = scan_strategies(
            self.settings,
            self.config,
            allow_cached_fallback=True,
            cache_max_age_minutes=180,
            pool_market_hash_names=pool_names,
        )
        self._print_scan_summary(report)

        listed, sold, rebought = self._run_guadao_cycle(report, wait_for_cycle=wait_for_cycle)
        if self._has_open_guadao_cycle():
            print("[等待] 挂刀循环尚未闭环，先跳过本轮导余额执行。")
            transfer_bought = 0
            transfer_listed = 0
            transfer_sold = 0
        else:
            transfer_bought = self._execute_transfer_buys(report, self.db.get_pool_status_map())
            transfer_listed = self._execute_transfer_sells()
            transfer_sold = self._refresh_transfer_sales()

        print(
            f"[{utc_now_iso()}] 执行完成 | 挂刀上架 {listed} | Steam卖出 {sold} | "
            f"C5补仓 {rebought} | 导余额买入 {transfer_bought} | "
            f"导余额上架C5 {transfer_listed} | 导余额卖出 {transfer_sold}"
        )
        self._print_run_result(
            report,
            pool_names=pool_names,
            listed=listed,
            sold=sold,
            rebought=rebought,
            transfer_bought=transfer_bought,
            transfer_listed=transfer_listed,
            transfer_sold=transfer_sold,
        )
        if self._pending_confirmation_count > 0:
            print(
                f"[提醒] {self._pending_confirmation_count} 件物品待 Steam Guard 确认，"
                "请运行: python main.py steam confirm"
            )

    def _print_scan_summary(self, report: Any) -> None:
        evaluated_count = len(getattr(report, "all_evaluated", []) or [])
        print(
            f"[扫描] 底仓池 {getattr(report, 'total_pool_types', 0)} 个品种 | "
            f"进入评估 {evaluated_count} 个 | "
            f"缺价 {getattr(report, 'missing_price_count', 0)} 个 | "
            f"挂刀候选 {getattr(report, 'guadao_count', 0)} 个 | "
            f"导余额候选 {getattr(report, 'transfer_count', 0)} 个"
        )

    def _print_run_result(
        self,
        report: Any,
        *,
        pool_names: list[str],
        listed: int,
        sold: int,
        rebought: int,
        transfer_bought: int,
        transfer_listed: int,
        transfer_sold: int,
    ) -> None:
        total_actions = listed + sold + rebought + transfer_bought + transfer_listed + transfer_sold
        if total_actions > 0:
            print(f"[结果] 本轮已执行 {total_actions} 个实际动作。")
            return

        print("[结果] 本轮只完成了扫描/状态检查，没有实际上架、买入或卖出。")
        reasons = self._describe_no_action_reasons(report, pool_names=pool_names)
        if not reasons:
            reasons = ["未命中可执行条件。"]
        for reason in reasons:
            print(f"[原因] {reason}")

    def _describe_no_action_reasons(self, report: Any, *, pool_names: list[str]) -> list[str]:
        reasons: list[str] = []
        open_statuses = self._get_open_guadao_statuses()
        if open_statuses:
            summary = ", ".join(
                f"{status}={count}" for status, count in sorted(open_statuses.items())
            )
            reasons.append(f"上一轮挂刀循环未闭环（{summary}），本轮仅做等待和状态检查。")

        inventory_type_names = self._current_inventory_type_names()
        missing_inventory_names = sorted(
            [
            name for name in pool_names
            if name not in inventory_type_names
            ]
        )
        if missing_inventory_names:
            sample = "、".join(missing_inventory_names[:3])
            suffix = " 等" if len(missing_inventory_names) > 3 else ""
            reasons.append(
                f"底仓池中 {len(missing_inventory_names)} 个品种当前真实库存里不存在，"
                f"未进入执行：{sample}{suffix}"
            )

        missing_price_count = int(getattr(report, "missing_price_count", 0) or 0)
        if missing_price_count > 0:
            reasons.append(f"{missing_price_count} 个品种缺少价格，无法完成策略评估。")

        evaluated = list(getattr(report, "all_evaluated", []) or [])
        guadao_candidates = list(getattr(report, "guadao_candidates", []) or [])
        transfer_candidates = list(getattr(report, "transfer_candidates", []) or [])
        if evaluated and not guadao_candidates and not transfer_candidates:
            reasons.append(f"已评估 {len(evaluated)} 个品种，但都未满足 list/transfer 阈值。")

        if guadao_candidates and all(int(candidate.tradable_count) <= 0 for candidate in guadao_candidates):
            reasons.append("存在挂刀候选，但当前没有可交易库存，无法上架。")

        if guadao_candidates and self.config.max_list_per_cycle <= 0:
            reasons.append("本轮 `max-list=0`，已禁用新的 Steam 上架。")

        if transfer_candidates and self.config.max_buy_per_cycle <= 0:
            reasons.append("本轮 `max-buy=0`，已禁用买入动作。")

        return reasons

    def _current_inventory_type_names(self) -> set[str]:
        names: set[str] = set()
        for item in list(self._last_inventory_payload.get("list") or []):
            if not isinstance(item, dict):
                continue
            market_hash_name = str(item.get("marketHashName") or "").strip()
            if market_hash_name:
                names.add(market_hash_name)
        return names

    def _run_guadao_cycle(self, report: Any, *, wait_for_cycle: bool) -> tuple[int, int, int]:
        status_map = self.db.get_pool_status_map()
        listed = 0
        sold = 0
        rebought = 0

        if self._has_open_guadao_cycle(status_map):
            print("[等待] 检测到上一轮挂刀循环未闭环，本轮先等待卖出/补仓完成。")
        else:
            listed = self._execute_guadao_listings(report, status_map)

        self._backfill_listing_ids()
        sold_delta, rebought_delta = self._advance_guadao_cycle()
        sold += sold_delta
        rebought += rebought_delta

        if wait_for_cycle and self._has_open_guadao_cycle():
            waited_sold, waited_rebought = self._wait_for_guadao_cycle_close()
            sold += waited_sold
            rebought += waited_rebought

        return listed, sold, rebought

    def _advance_guadao_cycle(self) -> tuple[int, int]:
        self._refresh_pending_listing_confirmations()
        sold = self._refresh_listings()
        rebought = self._execute_rebuys()
        return sold, rebought

    def _wait_for_guadao_cycle_close(self) -> tuple[int, int]:
        sold = 0
        rebought = 0
        while self._has_open_guadao_cycle():
            wait_seconds = self._guadao_wait_seconds()
            open_statuses = self._get_open_guadao_statuses()
            if open_statuses and set(open_statuses) == {POOL_STATUS_REBUY_FAILED}:
                print("[停止] 挂刀循环卡在补仓失败状态，需人工处理后才能开启下一轮。")
                return sold, rebought
            status_summary = ", ".join(
                f"{status}={count}" for status, count in sorted(open_statuses.items())
            ) or "unknown"
            print(
                f"[等待] 挂刀循环未完成（{status_summary}），"
                f"{int(wait_seconds)} 秒后继续检查。"
            )
            time.sleep(wait_seconds)
            self._sync_assets()
            self._backfill_listing_ids()
            sold_delta, rebought_delta = self._advance_guadao_cycle()
            sold += sold_delta
            rebought += rebought_delta
        return sold, rebought

    def _guadao_wait_seconds(self) -> float:
        return max(1.0, float(self.config.listing_check_interval_minutes) * 60.0)

    def _get_open_guadao_statuses(self) -> dict[str, int]:
        open_statuses = {
            POOL_STATUS_LISTING_PENDING,
            POOL_STATUS_LISTED,
            POOL_STATUS_PENDING_REBUY,
            POOL_STATUS_REBUY_FAILED,
        }
        counts: dict[str, int] = {}
        for status in self.db.get_pool_status_map().values():
            if status not in open_statuses:
                continue
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _has_open_guadao_cycle(self, status_map: dict[str, str] | None = None) -> bool:
        open_statuses = {
            POOL_STATUS_LISTING_PENDING,
            POOL_STATUS_LISTED,
            POOL_STATUS_PENDING_REBUY,
            POOL_STATUS_REBUY_FAILED,
        }
        current_status_map = status_map or self.db.get_pool_status_map()
        return any(status in open_statuses for status in current_status_map.values())

    def _sync_assets(self) -> None:
        inventory_payload = fetch_all_c5_inventories(
            self.c5_client,
            self.settings,
            allow_cached_fallback=True,
            cache_max_age_minutes=180,
        )
        items = list(inventory_payload.get("list") or [])
        self._last_inventory_payload = dict(inventory_payload)
        self._inventory_items_by_asset_id = {
            str(item.get("assetId")): dict(item)
            for item in items
            if isinstance(item, dict) and str(item.get("assetId") or "").strip()
        }
        self.db.upsert_inventory_assets(items)
        self._reconcile_transfer_buys()

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
            force_refresh=False,
            cache_ttl=self.config.steam_price_cache_ttl,
        )
        if pricing is None:
            return None
        return self._decision_from_list_price(candidate, pricing.list_price, pricing=pricing)

    def _decision_from_prices(
        self,
        *,
        rebuy_price: float,
        list_price: float,
        pricing: PricingDecision | None,
    ) -> ListingDecision | None:
        listing_ratio = calculate_listing_ratio(
            rebuy_price,
            list_price,
            steam_net_factor=self.config.steam_net_factor,
        )
        transfer_real_ratio = calculate_transfer_real_ratio(
            listing_ratio,
            c5_settlement_factor=self.config.c5_settlement_factor,
            balance_discount=self.config.balance_discount,
        )
        if listing_ratio is None or transfer_real_ratio is None:
            return None
        return ListingDecision(
            list_price=list_price,
            listing_ratio=listing_ratio,
            transfer_real_ratio=transfer_real_ratio,
            pricing=pricing,
        )

    def _decision_from_list_price(
        self,
        candidate: StrategyCandidate,
        list_price: float,
        *,
        pricing: PricingDecision | None,
    ) -> ListingDecision | None:
        return self._decision_from_prices(
            rebuy_price=float(candidate.rebuy_price),
            list_price=list_price,
            pricing=pricing,
        )

    def _execute_listings(self, report: Any, status_map: dict[str, str]) -> int:
        return self._execute_guadao_listings(report, status_map)

    def _can_execute_guadao(self, pool_status: str | None) -> bool:
        blocked_statuses = {
            POOL_STATUS_TRANSFER_BUYING,
            POOL_STATUS_TRANSFER_HOLDING,
            POOL_STATUS_TRANSFER_LISTED_C5,
            POOL_STATUS_TRANSFER_SOLD,
        }
        return (pool_status or POOL_STATUS_HOLDING) not in blocked_statuses

    def _execute_guadao_listings(self, report: Any, status_map: dict[str, str]) -> int:
        if not self.config.auto_list_enabled or not self.steam_client:
            return 0
        if self._has_open_guadao_cycle(status_map):
            return 0

        list_count = 0
        picked_asset_ids: set[str] = set()
        candidates = [
            candidate
            for candidate in report.guadao_candidates
            if candidate.primary_strategy == STRATEGY_GUADAO
        ]

        for candidate in candidates:
            if list_count >= self.config.max_list_per_cycle:
                break
            if not self._can_execute_guadao(status_map.get(candidate.market_hash_name)):
                continue
            if candidate.tradable_count <= 0:
                continue

            decision = self._decide_listing(candidate)
            if decision is None:
                continue
            if self.config.force_refresh_before_execution:
                final_pricing = fetch_listing_price(
                    self.steam_client,
                    app_id=self.settings.app_id,
                    market_hash_name=candidate.market_hash_name,
                    wall_min_count=self.config.listing_wall_min_count,
                    price_offset=self.config.listing_price_offset,
                    min_price=0.01,
                    country=self.config.steam_country,
                    language=self.config.steam_language,
                    currency=self.config.steam_currency,
                    force_refresh=True,
                    cache_ttl=self.config.steam_price_cache_ttl,
                )
                if final_pricing is None:
                    self._notify_skip(candidate.market_hash_name, "steam_price_unavailable", {})
                    continue
                decision = self._decision_from_list_price(
                    candidate,
                    final_pricing.list_price,
                    pricing=final_pricing,
                )
                if decision is None:
                    continue
            if decision.listing_ratio > self.config.guadao_max_listing_ratio:
                continue

            steam_id = self.steam_client.steam_id64
            while list_count < self.config.max_list_per_cycle:
                asset_row = self.db.pick_tradable_asset(
                    candidate.market_hash_name,
                    steam_id=steam_id,
                    exclude_asset_ids=picked_asset_ids,
                )
                if asset_row is None:
                    break

                asset_id = asset_row["asset_id"]
                picked_asset_ids.add(asset_id)
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
                listing_id = str(payload.get("listingid") or "")
                confirmation_note: dict[str, Any] = {
                    "needsConfirmation": True,
                    "confirmationStatus": "pending",
                }
                status_after = POOL_STATUS_LISTED
                confirmation_note, status_after = self._handle_listing_confirmation(
                    market_hash_name=candidate.market_hash_name,
                    asset_id=asset_id,
                    listing_id=listing_id,
                )

                self.db.set_pool_status(candidate.market_hash_name, status_after)
                self.db.set_asset_status(asset_id, "listed")
                status_map[candidate.market_hash_name] = status_after

                note = _build_note(
                    {
                        "listingId": listing_id,
                        "rebuyPrice": candidate.rebuy_price,
                        "steamListPrice": decision.list_price,
                        "strategy": candidate.primary_strategy,
                        **confirmation_note,
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
                print(
                    f"[上架] {candidate.market_hash_name} | "
                    f"asset={asset_id} | "
                    f"Steam挂价 ¥{decision.list_price:.2f} | "
                    f"预计到手 ¥{decision.list_price * 0.869:.2f}"
                )
                list_count += 1

        return list_count

    def _refresh_pending_listing_confirmations(self) -> int:
        if not self.steam_client:
            return 0
        try:
            active = self.steam_client.list_active_listings()
        except Exception as exc:
            print(f"[警告] 获取 Steam 挂单列表失败: {exc}")
            return 0
        active_listing_ids = {lst.listing_id for lst in active if lst.listing_id}
        updated = 0
        for pool_row in self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING):
            market_hash_name = pool_row["market_hash_name"]
            listed_ops = self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=200)
            target_op = None
            for op in listed_ops:
                if op["market_hash_name"] != market_hash_name:
                    continue
                note = _read_note(op["note"])
                listing_id = str(note.get("listingId") or "").strip()
                if listing_id and listing_id in active_listing_ids:
                    target_op = op
                    break
            if target_op is None:
                continue
            note = _read_note(target_op["note"])
            note["confirmationStatus"] = "confirmed_late"
            note["confirmationRecoveredAt"] = utc_now_iso()
            self.db.update_pool_operation(target_op["id"], note=_build_note(note))
            self.db.set_pool_status(market_hash_name, POOL_STATUS_LISTED)
            updated += 1
        return updated

    def _backfill_listing_ids(self) -> int:
        """上架确认后，Steam 才分配 listing_id。
        此方法查询当前活跃挂单，按 asset_id 匹配，把真实 listing_id 回填到 DB。
        """
        if not self.steam_client:
            return 0
        ops = self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=200)
        empty_ops = [
            op for op in ops
            if not _read_note(op["note"]).get("listingId") and op["asset_id"]
        ]
        if not empty_ops:
            return 0
        try:
            active_listings = self.steam_client.list_active_listings()
        except Exception:
            return 0
        asset_to_lid = {
            lst.asset_id: lst.listing_id
            for lst in active_listings
            if lst.asset_id and lst.listing_id
        }
        updated = 0
        for op in empty_ops:
            lid = asset_to_lid.get(op["asset_id"])
            if not lid:
                continue
            note = _read_note(op["note"])
            note["listingId"] = lid
            self.db.update_pool_operation(op["id"], note=_build_note(note))
            updated += 1
        return updated

    def _handle_listing_confirmation(
        self,
        *,
        market_hash_name: str,
        asset_id: str,
        listing_id: str,
    ) -> tuple[dict[str, Any], str]:
        note: dict[str, Any] = {
            "needsConfirmation": True,
            "confirmationStatus": "pending",
        }
        if not self.settings.steam_identity_secret or not self.settings.steam_device_id:
            note["confirmationStatus"] = "manual_required"
            note["confirmationMessage"] = "missing STEAM_IDENTITY_SECRET or STEAM_DEVICE_ID"
            self._pending_confirmation_count += 1
            self._notify_listing_confirmation_required(
                market_hash_name,
                asset_id=asset_id,
                listing_id=listing_id,
                reason="missing_credentials",
            )
            return note, POOL_STATUS_LISTING_PENDING

        try:
            confirmed_count = self.steam_client.confirm_all()
        except Exception as exc:
            note["confirmationStatus"] = "failed"
            note["confirmationMessage"] = str(exc)
            self._pending_confirmation_count += 1
            print(
                f"[提醒] Steam Guard 自动确认失败 | {market_hash_name} | "
                f"asset={asset_id} | listing={listing_id or '-'} | error={exc}"
            )
            self._notify_listing_confirmation_required(
                market_hash_name,
                asset_id=asset_id,
                listing_id=listing_id,
                reason=f"confirm_failed: {exc}",
            )
            return note, POOL_STATUS_LISTING_PENDING

        if confirmed_count <= 0:
            note["confirmationStatus"] = "not_found"
            note["confirmationMessage"] = "no pending Steam Guard confirmation found"
            self._pending_confirmation_count += 1
            self._notify_listing_confirmation_required(
                market_hash_name,
                asset_id=asset_id,
                listing_id=listing_id,
                reason="confirm_not_found",
            )
            return note, POOL_STATUS_LISTING_PENDING

        note["confirmationStatus"] = "confirmed"
        note["confirmationCount"] = confirmed_count
        return note, POOL_STATUS_LISTED

    def _notify_listing_confirmation_required(
        self,
        market_hash_name: str,
        *,
        asset_id: str,
        listing_id: str,
        reason: str,
    ) -> None:
        print(
            f"[提醒] 挂单待手动确认 | {market_hash_name} | asset={asset_id} | "
            f"listing={listing_id or '-'} | reason={reason}"
        )
        if not self.serverchan:
            return
        try:
            self.serverchan.send(
                f"[steam confirm] {market_hash_name}",
                (
                    f"{market_hash_name}\n\n"
                    f"- assetId: {asset_id}\n"
                    f"- listingId: {listing_id or '-'}\n"
                    f"- 状态: 待 Steam Guard 确认\n"
                    f"- 原因: {reason}\n\n"
                    "请运行: `python main.py steam confirm`"
                ),
            )
        except Exception as exc:
            print(f"  ServerChan 推送失败: {exc}")

    def _execute_transfer_buys(self, report: Any, status_map: dict[str, str]) -> int:
        if not self.steam_client:
            return 0

        buy_count = 0
        candidates = [
            candidate
            for candidate in report.transfer_candidates
            if candidate.primary_strategy == STRATEGY_TRANSFER
        ]
        for candidate in candidates:
            if buy_count >= self.config.max_buy_per_cycle:
                break
            if status_map.get(candidate.market_hash_name, POOL_STATUS_HOLDING) != POOL_STATUS_HOLDING:
                continue
            if self._execute_transfer_buy(candidate):
                buy_count += 1
                if not self.config.dry_run:
                    status_map[candidate.market_hash_name] = POOL_STATUS_TRANSFER_BUYING
        return buy_count

    def _find_transfer_sell_asset(
        self,
        market_hash_name: str,
    ) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        if not self.steam_client:
            return None, None
        for asset_row in self.db.list_assets(
            market_hash_name=market_hash_name,
            steam_id=self.steam_client.steam_id64,
            tradable=True,
            status="available",
        ):
            asset_id = str(asset_row["asset_id"])
            inventory_item = self._inventory_items_by_asset_id.get(asset_id)
            if not inventory_item:
                continue
            if not self._is_inventory_item_tradable(inventory_item):
                continue
            if not str(inventory_item.get("token") or "").strip():
                continue
            if not str(inventory_item.get("styleToken") or "").strip():
                continue
            return asset_id, inventory_item
        return None, None

    def _execute_transfer_buy(self, candidate: StrategyCandidate) -> bool:
        if not self.steam_client:
            return False
        sell_asset_id, sell_inventory_item = self._find_transfer_sell_asset(candidate.market_hash_name)
        if not sell_asset_id or not sell_inventory_item:
            self._notify_skip(candidate.market_hash_name, "no_tradable_asset", {})
            return False

        current_c5_sale_price = self._resolve_transfer_sale_price(
            candidate.market_hash_name,
            sell_inventory_item,
            {"targetC5Price": candidate.rebuy_price},
        )
        if current_c5_sale_price is None or current_c5_sale_price <= 0:
            self._notify_skip(candidate.market_hash_name, "c5_price_unavailable", {})
            return False
        try:
            item_nameid = self.steam_client.get_item_nameid(
                app_id=self.settings.app_id,
                market_hash_name=candidate.market_hash_name,
            )
            histogram = self.steam_client.item_orders_histogram(
                item_nameid=item_nameid,
                country=self.config.steam_country,
                language=self.config.steam_language,
                currency=self.config.steam_currency,
            )
        except SteamMarketError:
            self._notify_skip(candidate.market_hash_name, "steam_price_unavailable", {})
            return False

        lowest_sell_order = safe_int(histogram.get("lowest_sell_order"))
        if lowest_sell_order is None or lowest_sell_order <= 0:
            self._notify_skip(candidate.market_hash_name, "steam_price_unavailable", histogram)
            return False

        decision = self._decision_from_prices(
            rebuy_price=float(current_c5_sale_price),
            list_price=lowest_sell_order / 100.0,
            pricing=None,
        )
        if decision is None or decision.transfer_real_ratio < self.config.transfer_min_real_ratio:
            self._notify_skip(candidate.market_hash_name, "ratio_no_longer_profitable", histogram)
            return False

        try:
            listings_payload = self.steam_client.search_listings(
                app_id=self.settings.app_id,
                market_hash_name=candidate.market_hash_name,
                start=0,
                count=10,
            )
        except SteamMarketError:
            self._notify_skip(candidate.market_hash_name, "steam_price_unavailable", {})
            return False

        buy_target = self._pick_lowest_steam_listing(listings_payload)
        if buy_target is None:
            self._notify_skip(candidate.market_hash_name, "steam_price_unavailable", listings_payload)
            return False

        decision = self._decision_from_prices(
            rebuy_price=float(current_c5_sale_price),
            list_price=buy_target.total / 100.0,
            pricing=None,
        )
        if decision is None or decision.transfer_real_ratio < self.config.transfer_min_real_ratio:
            self._notify_skip(
                candidate.market_hash_name,
                "ratio_no_longer_profitable",
                {
                    "histogramTotal": round(lowest_sell_order / 100.0, 2),
                    "listingTotal": round(buy_target.total / 100.0, 2),
                },
            )
            return False

        note_payload = {
            "listingId": buy_target.listing_id,
            "subtotal": buy_target.subtotal,
            "fee": buy_target.fee,
            "total": buy_target.total,
            "steamBuyPrice": round(buy_target.total / 100.0, 2),
            "targetC5Price": float(current_c5_sale_price),
            "transferRatio": round(decision.transfer_real_ratio, 4),
            "steamId": self.steam_client.steam_id64,
            "sellAssetId": sell_asset_id,
            "sellAssetSteamId": str(sell_inventory_item.get("steamId") or ""),
            "sellAssetToken": str(sell_inventory_item.get("token") or ""),
            "sellAssetStyleToken": str(sell_inventory_item.get("styleToken") or ""),
            "beforeAssetIds": self.db.list_asset_ids(
                candidate.market_hash_name,
                steam_id=self.steam_client.steam_id64,
            ),
        }
        if self.config.dry_run:
            print(
                f"[dry-run] 导余额买入 {candidate.market_hash_name} listing={buy_target.listing_id} "
                f"price={buy_target.total / 100.0:.2f}"
            )
            print(
                f"[dry-run] 导余额上架C5 {candidate.market_hash_name} asset={sell_asset_id} "
                f"price={current_c5_sale_price:.2f}"
            )
            return True

        self.db.set_pool_status(candidate.market_hash_name, POOL_STATUS_TRANSFER_BUYING)
        try:
            payload = self.steam_client.buy_listing(
                listing_id=buy_target.listing_id,
                app_id=self.settings.app_id,
                subtotal=buy_target.subtotal,
                fee=buy_target.fee,
                total=buy_target.total,
            )
        except SteamMarketError as exc:
            self.db.set_pool_status(candidate.market_hash_name, POOL_STATUS_HOLDING)
            self._notify_skip(candidate.market_hash_name, "steam_price_unavailable", {"error": str(exc)})
            return False

        note = _build_note({**note_payload, "walletInfo": payload.get("wallet_info")})
        self.db.add_pool_operation(
            market_hash_name=candidate.market_hash_name,
            strategy=candidate.primary_strategy,
            operation_type=OP_TRANSFER_BUY,
            expected_price=buy_target.total / 100.0,
            note=note,
        )
        return True

    def _pick_lowest_steam_listing(self, payload: dict[str, Any]) -> SteamBuyTarget | None:
        listinginfo = payload.get("listinginfo") or payload.get("listings") or {}
        if not isinstance(listinginfo, dict):
            return None
        candidates: list[SteamBuyTarget] = []
        for raw_listing_id, raw_entry in listinginfo.items():
            if not isinstance(raw_entry, dict):
                continue
            listing_id = str(raw_entry.get("listingid") or raw_listing_id or "").strip()
            subtotal = safe_int(raw_entry.get("converted_price") or raw_entry.get("price"))
            fee = safe_int(raw_entry.get("converted_fee") or raw_entry.get("fee"))
            total = safe_int(raw_entry.get("converted_total") or raw_entry.get("total"))
            if total is None and subtotal is not None and fee is not None:
                total = subtotal + fee
            if not listing_id or subtotal is None or fee is None or total is None or total <= 0:
                continue
            candidates.append(
                SteamBuyTarget(
                    listing_id=listing_id,
                    subtotal=subtotal,
                    fee=fee,
                    total=total,
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (entry.total, entry.fee, entry.listing_id))
        return candidates[0]

    def _reconcile_transfer_buys(self) -> None:
        buy_ops = self.db.list_pool_operations_by_type_and_statuses(
            OP_TRANSFER_BUY,
            statuses=["pending", "listed", "sold", "cooldown"],
            limit=200,
        )
        claimed_asset_ids = {
            str(row["asset_id"])
            for row in buy_ops
            if row["asset_id"]
        }
        for op in reversed(buy_ops):
            note = _read_note(op["note"])
            asset_id = str(op["asset_id"] or note.get("boughtAssetId") or "").strip()
            if not asset_id:
                steam_id = str(note.get("steamId") or "").strip() or None
                sell_asset_id = str(note.get("sellAssetId") or "").strip()
                before_asset_ids = {
                    str(value)
                    for value in (note.get("beforeAssetIds") or [])
                    if str(value).strip()
                }
                for asset_row in self.db.list_assets(
                    market_hash_name=op["market_hash_name"],
                    steam_id=steam_id,
                ):
                    current_asset_id = str(asset_row["asset_id"])
                    if current_asset_id == sell_asset_id:
                        continue
                    if current_asset_id in before_asset_ids or current_asset_id in claimed_asset_ids:
                        continue
                    asset_id = current_asset_id
                    claimed_asset_ids.add(current_asset_id)
                    break
            if not asset_id:
                continue

            inventory_item = self._inventory_items_by_asset_id.get(asset_id)
            merged_note = {**note, "boughtAssetId": asset_id}
            if inventory_item:
                merged_note["steamId"] = str(inventory_item.get("steamId") or merged_note.get("steamId") or "")
                merged_note["tradableTime"] = inventory_item.get("tradableTime")
            self.db.update_pool_operation(op["id"], asset_id=asset_id, note=_build_note(merged_note))

    def _execute_transfer_sells(self) -> int:
        pending_ops = self.db.list_pool_operations_by_type_and_statuses(
            OP_TRANSFER_BUY,
            statuses=["pending"],
            limit=200,
        )
        sell_count = 0
        for op in pending_ops:
            if sell_count >= self.config.max_list_per_cycle:
                break
            note = _read_note(op["note"])
            asset_id = str(note.get("sellAssetId") or "").strip()
            if not asset_id:
                continue
            inventory_item = self._inventory_items_by_asset_id.get(asset_id)
            if not inventory_item:
                continue
            if not self._is_inventory_item_tradable(inventory_item):
                continue

            sale_price = self._resolve_transfer_sale_price(op["market_hash_name"], inventory_item, note)
            if sale_price is None or sale_price <= 0:
                continue
            if self.config.dry_run:
                print(
                    f"[dry-run] 导余额上架C5 {op['market_hash_name']} asset={asset_id} "
                    f"price={sale_price:.2f}"
                )
                sell_count += 1
                continue

            try:
                payload = self.c5_client.sale_create(
                    app_id=self.settings.app_id,
                    items=[
                        {
                            "assetId": asset_id,
                            "marketHashName": op["market_hash_name"],
                            "price": sale_price,
                            "token": note.get("sellAssetToken") or inventory_item.get("token"),
                            "styleToken": note.get("sellAssetStyleToken") or inventory_item.get("styleToken"),
                        }
                    ],
                )
            except C5GameError as exc:
                self._notify_skip(op["market_hash_name"], "price_too_high", {"error": str(exc)})
                continue

            product_id = self._extract_c5_sale_id(payload)
            sell_note = _build_note(
                {
                    "sourceTransferBuyOpId": op["id"],
                    "assetId": asset_id,
                    "marketHashName": op["market_hash_name"],
                    "c5SalePrice": round(sale_price, 2),
                    "productId": product_id,
                    "raw": payload,
                }
            )
            sell_op_id = self.db.add_pool_operation(
                market_hash_name=op["market_hash_name"],
                strategy=op["strategy"],
                operation_type=OP_TRANSFER_SELL,
                expected_price=sale_price,
                asset_id=asset_id,
                note=sell_note,
            )
            self.db.update_pool_operation(sell_op_id, status="listed", note=sell_note)
            self.db.update_pool_operation(
                op["id"],
                status="listed",
                note=_build_note(
                    {
                        **note,
                        "linkedTransferSellOpId": sell_op_id,
                        "productId": product_id,
                    }
                ),
            )
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_TRANSFER_LISTED_C5)
            self.db.set_asset_status(asset_id, "listed")
            sell_count += 1
        return sell_count

    def _refresh_transfer_holdings(self) -> int:
        updated = 0
        buy_ops = self.db.list_pool_operations_by_type_and_statuses(
            OP_TRANSFER_BUY,
            statuses=["sold", "cooldown"],
            limit=200,
        )
        for op in buy_ops:
            note = _read_note(op["note"])
            bought_asset_id = str(op["asset_id"] or note.get("boughtAssetId") or "").strip()
            if not bought_asset_id:
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_TRANSFER_SOLD)
                continue
            inventory_item = self._inventory_items_by_asset_id.get(bought_asset_id)
            if not inventory_item or not self._is_inventory_item_tradable(inventory_item):
                self.db.update_pool_operation(op["id"], status="cooldown")
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_TRANSFER_HOLDING)
                updated += 1
                continue
            self.db.update_pool_operation(op["id"], status="completed")
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
            updated += 1
        return updated

    def _resolve_transfer_sale_price(
        self,
        market_hash_name: str,
        inventory_item: dict[str, Any],
        note: dict[str, Any],
    ) -> float | None:
        fallback_price = safe_float(inventory_item.get("price")) or safe_float(note.get("targetC5Price"))
        try:
            payload = self.c5_client.price_batch([market_hash_name], app_id=self.settings.app_id)
        except Exception:
            return fallback_price
        if not isinstance(payload, dict):
            return fallback_price
        return safe_float((payload.get(market_hash_name) or {}).get("price")) or fallback_price

    def _is_inventory_item_tradable(self, item: dict[str, Any]) -> bool:
        if item.get("ifTradable") is True:
            return True
        tradable_time = _parse_iso(str(item.get("tradableTime") or "").strip())
        if tradable_time is None:
            return False
        return tradable_time <= _now_utc()

    def _extract_c5_sale_id(self, payload: dict[str, Any]) -> str | None:
        direct_value = payload.get("id") or payload.get("productId") or payload.get("saleId")
        if direct_value not in (None, ""):
            return str(direct_value)
        for key in ("successList", "list", "dataList", "items", "records"):
            rows = payload.get(key)
            if not isinstance(rows, list) or not rows:
                continue
            first = rows[0]
            if not isinstance(first, dict):
                continue
            value = first.get("id") or first.get("productId") or first.get("saleId")
            if value not in (None, ""):
                return str(value)
        return None

    def _refresh_listings(self) -> int:
        if not self.steam_client:
            return 0
        try:
            active = self.steam_client.list_active_listings()
        except Exception as exc:
            print(f"[警告] 获取 Steam 挂单列表失败（可能 Cookie 过期）: {exc}")
            return 0
        active_listing_ids = {lst.listing_id for lst in active if lst.listing_id}
        active_asset_ids = {lst.asset_id for lst in active if lst.asset_id}
        now = _now_utc()
        sold_count = 0
        pool_status_map = self.db.get_pool_status_map()
        pending_rebuys = {
            row["market_hash_name"]
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=500)
        }

        listed_ops = self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=200)
        for op in listed_ops:
            pool_status = pool_status_map.get(op["market_hash_name"], POOL_STATUS_HOLDING)
            if pool_status == POOL_STATUS_LISTING_PENDING:
                continue
            note = _read_note(op["note"])
            listing_id = str(note.get("listingId") or "")
            asset_id = str(op["asset_id"] or "")

            # 判断是否仍在活跃挂单中（listing_id 优先，没有则用 asset_id 兜底）
            if listing_id and listing_id in active_listing_ids:
                continue
            if not listing_id and asset_id and asset_id in active_asset_ids:
                continue

            created_at = _parse_iso(op["created_at"])
            if created_at and (now - created_at).total_seconds() < self.config.listing_check_interval_minutes * 60:
                continue

            self.db.update_pool_operation(op["id"], status="sold")
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
            if asset_id:
                self.db.set_asset_status(asset_id, "sold")

            steam_list_price = note.get("steamListPrice")
            print(
                f"[卖出] {op['market_hash_name']} | "
                f"asset={asset_id} | "
                f"Steam售价 ¥{steam_list_price or '?'}"
            )

            rebuy_price = note.get("rebuyPrice")
            if isinstance(rebuy_price, (int, float)) and rebuy_price > 0:
                if op["market_hash_name"] not in pending_rebuys:
                    self.db.add_pool_operation(
                        market_hash_name=op["market_hash_name"],
                        strategy=op["strategy"],
                        operation_type=OP_REBUY_C5,
                        expected_price=float(rebuy_price),
                        note=_build_note(
                            {
                                "sourceListing": listing_id,
                                "steamListPrice": steam_list_price,
                            }
                        ),
                    )
                    pending_rebuys.add(op["market_hash_name"])
            sold_count += 1
        return sold_count

    def _refresh_transfer_sales(self) -> int:
        listed_ops = self.db.list_pool_operations_by_type(OP_TRANSFER_SELL, status="listed", limit=200)
        if not listed_ops:
            return 0
        active_ids = self._load_active_c5_sale_ids()
        now = _now_utc()
        sold_count = 0
        for op in listed_ops:
            note = _read_note(op["note"])
            product_id = str(note.get("productId") or "").strip()
            if not product_id or product_id in active_ids:
                continue
            created_at = _parse_iso(op["created_at"])
            if created_at and (now - created_at).total_seconds() < self.config.listing_check_interval_minutes * 60:
                continue
            self.db.update_pool_operation(op["id"], status="sold")
            if op["asset_id"]:
                self.db.set_asset_status(op["asset_id"], "sold")
            source_buy_op_id = safe_int(note.get("sourceTransferBuyOpId"))
            if source_buy_op_id is not None:
                self.db.update_pool_operation(source_buy_op_id, status="sold")
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_TRANSFER_SOLD)
            sold_count += 1
        return sold_count

    def _load_active_c5_sale_ids(self) -> set[str]:
        active_ids: set[str] = set()
        page = 1
        limit = 100
        while True:
            payload = self.c5_client.sale_search(
                app_id=self.settings.app_id,
                page=page,
                limit=limit,
            )
            rows = payload.get("list") or payload.get("items") or []
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                value = row.get("id") or row.get("productId") or row.get("saleId")
                if value not in (None, ""):
                    active_ids.add(str(value))
            total = safe_int(payload.get("total"))
            if len(rows) < limit:
                break
            if total is not None and page * limit >= total:
                break
            page += 1
        return active_ids

    def _resolve_trade_url(self) -> str | None:
        """优先用环境变量，没有则自动从 Steam 页面获取。"""
        if self.settings.steam_trade_url:
            return self.settings.steam_trade_url
        if not self.steam_client:
            return None
        try:
            url = self.steam_client.get_trade_url()
            self.settings.steam_trade_url = url  # 缓存，避免重复请求
            return url
        except Exception as exc:
            print(f"[警告] 自动获取交易链接失败: {exc}")
            return None

    def _execute_rebuys(self) -> int:
        if not self.config.auto_rebuy_enabled:
            return 0
        trade_url = self._resolve_trade_url()
        if not trade_url:
            print("[提示] 未配置 STEAM_TRADE_URL，将尝试不带 tradeUrl 直接补仓（C5 使用账号预设链接）")
        pending = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=200)
        rebuy_count = 0
        for op in pending:
            if rebuy_count >= self.config.max_buy_per_cycle:
                break
            expected_price = op["expected_price"]
            if expected_price is None:
                continue
            note = _read_note(op["note"])
            expected_steam_list = note.get("steamListPrice")
            result = execute_rebuy(
                client=self.c5_client,
                steam_client=self.steam_client,
                market_hash_name=op["market_hash_name"],
                expected_price=float(expected_price),
                expected_steam_list_price=float(expected_steam_list) if expected_steam_list else None,
                app_id=self.settings.app_id,
                tolerance_pct=self.config.price_tolerance_pct,
                dry_run=self.config.dry_run,
                verify_steam=self.config.verify_steam_before_rebuy,
                force_refresh=self.config.force_refresh_before_execution,
                pricing_kwargs={
                    "wall_min_count": self.config.listing_wall_min_count,
                    "price_offset": self.config.listing_price_offset,
                    "country": self.config.steam_country,
                    "language": self.config.steam_language,
                    "currency": self.config.steam_currency,
                    "cache_ttl": self.config.steam_price_cache_ttl,
                },
                steam_net_factor=self.config.steam_net_factor,
                guadao_max_listing_ratio=self.config.guadao_max_listing_ratio,
                trade_url=trade_url,
            )
            if result.reason in (
                "steam_crashed",
                "ratio_no_longer_profitable",
                "steam_price_unavailable",
                "no_matching_listing",
            ):
                # 临时性跳过：保持 pending，下次循环重试；只更新 note 记录原因
                self.db.update_pool_operation(
                    op["id"],
                    note=_build_note(
                        {
                            **note,
                            "lastSkipReason": result.reason,
                            "steamPriceNow": result.steam_price_now,
                            "listingRatioNow": result.listing_ratio_now,
                        }
                    ),
                )
                print(
                    f"[补仓等待] {op['market_hash_name']} | "
                    f"原因: {result.reason} | "
                    f"Steam现价: {result.steam_price_now} | "
                    f"ratio: {result.listing_ratio_now:.4f}"
                    if result.listing_ratio_now else
                    f"[补仓等待] {op['market_hash_name']} | 原因: {result.reason}"
                )
                self._notify_skip(op["market_hash_name"], result.reason, result)
                continue
            if result.reason == "price_too_high":
                # C5 当前价格超出预算上限 → 永久跳过本次补仓
                self.db.update_pool_operation(
                    op["id"],
                    status="skipped",
                    note=_build_note(
                        {
                            **note,
                            "skipReason": result.reason,
                            "actualPrice": result.actual_price,
                        }
                    ),
                )
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
                print(f"[补仓跳过] {op['market_hash_name']} | C5价格过高: ¥{result.actual_price}")
                self._notify_skip(op["market_hash_name"], result.reason, result)
                continue
            if result.success and not result.skipped:
                self.db.update_pool_operation(op["id"], status="completed", actual_price=result.actual_price)
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
                print(
                    f"[补仓] {op['market_hash_name']} | "
                    f"C5买入 ¥{result.actual_price:.2f}"
                )
                rebuy_count += 1
            elif result.skipped:
                self.db.update_pool_operation(op["id"], status="dry_run")
            else:
                self.db.update_pool_operation(op["id"], status="failed")
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_REBUY_FAILED)
                print(f"[补仓失败] {op['market_hash_name']} | 原因: {result.reason}")
                if self.serverchan:
                    try:
                        self.serverchan.send(
                            f"[补仓失败] {op['market_hash_name']}",
                            f"原因: {result.reason}\nC5价格: {result.actual_price}\n"
                            f"Steam价格: {result.steam_price_now}",
                        )
                    except Exception:
                        pass
        return rebuy_count

    def _notify_skip(self, market_hash_name: str, reason: str, details: Any) -> None:
        if not self.serverchan:
            return
        title_map = {
            "steam_crashed": "[rebuy] steam dropped",
            "ratio_no_longer_profitable": "[rebuy] ratio not profitable",
            "steam_price_unavailable": "[rebuy] steam price unavailable",
            "no_matching_listing": "[rebuy] no matching listing",
            "price_too_high": "[rebuy] c5 price too high",
            "steam_price_dropped": "[list] steam price dropped",
            "no_tradable_asset": "[transfer] no tradable base asset",
            "c5_price_unavailable": "[transfer] c5 price unavailable",
        }
        title = f"{title_map.get(reason, '[skip]')} - {market_hash_name}"
        body = str(details)
        try:
            self.serverchan.send(title, body)
        except Exception:
            pass
