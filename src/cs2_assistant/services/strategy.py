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
from typing import Any

from cs2_assistant.config import Settings
from cs2_assistant.models import (
    STRATEGY_GUADAO,
    STRATEGY_HOLD,
    STRATEGY_TRANSFER,
    StrategyCandidate,
    StrategyConfig,
    StrategyScanReport,
)
from cs2_assistant.services.market import (
    MarketService,
    calculate_listing_ratio,
    calculate_steam_after_tax,
    calculate_transfer_real_ratio,
)
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
        data = json.loads(path.read_text(encoding="utf-8"))
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
) -> list[str]:
    """Classify which strategies apply to an item based on its ratios."""
    strategies: list[str] = []

    # 挂刀做T: listing_ratio 低 → 有利
    if listing_ratio <= config.guadao_max_listing_ratio:
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

    pool_total = len(pool_market_hash_names) if pool_market_hash_names is not None else len(all_inventory_types)

    if not all_inventory_types:
        return StrategyScanReport(
            generated_at=utc_now_iso(),
            inventory_source=str(inventory_payload.get("source") or "live"),
            config=config,
            guadao_candidates=[],
            transfer_candidates=[],
            hold_items=[],
            all_evaluated=[],
            total_pool_types=pool_total,
            missing_price_count=0,
        )

    # Fetch market prices using existing infrastructure
    market_service = build_market_service(settings, include_c5_purchase_prices=False)
    states = market_service.refresh_items(all_inventory_types)
    state_map = {state.market_hash_name: state for state in states}

    guadao_candidates: list[StrategyCandidate] = []
    transfer_candidates: list[StrategyCandidate] = []
    hold_items: list[StrategyCandidate] = []
    all_evaluated: list[StrategyCandidate] = []
    missing_price_count = 0

    for item_type in all_inventory_types:
        mhn = item_type["market_hash_name"]
        state = state_map.get(mhn)
        if state is None:
            missing_price_count += 1
            continue

        # Determine rebuy_price (C5 price - what you'd pay to rebuy on C5)
        rebuy_price = item_type.get("reference_price")
        rebuy_source = "inventory_price"
        if rebuy_price is None:
            rebuy_price = state.c5_sell_price
            rebuy_source = state.c5_price_source or "unknown"
        if rebuy_price is None:
            missing_price_count += 1
            continue

        # Determine steam_sell_price
        steam_sell_price = state.steam_sell_price
        if steam_sell_price is None:
            missing_price_count += 1
            continue

        # Min price filter
        if rebuy_price < config.min_price:
            continue

        # Calculate strategy metrics
        steam_after_tax = calculate_steam_after_tax(
            steam_sell_price, steam_net_factor=config.steam_net_factor
        )
        if steam_after_tax is None:
            missing_price_count += 1
            continue

        listing_ratio = calculate_listing_ratio(
            rebuy_price,
            steam_sell_price,
            steam_net_factor=config.steam_net_factor,
        )
        if listing_ratio is None:
            continue

        transfer_real_ratio = calculate_transfer_real_ratio(
            listing_ratio,
            c5_settlement_factor=config.c5_settlement_factor,
            balance_discount=config.balance_discount,
        )
        if transfer_real_ratio is None:
            continue

        # Classify strategies
        strategies = classify_strategies(listing_ratio, transfer_real_ratio, config)

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
        total_pool_types=pool_total,
        missing_price_count=missing_price_count,
    )
