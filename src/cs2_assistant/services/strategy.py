"""Strategy engine for inventory-pool based T-tool.

Two strategies share the same base inventory (底仓):

1. 挂刀做T (guadao): listing_ratio LOW → sell on Steam, rebuy cheaply on C5
   - Goal: obtain Steam balance at a discount
2. 导余额做T (transfer): transfer_real_ratio HIGH → use cheap balance to profit
   - Goal: turn discounted balance into profit

Formulas:
  listing_ratio = rebuy_price / steam_after_tax_price
  transfer_real_ratio = listing_ratio × c5_settlement_factor - balance_discount
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cs2_assistant.config import Settings
from cs2_assistant.models import (
    STRATEGY_GUADAO,
    STRATEGY_HOLD,
    STRATEGY_TRANSFER,
    StrategyCandidate,
    StrategyConfig,
    StrategyScanReport,
    guadao_scope_allows_item,
    looks_like_weapon_case_name,
)
from cs2_assistant.services.market import (
    MarketService,
    calculate_listing_ratio,
    calculate_steam_after_tax,
    calculate_transfer_real_ratio,
)
from cs2_assistant.services.pricing import PricingDecision
from cs2_assistant.services.t_yield_scan import (
    build_market_service,
    fetch_all_c5_inventories,
    summarize_inventory_types,
)
from cs2_assistant.utils import ensure_parent_dir, safe_float, utc_now_iso


def _strategy_config_path(settings: Settings) -> Path:
    return settings.db_path.parent / "strategy_config.json"


def load_strategy_config(settings: Settings) -> StrategyConfig:
    path = _strategy_config_path(settings)
    if not path.exists():
        return StrategyConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return StrategyConfig()
    if not isinstance(data, dict):
        return StrategyConfig()
    return StrategyConfig.from_dict(data)


def save_strategy_config(settings: Settings, config: StrategyConfig) -> Path:
    path = _strategy_config_path(settings)
    ensure_parent_dir(path)
    path.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def classify_strategies(
    listing_ratio: float,
    transfer_real_ratio: float,
    config: StrategyConfig,
    *,
    is_weapon_case: bool = False,
    market_hash_name: str = "",
) -> list[str]:
    """Classify which strategies apply to an item based on its ratios."""
    strategies: list[str] = []

    # 挂刀做T: listing_ratio 低 → 有利
    max_listing_ratio = config.guadao_max_listing_ratio_for(market_hash_name)
    if listing_ratio <= max_listing_ratio and guadao_scope_allows_item(
        config.guadao_item_scope,
        is_weapon_case=is_weapon_case,
    ):
        strategies.append(STRATEGY_GUADAO)

    # 导余额做T: transfer_real_ratio 高 → 有利
    if transfer_real_ratio >= config.transfer_min_real_ratio:
        strategies.append(STRATEGY_TRANSFER)

    return strategies




def scan_strategies(
    settings: Settings,
    config: StrategyConfig | None = None,
    *,
    allow_cached_fallback: bool = True,
    cache_max_age_minutes: int | None = 180,
    pool_market_hash_names: list[str] | None = None,
    inventory_payload: dict[str, Any] | None = None,
    weapon_case_market_hash_names: set[str] | None = None,
    steam_request_source: str = "notify",
    refresh_steam_accounts: bool = True,
    steam_orderbook_max_workers: int = 4,
    steam_orderbook_admission_timeout_seconds: float | None = None,
    steam_orderbook_price_resolver: (
        Callable[[str, dict[str, Any]], PricingDecision | None] | None
    ) = None,
) -> StrategyScanReport:
    """Scan the inventory pool and evaluate strategies for each item type.

    Uses the same infrastructure as t-yield scan but applies the two-strategy
    model (guadao / transfer) based on listing_ratio and transfer_real_ratio.
    """
    if config is None:
        config = load_strategy_config(settings)

    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    if not settings.steamdt_api_key and not settings.csqaq_api_token:
        raise RuntimeError("缺少 STEAMDT_API_KEY 或 CSQAQ_API_KEY / CSQAQ_API_TOKEN 环境变量。")

    if inventory_payload is None:
        from cs2_assistant.clients import C5GameClient

        c5_client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
        inventory_payload = fetch_all_c5_inventories(
            c5_client,
            settings,
            allow_cached_fallback=allow_cached_fallback,
            cache_max_age_minutes=cache_max_age_minutes,
        )
    account_lookup = {
        str(account.get("steamId") or "").strip(): (account.get("nickname") or str(account.get("steamId") or "").strip())
        for account in list(inventory_payload.get("accounts") or [])
        if str(account.get("steamId") or "").strip()
    }
    all_inventory_types = summarize_inventory_types(
        list(inventory_payload.get("list") or [])
    )
    if pool_market_hash_names is not None:
        pool_set = {str(name).strip() for name in pool_market_hash_names if str(name).strip()}
        all_inventory_types = [
            row for row in all_inventory_types if row.get("market_hash_name") in pool_set
        ]

    matched_inventory_type_count = len(all_inventory_types)

    if not all_inventory_types:
        return StrategyScanReport(
            generated_at=utc_now_iso(),
            inventory_source=str(inventory_payload.get("source") or "live"),
            config=config,
            guadao_candidates=[],
            transfer_candidates=[],
            hold_items=[],
            all_evaluated=[],
            total_pool_types=matched_inventory_type_count,
            missing_price_count=0,
            item_outcomes=[],
        )

    # Fetch market prices using existing infrastructure
    market_service = build_market_service(
        settings,
        include_c5_purchase_prices=False,
        steam_request_source=steam_request_source,
        refresh_steam_accounts=refresh_steam_accounts,
        steam_orderbook_max_workers=steam_orderbook_max_workers,
        steam_orderbook_admission_timeout_seconds=(
            steam_orderbook_admission_timeout_seconds
        ),
        steam_orderbook_price_resolver=steam_orderbook_price_resolver,
    )
    states = market_service.refresh_items(all_inventory_types)
    state_map = {state.market_hash_name: state for state in states}

    guadao_candidates: list[StrategyCandidate] = []
    transfer_candidates: list[StrategyCandidate] = []
    hold_items: list[StrategyCandidate] = []
    all_evaluated: list[StrategyCandidate] = []
    missing_price_count = 0
    item_outcomes: list[dict[str, Any]] = []

    for item_type in all_inventory_types:
        mhn = item_type["market_hash_name"]
        state = state_map.get(mhn)
        outcome: dict[str, Any] = {
            "name": str(item_type.get("name_cn") or mhn),
            "marketHashName": mhn,
            "status": None,
            "reason": None,
            "stage": None,
            "c5RebuyPrice": None,
            "steamListPrice": None,
            "requestSent": None,
        }
        if state is None:
            outcome.update(
                {
                    "status": "market_state_missing",
                    "reason": "行情服务没有返回该品类的状态记录",
                    "stage": "market_state",
                }
            )
            item_outcomes.append(outcome)
            continue

        # Determine rebuy_price (C5 price - what you'd pay to rebuy on C5)
        rebuy_price = item_type.get("reference_price")
        rebuy_source = "inventory_price"
        if rebuy_price is None:
            rebuy_price = state.c5_sell_price
            rebuy_source = state.c5_price_source or "unknown"
        if rebuy_price is None:
            c5_error = str(state.raw_json.get("c5_batch_error") or "").strip()
            if c5_error:
                outcome.update(
                    {
                        "status": "request_failed",
                        "reason": f"C5 行情请求失败：{c5_error}",
                        "stage": "c5_price",
                        "requestSent": True,
                    }
                )
            else:
                missing_price_count += 1
                outcome.update(
                    {
                        "status": "c5_price_missing",
                        "reason": "C5 行情已读取，但没有可用补仓价",
                        "stage": "c5_price",
                        "requestSent": True,
                    }
                )
            item_outcomes.append(outcome)
            continue
        outcome["c5RebuyPrice"] = float(rebuy_price)

        # Determine steam_sell_price
        steam_sell_price = state.steam_sell_price
        if steam_sell_price is None:
            error_type = str(
                state.raw_json.get("steam_orderbook_error_type") or ""
            ).strip()
            error = str(state.raw_json.get("steam_orderbook_error") or "").strip()
            if error_type == "queue_timeout":
                status = "queue_deferred"
                reason = "Steam 队列等待超时，HTTP 请求尚未发送；顺延到下一轮"
                request_sent = False
            elif error_type == "empty_sell_orderbook":
                status = "steam_price_missing"
                reason = "Steam orderbook 请求成功，但公开卖盘为空"
                request_sent = True
                missing_price_count += 1
            elif error_type:
                status = "request_failed"
                reason = f"Steam orderbook 请求失败：{error or error_type}"
                request_sent = error_type != "queue_timeout"
            else:
                status = "steam_price_missing"
                reason = "Steam orderbook 没有返回可用卖盘价"
                request_sent = None
                missing_price_count += 1
            outcome.update(
                {
                    "status": status,
                    "reason": reason,
                    "stage": "steam_orderbook",
                    "requestSent": request_sent,
                }
            )
            item_outcomes.append(outcome)
            continue
        if not config.dry_run and state.steam_price_source != "steam_orderbook":
            outcome.update(
                {
                    "status": "steam_price_not_orderbook",
                    "reason": (
                        f"Steam 价格来源为 {state.steam_price_source or 'unknown'}，"
                        "真实扫描拒绝使用"
                    ),
                    "stage": "steam_price_source",
                    "requestSent": True,
                }
            )
            item_outcomes.append(outcome)
            continue
        outcome["steamListPrice"] = float(steam_sell_price)
        outcome["requestSent"] = True

        # Min price filter
        if rebuy_price < config.min_price:
            outcome.update(
                {
                    "status": "below_min_price",
                    "reason": (
                        f"C5 补仓价 ¥{float(rebuy_price):.2f} "
                        f"低于扫描下限 ¥{float(config.min_price):.2f}"
                    ),
                    "stage": "minimum_price_filter",
                }
            )
            item_outcomes.append(outcome)
            continue

        # Calculate strategy metrics
        steam_after_tax = calculate_steam_after_tax(
            steam_sell_price, steam_net_factor=config.steam_net_factor
        )
        if steam_after_tax is None:
            outcome.update(
                {
                    "status": "calculation_failed",
                    "reason": "Steam 税后到手价无法计算",
                    "stage": "steam_after_tax",
                }
            )
            item_outcomes.append(outcome)
            continue

        listing_ratio = calculate_listing_ratio(
            rebuy_price,
            steam_sell_price,
            steam_net_factor=config.steam_net_factor,
        )
        if listing_ratio is None:
            outcome.update(
                {
                    "status": "calculation_failed",
                    "reason": "挂刀比例无法计算",
                    "stage": "listing_ratio",
                }
            )
            item_outcomes.append(outcome)
            continue

        transfer_real_ratio = calculate_transfer_real_ratio(
            listing_ratio,
            c5_settlement_factor=config.c5_settlement_factor,
            balance_discount=config.balance_discount,
        )
        if transfer_real_ratio is None:
            outcome.update(
                {
                    "status": "calculation_failed",
                    "reason": "导余额收益比例无法计算",
                    "stage": "transfer_ratio",
                }
            )
            item_outcomes.append(outcome)
            continue

        item_is_weapon_case = (
            (weapon_case_market_hash_names is not None and mhn in weapon_case_market_hash_names)
            or looks_like_weapon_case_name(mhn)
            or looks_like_weapon_case_name(state.name_cn)
            or looks_like_weapon_case_name(item_type.get("name_cn"))
        )

        # Classify strategies
        strategies = classify_strategies(
            listing_ratio,
            transfer_real_ratio,
            config,
            is_weapon_case=item_is_weapon_case,
            market_hash_name=mhn,
        )

        steam_ids = item_type.get("steam_ids") or []
        steam_accounts = [
            str(account_lookup.get(str(steam_id).strip()) or str(steam_id).strip())
            for steam_id in steam_ids
            if str(steam_id).strip()
        ]

        candidate = StrategyCandidate(
            name=state.name_cn or item_type["name_cn"],
            market_hash_name=mhn,
            inventory_count=int(item_type["inventory_count"]),
            tradable_count=int(item_type["tradable_count"]),
            rebuy_price=float(rebuy_price),
            rebuy_price_source=rebuy_source or "unknown",
            steam_sell_price=float(steam_sell_price),
            steam_price_source=state.steam_price_source or "unknown",
            steam_after_tax_price=float(steam_after_tax),
            listing_ratio=float(listing_ratio),
            transfer_real_ratio=float(transfer_real_ratio),
            recommended_strategies=strategies,
            steam_accounts=steam_accounts,
        )

        all_evaluated.append(candidate)
        outcome.update(
            {
                "name": candidate.name,
                "status": "evaluated",
                "reason": "价格齐全，已完成策略评估",
                "stage": "strategy_evaluation",
                "listingRatio": round(float(candidate.listing_ratio), 6),
                "listingRatioPct": round(float(candidate.listing_ratio_pct), 2),
            }
        )
        item_outcomes.append(outcome)
        if STRATEGY_GUADAO in strategies:
            guadao_candidates.append(candidate)
        if STRATEGY_TRANSFER in strategies:
            transfer_candidates.append(candidate)
        if not strategies:
            hold_items.append(candidate)

    # Sort: guadao by listing_ratio ASC (lower is better)
    guadao_candidates.sort(key=lambda c: c.listing_ratio)
    # Sort: transfer by transfer_real_ratio DESC (higher is better)
    transfer_candidates.sort(key=lambda c: c.transfer_real_ratio, reverse=True)
    # Sort all by listing_ratio ASC
    all_evaluated.sort(key=lambda c: c.listing_ratio)

    return StrategyScanReport(
        generated_at=utc_now_iso(),
        inventory_source=str(inventory_payload.get("source") or "live"),
        config=config,
        guadao_candidates=guadao_candidates,
        transfer_candidates=transfer_candidates,
        hold_items=hold_items,
        all_evaluated=all_evaluated,
        total_pool_types=matched_inventory_type_count,
        missing_price_count=missing_price_count,
        item_outcomes=item_outcomes,
    )
