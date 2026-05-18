from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cs2_assistant.accounts import Account, AccountStore
from cs2_assistant.clients import (
    C5GameClient,
    C5GameError,
    ServerChanClient,
    SteamMarketClient,
    SteamMarketError,
)
from cs2_assistant.config import PROJECT_ROOT, Settings
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
    guadao_scope_allows_item,
)
from cs2_assistant.services.executor_buy import execute_rebuy
from cs2_assistant.services.market import calculate_listing_ratio, calculate_transfer_real_ratio
from cs2_assistant.services.pricing import PricingDecision, fetch_listing_price
from cs2_assistant.services.strategy import load_strategy_config, scan_strategies
from cs2_assistant.services.t_yield_scan import fetch_all_c5_inventories, summarize_inventory_types
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


REBUY_NO_MATCHING_TIMEOUT_SECONDS = 3 * 60 * 60


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


def _looks_like_weapon_case_name(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized.endswith(" case") or "武器箱" in value


def _steam_id64_from_trade_url(trade_url: str | None) -> str | None:
    if not trade_url:
        return None
    match = re.search(r"[?&]partner=(\d+)", trade_url)
    if not match:
        return None
    try:
        partner = int(match.group(1))
    except ValueError:
        return None
    return str(partner + 76561197960265728)


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
        account: Account | str | None = None,
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

        self.account_store = AccountStore(PROJECT_ROOT / "config")
        if isinstance(account, Account):
            self.account = account
        elif account is not None:
            self.account = self.account_store.get_account(str(account))
        else:
            self.account = self.account_store.get_current()

        self._c5_api_key = (self.account.c5_api_key if self.account else None) or settings.c5_api_key
        if self.account:
            self._steam_cookies = self.account.cookies
            self._steam_identity_secret = self.account.identity_secret
            self._steam_device_id = self.account.device_id
            self._steam_trade_url = self.account.trade_url
        else:
            self._steam_cookies = settings.steam_cookies
            self._steam_identity_secret = settings.steam_identity_secret
            self._steam_device_id = settings.steam_device_id
            self._steam_trade_url = None

        self.db = Database(settings.db_path)
        self.db.initialize()
        if not self._c5_api_key:
            raise RuntimeError("missing C5GAME_API_KEY / C5_API_KEY")
        self.c5_client = C5GameClient(self._c5_api_key, settings.c5_base_url)
        self.serverchan = (
            ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url)
            if settings.serverchan_sendkey
            else None
        )

        self.steam_client = None
        if self.config.execution_enabled or self.config.auto_list_enabled:
            if not self._steam_cookies:
                raise RuntimeError("missing STEAM_COOKIES for auto execution")
            self.steam_client = SteamMarketClient(
                cookies=self._steam_cookies,
                steam_id64=None,
                identity_secret=self._steam_identity_secret,
                device_id=self._steam_device_id,
                account_id=self.account.id if self.account else None,
                base_url=settings.steam_market_base_url,
            )

        self._last_inventory_payload: dict[str, Any] = {}
        self._inventory_items_by_asset_id: dict[str, dict[str, Any]] = {}
        self._pending_confirmation_count = 0
        self._stop_requested = False
        self._stop_reason: str | None = None
        self._case_open_guadao_limit_notified = False

        if (
            self.config.execution_enabled
            and not self.config.dry_run
            and self.config.auto_list_enabled
            and (not self._steam_identity_secret or not self._steam_device_id)
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
            if self._stop_requested:
                if self._stop_reason:
                    print(f"[停止] {self._stop_reason}")
                return
            if once:
                return
            time.sleep(self.config.cycle_interval_minutes * 60)

    def run_once(self, *, wait_for_cycle: bool = True) -> None:
        self._pending_confirmation_count = 0
        self._sync_assets()
        pool_names = self.db.get_pool_market_hash_names()
        if not pool_names:
            print("底仓为空，且当前 C5 库存未同步到可执行品种，跳过执行。")
            return
        scan_pool_names = self._pool_names_for_strategy_scan(pool_names)

        self._refresh_transfer_holdings()
        report = scan_strategies(
            self.settings,
            self.config,
            allow_cached_fallback=True,
            cache_max_age_minutes=180,
            pool_market_hash_names=scan_pool_names,
            inventory_payload=self._last_inventory_payload,
        )
        self._print_scan_summary(report)
        self._filter_guadao_candidates_by_account(report)

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
            pool_names=scan_pool_names,
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

    def _pool_names_for_strategy_scan(self, pool_names: list[str]) -> list[str]:
        if self._transfer_scan_enabled():
            return pool_names
        return [
            market_hash_name
            for market_hash_name in pool_names
            if self._guadao_scope_allows_market_hash_name(market_hash_name)
        ]

    def _transfer_scan_enabled(self) -> bool:
        if self.config.max_buy_per_cycle <= 0:
            return False
        return float(self.config.transfer_min_real_ratio) < 9999.0

    def _print_scan_summary(self, report: Any) -> None:
        evaluated_count = len(getattr(report, "all_evaluated", []) or [])
        print(
            f"[扫描] 底仓池 {getattr(report, 'total_pool_types', 0)} 个品种 | "
            f"进入评估 {evaluated_count} 个 | "
            f"缺价 {getattr(report, 'missing_price_count', 0)} 个 | "
            f"挂刀候选 {getattr(report, 'guadao_count', 0)} 个 | "
            f"导余额候选 {getattr(report, 'transfer_count', 0)} 个"
        )

    def _filter_guadao_candidates_by_account(self, report: Any) -> None:
        """剔除当前 executor 账号本地实际不持有可交易资产的挂刀候选。

        scan_strategies 的库存数据来自 C5（聚合所有绑定的 Steam 账号），
        但真正能挂单的只有当前 STEAM_COOKIES 对应的那一个账号。
        这里把跨账号才有货的候选过滤掉，避免日志误导“挂刀候选 N 个”却一直 0 上架。
        """
        if not self.steam_client:
            return
        candidates = list(getattr(report, "guadao_candidates", []) or [])
        if not candidates:
            self._guadao_skipped_by_account = []
            return

        steam_id = str(getattr(self.steam_client, "steam_id64", "") or "")
        if not steam_id:
            self._guadao_skipped_by_account = []
            return

        kept: list[Any] = []
        skipped: list[tuple[str, int, int]] = []
        for candidate in candidates:
            mhn = candidate.market_hash_name
            local_assets = self.db.list_assets(
                market_hash_name=mhn,
                steam_id=steam_id,
                tradable=True,
                status="available",
            )
            local_count = len(local_assets)
            if local_count > 0:
                kept.append(candidate)
            else:
                # report.tradable_count 是跨所有账号合计；这里记下来日志里区分
                skipped.append((mhn, candidate.tradable_count, local_count))

        # 记录到 engine 用于 _describe_no_action_reasons 给出更清晰原因
        self._guadao_skipped_by_account = skipped
        if not skipped:
            return

        # 将筛后的候选写回 report（StrategyScanReport 是 slots dataclass，可直接赋值）
        try:
            report.guadao_candidates = kept
        except Exception:
            # 兜底：如果 slots 不允许赋值，至少打印日志告知
            pass

        for mhn, total_tradable, _local in skipped:
            print(
                f"[过滤] 挂刀候选 {mhn} 在当前 executor 账号 ({steam_id}) "
                f"无可交易资产，已跳过；C5 聚合可交易 {total_tradable} 件分散在其他绑定账号。"
            )
        print(
            f"[过滤] 账号过滤后剩余挂刀候选 {len(kept)} 个 (原 {len(candidates)} 个)"
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
            if self.config.dry_run:
                print(f"[结果] dry-run 模拟了 {total_actions} 个动作，没有真实下单。")
            else:
                print(f"[结果] 本轮已执行 {total_actions} 个实际动作。")
            return

        if self.config.dry_run:
            print("[结果] 本轮只完成了 dry-run 扫描/状态检查，没有真实下单。")
        else:
            print("[结果] 本轮只完成了扫描/状态检查，没有实际上架、买入或卖出。")
        reasons = self._describe_no_action_reasons(report, pool_names=pool_names)
        if not reasons:
            reasons = ["未命中可执行条件。"]
        for reason in reasons:
            print(f"[原因] {reason}")

    def _describe_no_action_reasons(self, report: Any, *, pool_names: list[str]) -> list[str]:
        reasons: list[str] = []
        skipped_by_account = getattr(self, "_guadao_skipped_by_account", []) or []
        if skipped_by_account:
            sample = "、".join(mhn for mhn, _t, _l in skipped_by_account[:3])
            suffix = " 等" if len(skipped_by_account) > 3 else ""
            reasons.append(
                f"挂刀候选 {len(skipped_by_account)} 个在当前 executor 账号本地无可交易资产，"
                f"被账号过滤跳过：{sample}{suffix}"
            )
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
        lowest_listing_summary = self._lowest_listing_ratio_reason(report)
        if lowest_listing_summary:
            reasons.append(lowest_listing_summary)
        if evaluated and not guadao_candidates and not transfer_candidates:
            reasons.append(f"已评估 {len(evaluated)} 个品种，但都未满足 list/transfer 阈值。")

        if guadao_candidates and all(int(candidate.tradable_count) <= 0 for candidate in guadao_candidates):
            reasons.append("存在挂刀候选，但当前没有可交易库存，无法上架。")

        if guadao_candidates and self.config.max_list_per_cycle <= 0:
            reasons.append("本轮 `max-list=0`，已禁用新的 Steam 上架。")

        if transfer_candidates and self.config.max_buy_per_cycle <= 0:
            reasons.append("本轮 `max-buy=0`，已禁用买入动作。")

        return reasons

    def _lowest_listing_ratio_reason(self, report: Any) -> str | None:
        evaluated = [
            candidate
            for candidate in list(getattr(report, "all_evaluated", []) or [])
            if getattr(candidate, "inventory_count", 0) > 0
        ]
        if not evaluated:
            return None

        best_candidate = min(evaluated, key=lambda candidate: float(candidate.listing_ratio))
        best_ratio_pct = float(best_candidate.listing_ratio_pct)
        threshold_pct = float(self.config.guadao_max_listing_ratio) * 100.0
        skipped_market_names = {
            str(market_hash_name)
            for market_hash_name, _total_tradable, _local in (getattr(self, "_guadao_skipped_by_account", []) or [])
        }

        if best_candidate.market_hash_name in skipped_market_names:
            return (
                f"当前库内最低预计挂刀比例为 {best_ratio_pct:.2f}% "
                f"（{best_candidate.market_hash_name}），配置挂刀阈值为 {threshold_pct:.2f}%："
                "比例已满足，但当前 executor 账号无可交易资产。"
            )

        if best_candidate.listing_ratio <= self.config.guadao_max_listing_ratio:
            return (
                f"当前库内最低预计挂刀比例为 {best_ratio_pct:.2f}% "
                f"（{best_candidate.market_hash_name}），配置挂刀阈值为 {threshold_pct:.2f}%。"
            )

        return (
            f"当前库内最低预计挂刀比例为 {best_ratio_pct:.2f}% "
            f"（{best_candidate.market_hash_name}），配置挂刀阈值为 {threshold_pct:.2f}%："
            "最低比例仍高于阈值，暂不满足挂刀条件。"
        )

    def _current_inventory_type_names(self) -> set[str]:
        names: set[str] = set()
        for item in list(self._last_inventory_payload.get("list") or []):
            if not isinstance(item, dict):
                continue
            market_hash_name = str(item.get("marketHashName") or "").strip()
            if market_hash_name:
                names.add(market_hash_name)
        return names

    def _effective_steam_identity_secret(self) -> str | None:
        return getattr(self, "_steam_identity_secret", None) or self.settings.steam_identity_secret

    def _effective_steam_device_id(self) -> str | None:
        return getattr(self, "_steam_device_id", None) or self.settings.steam_device_id

    def _effective_steam_trade_url(self) -> str | None:
        return getattr(self, "_steam_trade_url", None)

    def _expected_rebuy_steam_id64(self) -> str | None:
        account_steam_id = str(self.account.steam_id64 or "").strip() if self.account else ""
        if account_steam_id:
            return account_steam_id
        client_steam_id = str(getattr(self.steam_client, "steam_id64", "") or "").strip()
        return client_steam_id or None

    def _is_trade_url_for_expected_account(self, trade_url: str | None) -> bool:
        expected_steam_id = self._expected_rebuy_steam_id64()
        trade_url_steam_id = _steam_id64_from_trade_url(trade_url)
        if not expected_steam_id or not trade_url_steam_id:
            return True
        return expected_steam_id == trade_url_steam_id

    def _run_guadao_cycle(self, report: Any, *, wait_for_cycle: bool) -> tuple[int, int, int]:
        status_map = self.db.get_pool_status_map()
        listed = 0
        sold = 0
        rebought = 0

        if self._has_open_guadao_cycle(status_map):
            print("[等待] 检测到上一轮挂刀循环未闭环，本轮先等待卖出/补仓完成。")
        else:
            case_open_count = self._open_case_guadao_count()
            if case_open_count > 0:
                print(
                    f"[继续] 箱子未闭环挂刀 {case_open_count}/{self._case_max_open_guadao_count()}，"
                    "未达上限，本轮继续开启新挂刀。"
                )
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
            case_open_count = self._open_case_guadao_count()
            if self._case_open_guadao_limit_reached(case_open_count):
                self._notify_case_open_guadao_limit(case_open_count)
                return sold, rebought
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
        listed_sell_ops = self.db.list_pool_operations_by_type(
            OP_SELL_STEAM,
            status="listed",
            limit=500,
        )
        pending_rebuy_ops = self.db.list_pool_operations_by_type(
            OP_REBUY_C5,
            status="pending",
            limit=500,
        )
        failed_rebuy_ops = self.db.list_pool_operations_by_type(
            OP_REBUY_C5,
            status="failed",
            limit=500,
        )
        if listed_sell_ops:
            counts["sell_on_steam.listed"] = len(listed_sell_ops)
        if pending_rebuy_ops:
            counts["rebuy_on_c5.pending"] = len(pending_rebuy_ops)
        if failed_rebuy_ops:
            counts["rebuy_on_c5.failed"] = len(failed_rebuy_ops)
        case_open_count = self._open_case_guadao_count()
        if case_open_count:
            counts["case_open_guadao.unclosed"] = case_open_count
        return counts

    def _has_open_guadao_cycle(self, status_map: dict[str, str] | None = None) -> bool:
        current_status_map = status_map or self.db.get_pool_status_map()
        hard_block_statuses = {
            POOL_STATUS_LISTING_PENDING,
            POOL_STATUS_REBUY_FAILED,
        }
        case_pool_open_count = 0
        for market_hash_name, status in current_status_map.items():
            if status in hard_block_statuses:
                return True
            if status not in {POOL_STATUS_LISTED, POOL_STATUS_PENDING_REBUY}:
                continue
            if not self._is_weapon_case(market_hash_name):
                return True
            case_pool_open_count += 1

        if self._has_non_case_open_guadao_operation():
            return True
        if self.db.list_pool_operations_by_type(OP_REBUY_C5, status="failed", limit=1):
            return True

        case_open_count = self._open_case_guadao_count()
        if case_pool_open_count and case_open_count == 0:
            return True
        if self._case_open_guadao_limit_reached(case_open_count):
            self._notify_case_open_guadao_limit(case_open_count)
            return True
        return False

    def _case_max_open_guadao_count(self) -> int:
        return max(0, int(self.config.case_max_open_guadao_count))

    def _open_case_guadao_count(self) -> int:
        count = 0
        limit = max(500, self._case_max_open_guadao_count() + 10)
        for operation_type, status in (
            (OP_SELL_STEAM, "listed"),
            (OP_REBUY_C5, "pending"),
        ):
            for op in self.db.list_pool_operations_by_type(operation_type, status=status, limit=limit):
                if not self._is_weapon_case(op["market_hash_name"]):
                    continue
                quantity = safe_int(op["quantity"]) or 1
                count += max(1, quantity)
        return count

    def _has_non_case_open_guadao_operation(self) -> bool:
        for operation_type, status in (
            (OP_SELL_STEAM, "listed"),
            (OP_REBUY_C5, "pending"),
        ):
            for op in self.db.list_pool_operations_by_type(operation_type, status=status, limit=500):
                if not self._is_weapon_case(op["market_hash_name"]):
                    return True
        return False

    def _case_open_guadao_limit_reached(self, count: int | None = None) -> bool:
        count = self._open_case_guadao_count() if count is None else count
        return count > 0 and count >= self._case_max_open_guadao_count()

    def _notify_case_open_guadao_limit(self, count: int) -> None:
        limit = self._case_max_open_guadao_count()
        self._stop_requested = True
        self._stop_reason = (
            f"箱子未闭环挂刀已达到 {count}/{limit} 个，已停止开启新一轮，等待人工处理。"
        )
        if getattr(self, "_case_open_guadao_limit_notified", False):
            return
        self._case_open_guadao_limit_notified = True
        print(f"[提醒] {self._stop_reason}")
        serverchan = getattr(self, "serverchan", None)
        if not serverchan:
            return
        try:
            serverchan.send(
                "[挂刀暂停] 箱子未闭环已达上限",
                (
                    f"箱子未闭环挂刀: {count}/{limit}\n"
                    "状态: 已停止开启新一轮\n"
                    "处理: 请手动检查 Steam 挂单和 C5 补仓状态"
                ),
            )
        except Exception as exc:
            print(f"  ServerChan 推送失败: {exc}")

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
        inventory_source = str(inventory_payload.get("source") or "").lower()
        self.db.sync_pool_from_inventory(
            summarize_inventory_types(items),
            zero_missing_holding=inventory_source != "cache",
        )
        self._reconcile_transfer_buys()

    def _decide_listing(self, candidate: StrategyCandidate) -> ListingDecision | None:
        if not self.steam_client:
            return None
        price_offset = self._listing_price_offset_for_candidate(candidate)
        pricing = fetch_listing_price(
            self.steam_client,
            app_id=self.settings.app_id,
            market_hash_name=candidate.market_hash_name,
            wall_min_count=self._listing_wall_min_count_for_candidate(candidate),
            price_offset=price_offset,
            min_price=0.01,
            country=self.config.steam_country,
            language=self.config.steam_language,
            currency=self.config.steam_currency,
            force_refresh=False,
            cache_ttl=self.config.steam_price_cache_ttl,
        )
        if pricing is None:
            if not self.config.dry_run:
                print(
                    f"[上架跳过] {candidate.market_hash_name} | "
                    "真实执行必须获取 Steam 实时挂单墙价格，当前取价失败"
                )
                return None
            fallback_price = safe_float(candidate.steam_sell_price)
            if fallback_price is None or fallback_price <= 0:
                return None
            pricing = PricingDecision(
                list_price=float(fallback_price),
                wall_price=None,
                reason="scan_price_fallback",
            )
            print(
                f"[上架定价] {candidate.market_hash_name} | "
                f"Steam 实时挂单墙取价失败，dry-run 使用扫描价 CNY {fallback_price:.2f}"
            )
        return self._decision_from_list_price(candidate, pricing.list_price, pricing=pricing)

    def _is_weapon_case(self, market_hash_name: str) -> bool:
        item = self.db.get_item(market_hash_name)
        if item is not None:
            if _looks_like_weapon_case_name(str(item["market_hash_name"])):
                return True
            if _looks_like_weapon_case_name(str(item["name_cn"])):
                return True
            raw_json = _read_note(item["raw_json"])
            if _looks_like_weapon_case_name(str(raw_json.get("marketHashName") or "")):
                return True
            if _looks_like_weapon_case_name(str(raw_json.get("name") or "")):
                return True
            type_name = str(raw_json.get("typeName") or raw_json.get("type") or "")
            if "武器箱" in type_name or "weaponcase" in type_name.lower():
                return True
        return _looks_like_weapon_case_name(market_hash_name)

    def _guadao_scope_allows_market_hash_name(self, market_hash_name: str) -> bool:
        return guadao_scope_allows_item(
            self.config.guadao_item_scope,
            is_weapon_case=self._is_weapon_case(market_hash_name),
        )

    def _listing_price_offset_for_candidate(self, candidate: StrategyCandidate) -> float:
        return self._listing_price_offset_for_market_hash_name(candidate.market_hash_name)

    def _listing_wall_min_count_for_candidate(self, candidate: StrategyCandidate) -> int:
        return self._listing_wall_min_count_for_market_hash_name(candidate.market_hash_name)

    def _listing_wall_min_count_for_market_hash_name(self, market_hash_name: str) -> int:
        if not self._is_weapon_case(market_hash_name):
            return 1
        return self.config.listing_wall_min_count

    def _listing_price_offset_for_market_hash_name(self, market_hash_name: str) -> float:
        if self._is_weapon_case(market_hash_name):
            case_offset = self.config.case_listing_price_offset
            if case_offset is not None:
                return case_offset
        return self.config.listing_price_offset

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
        # 本轮内 (market_hash_name, reason) 维度的去重，避免同饰品在同一轮里重复推送跳过通知
        self._notified_listing_skips_cycle: set[tuple[str, str]] = set()
        candidates = [
            candidate
            for candidate in report.guadao_candidates
            if candidate.primary_strategy == STRATEGY_GUADAO
            and self._guadao_scope_allows_market_hash_name(candidate.market_hash_name)
        ]

        for candidate in candidates:
            if getattr(self, "_stop_requested", False):
                break
            if list_count >= self.config.max_list_per_cycle:
                break
            if self._is_weapon_case(candidate.market_hash_name):
                case_open_count = self._open_case_guadao_count()
                if self._case_open_guadao_limit_reached(case_open_count):
                    self._notify_case_open_guadao_limit(case_open_count)
                    break
            if not self._can_execute_guadao(status_map.get(candidate.market_hash_name)):
                continue
            if candidate.tradable_count <= 0:
                continue

            decision = self._decide_listing(candidate)
            if decision is None:
                continue
            if self.config.force_refresh_before_execution:
                price_offset = self._listing_price_offset_for_candidate(candidate)
                final_pricing = fetch_listing_price(
                    self.steam_client,
                    app_id=self.settings.app_id,
                    market_hash_name=candidate.market_hash_name,
                    wall_min_count=self._listing_wall_min_count_for_candidate(candidate),
                    price_offset=price_offset,
                    min_price=0.01,
                    country=self.config.steam_country,
                    language=self.config.steam_language,
                    currency=self.config.steam_currency,
                    force_refresh=True,
                    cache_ttl=self.config.steam_price_cache_ttl,
                )
                if final_pricing is None:
                    if not self.config.dry_run:
                        cycle_key = (candidate.market_hash_name, "steam_price_unavailable")
                        if cycle_key not in self._notified_listing_skips_cycle:
                            self._notify_skip(candidate.market_hash_name, "steam_price_unavailable", {})
                            self._notified_listing_skips_cycle.add(cycle_key)
                        print(
                            f"[上架跳过] {candidate.market_hash_name} | "
                            "force_refresh 未能获取 Steam 实时挂单墙价格，真实执行不上架"
                        )
                        continue
                    print(
                        f"[上架定价] {candidate.market_hash_name} | "
                        "force_refresh 实时取价失败，dry-run 沿用扫描价/缓存价继续判断"
                    )
                    final_pricing = decision.pricing
                decision = self._decision_from_list_price(
                    candidate,
                    final_pricing.list_price if final_pricing is not None else decision.list_price,
                    pricing=final_pricing,
                )
                if decision is None:
                    continue
            if decision.listing_ratio > self.config.guadao_max_listing_ratio:
                continue

            steam_id = self.steam_client.steam_id64
            while list_count < self.config.max_list_per_cycle:
                if self._is_weapon_case(candidate.market_hash_name):
                    case_open_count = self._open_case_guadao_count()
                    if self._case_open_guadao_limit_reached(case_open_count):
                        self._notify_case_open_guadao_limit(case_open_count)
                        break
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
                    steam_net_factor=self.config.steam_net_factor,
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
                    f"预计挂刀比例 {decision.listing_ratio * 100:.2f}% | "
                    f"Steam挂价 CNY {decision.list_price:.2f} | "
                    f"预计到手 CNY {decision.list_price * 0.869:.2f}"
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
        if not self._effective_steam_identity_secret() or not self._effective_steam_device_id():
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
        steam_pricing = fetch_listing_price(
            self.steam_client,
            app_id=self.settings.app_id,
            market_hash_name=candidate.market_hash_name,
            wall_min_count=1,
            price_offset=0.0,
            min_price=0.01,
            country=self.config.steam_country,
            language=self.config.steam_language,
            currency=self.config.steam_currency,
            force_refresh=True,
            cache_ttl=self.config.steam_price_cache_ttl,
        )
        if steam_pricing is None:
            self._notify_skip(candidate.market_hash_name, "steam_price_unavailable", {})
            return False

        decision = self._decision_from_prices(
            rebuy_price=float(current_c5_sale_price),
            list_price=steam_pricing.list_price,
            pricing=steam_pricing,
        )
        if decision is None or decision.transfer_real_ratio < self.config.transfer_min_real_ratio:
            self._notify_skip(
                candidate.market_hash_name,
                "ratio_no_longer_profitable",
                {
                    "steamPriceNow": steam_pricing.list_price,
                    "steamPriceReason": steam_pricing.reason,
                },
            )
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
                    "steamPriceNow": round(steam_pricing.list_price, 2),
                    "steamPriceReason": steam_pricing.reason,
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
            print(
                "[警告] 获取 Steam 挂单列表失败，暂按网络/Steam 超时处理，"
                f"不会判定为已卖出: {exc}"
            )
            return 0
        active_listing_ids = {lst.listing_id for lst in active if lst.listing_id}
        active_asset_ids = {lst.asset_id for lst in active if lst.asset_id}
        now = _now_utc()
        sold_count = 0
        pool_status_map = self.db.get_pool_status_map()
        existing_rebuy_sources: set[str] = set()
        existing_rebuy_sell_ops: set[str] = set()
        for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=1000):
            rebuy_note = _read_note(row["note"])
            source_listing = str(rebuy_note.get("sourceListing") or "").strip()
            source_sell_op = str(rebuy_note.get("sourceSellOperationId") or "").strip()
            if source_listing:
                existing_rebuy_sources.add(source_listing)
            if source_sell_op:
                existing_rebuy_sell_ops.add(source_sell_op)

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
            steam_net_price = (
                float(steam_list_price) * self.config.steam_net_factor
                if isinstance(steam_list_price, (int, float))
                else None
            )
            sold_message = (
                f"[卖出] {op['market_hash_name']} | "
                f"asset={asset_id} | "
                f"Steam售价 CNY {steam_list_price or '?'}"
            )
            if steam_net_price is not None:
                sold_message += f" | 税后到手 CNY {steam_net_price:.2f}"
            print(sold_message)

            rebuy_price = note.get("rebuyPrice")
            if isinstance(rebuy_price, (int, float)) and rebuy_price > 0:
                source_sell_op_id = str(op["id"])
                has_rebuy = source_sell_op_id in existing_rebuy_sell_ops or (
                    bool(listing_id) and listing_id in existing_rebuy_sources
                )
                if not has_rebuy:
                    self.db.add_pool_operation(
                        market_hash_name=op["market_hash_name"],
                        strategy=op["strategy"],
                        operation_type=OP_REBUY_C5,
                        expected_price=float(rebuy_price),
                        note=_build_note(
                            {
                                "sourceListing": listing_id,
                                "sourceSellOperationId": op["id"],
                                "steamListPrice": steam_list_price,
                            }
                        ),
                    )
                    if listing_id:
                        existing_rebuy_sources.add(listing_id)
                    existing_rebuy_sell_ops.add(source_sell_op_id)
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
        """Use the current imported account's trade URL, never a global fallback."""
        trade_url = self._effective_steam_trade_url()
        if trade_url:
            if self._is_trade_url_for_expected_account(trade_url):
                return trade_url
            expected = self._expected_rebuy_steam_id64() or "unknown"
            actual = _steam_id64_from_trade_url(trade_url) or "unknown"
            print(
                "[警告] 已忽略不匹配的交易链接："
                f"当前执行账号 steam={expected}，tradeUrl 指向 steam={actual}"
            )
            self._steam_trade_url = None
        if not self.steam_client:
            return None
        try:
            url = self.steam_client.get_trade_url()
            if not self._is_trade_url_for_expected_account(url):
                expected = self._expected_rebuy_steam_id64() or "unknown"
                actual = _steam_id64_from_trade_url(url) or "unknown"
                print(
                    "[警告] 自动获取到的交易链接与当前执行账号不一致，"
                    f"当前执行账号 steam={expected}，tradeUrl 指向 steam={actual}"
                )
                return None
            self._steam_trade_url = url
            if self.account:
                self.account_store.update_account(self.account.id, trade_url=url)
            return url
        except Exception as exc:
            print(f"[警告] 自动获取交易链接失败: {exc}")
            return None

    def _execute_rebuys(self) -> int:
        if not self.config.auto_rebuy_enabled:
            return 0
        trade_url = self._resolve_trade_url()
        if not trade_url:
            print("[提示] 当前账号未能获取交易链接，将尝试不带 tradeUrl 直接补仓（C5 使用账号预设链接）")
        pending = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=200)
        rebuy_count = 0
        # 本轮内同一饰品 + 同一原因只推一次，避免同一物品多个补仓单连环刷屏
        notified_this_cycle: set[tuple[str, str]] = set()
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
                    "wall_min_count": self._listing_wall_min_count_for_market_hash_name(
                        op["market_hash_name"]
                    ),
                    "price_offset": self._listing_price_offset_for_market_hash_name(
                        op["market_hash_name"]
                    ),
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
                timeout_triggered = False
                no_matching_since = note.get("noMatchingSince")
                if result.reason == "no_matching_listing":
                    timeout_triggered = self._handle_no_matching_rebuy_timeout(
                        op=op,
                        note=note,
                        result=result,
                    )
                    if timeout_triggered:
                        return rebuy_count
                    no_matching_since = no_matching_since or utc_now_iso()
                # 临时性跳过：保持 pending，下次循环重试；只更新 note 记录原因
                previous_notified_reason = note.get("lastNotifiedSkipReason")
                self.db.update_pool_operation(
                    op["id"],
                    note=_build_note(
                        {
                            **note,
                            "lastSkipReason": result.reason,
                            "lastNotifiedSkipReason": result.reason,
                            "steamPriceNow": result.steam_price_now,
                            "listingRatioNow": result.listing_ratio_now,
                            "noMatchingSince": no_matching_since,
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
                # 去重：只在原因首次出现或原因发生变化时才推 ServerChan，
                # 避免每轮循环（无论是 Steam 掉价、没有合适挂单等持续状态）都刷屏。
                cycle_key = (op["market_hash_name"], result.reason)
                reason_changed = previous_notified_reason != result.reason
                if reason_changed and cycle_key not in notified_this_cycle:
                    self._notify_skip(op["market_hash_name"], result.reason, result)
                    notified_this_cycle.add(cycle_key)
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
                print(f"[补仓跳过] {op['market_hash_name']} | C5价格过高: CNY {result.actual_price}")
                cycle_key = (op["market_hash_name"], result.reason)
                if cycle_key not in notified_this_cycle:
                    self._notify_skip(op["market_hash_name"], result.reason, result)
                    notified_this_cycle.add(cycle_key)
                continue
            if result.success and not result.skipped:
                self.db.update_pool_operation(op["id"], status="completed", actual_price=result.actual_price)
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
                print(
                    f"[补仓] {op['market_hash_name']} | "
                    f"C5买入 CNY {result.actual_price:.2f}"
                )
                rebuy_count += 1
            elif result.skipped:
                self.db.update_pool_operation(op["id"], status="dry_run")
            else:
                self.db.update_pool_operation(op["id"], status="failed")
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_REBUY_FAILED)
                print(f"[补仓失败] {op['market_hash_name']} | 原因: {result.reason}")
                cycle_key = (op["market_hash_name"], f"failed:{result.reason}")
                if self.serverchan and cycle_key not in notified_this_cycle:
                    try:
                        self.serverchan.send(
                            f"[补仓失败] {op['market_hash_name']}",
                            f"原因: {result.reason}\nC5价格: {result.actual_price}\n"
                            f"Steam价格: {result.steam_price_now}",
                        )
                    except Exception:
                        pass
                    notified_this_cycle.add(cycle_key)
        return rebuy_count

    def _handle_no_matching_rebuy_timeout(
        self,
        *,
        op: Any,
        note: dict[str, Any],
        result: Any,
    ) -> bool:
        started_at = _parse_iso(str(note.get("noMatchingSince") or "")) or _parse_iso(op["created_at"])
        if started_at is None:
            return False
        elapsed_seconds = (_now_utc() - started_at).total_seconds()
        if elapsed_seconds < REBUY_NO_MATCHING_TIMEOUT_SECONDS:
            return False

        timeout_hours = REBUY_NO_MATCHING_TIMEOUT_SECONDS / 3600
        timeout_reason = "no_matching_listing_timeout"
        updated_note = {
            **note,
            "lastSkipReason": result.reason,
            "noMatchingSince": started_at.isoformat(),
            "manualRequired": True,
            "timeoutReason": timeout_reason,
            "timeoutHours": timeout_hours,
            "steamPriceNow": result.steam_price_now,
            "listingRatioNow": result.listing_ratio_now,
        }
        self.db.update_pool_operation(
            op["id"],
            status="failed",
            note=_build_note(updated_note),
        )
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_REBUY_FAILED)
        self._stop_requested = True
        self._stop_reason = (
            f"{op['market_hash_name']} 补仓已连续等待超过 {timeout_hours:.0f} 小时"
            "（无满足条件的在售饰品），已停止执行，需人工处理。"
        )
        print(
            f"[补仓超时] {op['market_hash_name']} | "
            f"原因: no_matching_listing 持续超过 {timeout_hours:.0f} 小时 | "
            "已转人工处理"
        )
        if self.serverchan:
            try:
                self.serverchan.send(
                    f"[补仓超时] {op['market_hash_name']}",
                    (
                        f"原因: no_matching_listing 持续超过 {timeout_hours:.0f} 小时\n"
                        "状态: 已停止程序，等待人工处理\n"
                        f"C5价格: {result.actual_price}\n"
                        f"Steam价格: {result.steam_price_now}\n"
                        f"ratio: {result.listing_ratio_now}"
                    ),
                )
            except Exception:
                pass
        return True

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
