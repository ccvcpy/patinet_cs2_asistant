from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CatalogItem:
    market_hash_name: str
    name_cn: str
    c5_item_id: str | None = None
    steam_item_id: str | None = None
    raw_json: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketState:
    market_hash_name: str
    name_cn: str | None = None
    c5_sell_price: float | None = None
    c5_sell_count: int | None = None
    c5_bid_price: float | None = None
    c5_bid_count: int | None = None
    c5_item_id: str | None = None
    c5_website: str | None = None
    steam_sell_price: float | None = None
    steam_sell_count: int | None = None
    steam_bid_price: float | None = None
    steam_bid_count: int | None = None
    c5_price_source: str | None = None
    steam_price_source: str | None = None
    ratio: float | None = None
    raw_json: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BasketState:
    name: str
    total_value: float
    components: list[dict[str, Any]]


@dataclass(slots=True)
class TriggeredAlert:
    rule_id: int
    target_type: str
    target_key: str
    metric: str
    observed_value: float
    threshold: float
    message: str


@dataclass(slots=True)
class NotificationMessage:
    title: str
    body: str


# ---------------------------------------------------------------------------
# Strategy models (inventory-pool based T-tool)
# ---------------------------------------------------------------------------

STRATEGY_GUADAO = "guadao"        # 挂刀做T
STRATEGY_TRANSFER = "transfer"    # 导余额做T
STRATEGY_HOLD = "hold"            # 持有不动（不满足任何策略）

STRATEGY_LABELS: dict[str, str] = {
    STRATEGY_GUADAO: "挂刀做T",
    STRATEGY_TRANSFER: "导余额做T",
    STRATEGY_HOLD: "持有",
}

POOL_STATUS_HOLDING = "holding"           # 持有中
POOL_STATUS_LISTING_PENDING = "listing_pending"  # 挂卖待确认
POOL_STATUS_LISTED = "listed"             # 已挂单 Steam
POOL_STATUS_SOLD = "sold"                 # 已卖出
POOL_STATUS_PENDING_REBUY = "pending_rebuy"  # 待补仓
POOL_STATUS_REBUY_FAILED = "rebuy_failed"     # 补仓失败
POOL_STATUS_TRANSFER_BUYING = "transfer_buying"      # transfer: Steam 已买，等待卖出旧底仓
POOL_STATUS_TRANSFER_HOLDING = "transfer_holding"    # transfer: 旧底仓已卖，replacement 冷却中
POOL_STATUS_TRANSFER_LISTED_C5 = "transfer_listed_c5"  # transfer: 旧底仓已挂 C5
POOL_STATUS_TRANSFER_SOLD = "transfer_sold"          # transfer: 旧底仓已卖，等待 replacement 对齐

POOL_STATUS_LABELS: dict[str, str] = {
    POOL_STATUS_LISTING_PENDING: "listing_pending",
    POOL_STATUS_REBUY_FAILED: "rebuy_failed",
    POOL_STATUS_HOLDING: "holding",
    POOL_STATUS_LISTED: "listed",
    POOL_STATUS_SOLD: "sold",
    POOL_STATUS_PENDING_REBUY: "pending_rebuy",
    POOL_STATUS_TRANSFER_BUYING: "transfer_buying",
    POOL_STATUS_TRANSFER_HOLDING: "transfer_holding",
    POOL_STATUS_TRANSFER_LISTED_C5: "transfer_listed_c5",
    POOL_STATUS_TRANSFER_SOLD: "transfer_sold",
}

OP_SELL_STEAM = "sell_on_steam"       # 在 Steam 挂卖
OP_REBUY_C5 = "rebuy_on_c5"          # 在 C5 补仓
OP_TRANSFER_BUY = "transfer_buy"     # 导余额：用余额在 Steam 买入
OP_TRANSFER_SELL = "transfer_sell"   # 导余额：在 C5 卖出


@dataclass(slots=True)
class StrategyConfig:
    #
    steam_net_factor: float = 0.869
    c5_settlement_factor: float = 0.869
    balance_discount: float = 0.73
    guadao_max_listing_ratio: float = 0.95
    transfer_min_real_ratio: float = 0.05
    min_price: float = 10.0
    poll_interval_minutes: int = 30
    top_n: int = 20
    execution_enabled: bool = False
    auto_list_enabled: bool = True
    auto_rebuy_enabled: bool = True
    price_tolerance_pct: float = 1.0
    max_list_per_cycle: int = 5
    max_buy_per_cycle: int = 3
    cycle_interval_minutes: int = 15
    listing_check_interval_minutes: int = 5
    dry_run: bool = True
    steam_context_id: str = "2"
    steam_currency: int = 23
    steam_country: str = "CN"
    steam_language: str = "schinese"
    listing_wall_min_count: int = 20
    listing_price_offset: float = 0.01
    force_refresh_before_execution: bool = True
    steam_price_cache_ttl: float = 60.0
    verify_steam_before_rebuy: bool = True
    rebuy_steam_drop_tolerance_pct: float = 5.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "steamNetFactor": self.steam_net_factor,
            "c5SettlementFactor": self.c5_settlement_factor,
            "balanceDiscount": self.balance_discount,
            "guadaoMaxListingRatio": self.guadao_max_listing_ratio,
            "transferMinRealRatio": self.transfer_min_real_ratio,
            "minPrice": self.min_price,
            "pollIntervalMinutes": self.poll_interval_minutes,
            "topN": self.top_n,
            "executionEnabled": self.execution_enabled,
            "autoListEnabled": self.auto_list_enabled,
            "autoRebuyEnabled": self.auto_rebuy_enabled,
            "priceTolerancePct": self.price_tolerance_pct,
            "maxListPerCycle": self.max_list_per_cycle,
            "maxBuyPerCycle": self.max_buy_per_cycle,
            "cycleIntervalMinutes": self.cycle_interval_minutes,
            "listingCheckIntervalMinutes": self.listing_check_interval_minutes,
            "dryRun": self.dry_run,
            "steamContextId": self.steam_context_id,
            "steamCurrency": self.steam_currency,
            "steamCountry": self.steam_country,
            "steamLanguage": self.steam_language,
            "listingWallMinCount": self.listing_wall_min_count,
            "listingPriceOffset": self.listing_price_offset,
            "forceRefreshBeforeExecution": self.force_refresh_before_execution,
            "steamPriceCacheTtl": self.steam_price_cache_ttl,
            "verifySteamBeforeRebuy": self.verify_steam_before_rebuy,
            "rebuySteamDropTolerancePct": self.rebuy_steam_drop_tolerance_pct,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyConfig":
        def _as_bool(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "y", "on"}
            return default

        return cls(
            steam_net_factor=float(data.get("steamNetFactor", 0.869)),
            c5_settlement_factor=float(data.get("c5SettlementFactor", 0.869)),
            balance_discount=float(data.get("balanceDiscount", 0.73)),
            guadao_max_listing_ratio=float(data.get("guadaoMaxListingRatio", 0.95)),
            transfer_min_real_ratio=float(data.get("transferMinRealRatio", 0.05)),
            min_price=float(data.get("minPrice", 10.0)),
            poll_interval_minutes=int(data.get("pollIntervalMinutes", 30)),
            top_n=int(data.get("topN", 20)),
            execution_enabled=_as_bool(data.get("executionEnabled"), False),
            auto_list_enabled=_as_bool(data.get("autoListEnabled"), True),
            auto_rebuy_enabled=_as_bool(data.get("autoRebuyEnabled"), True),
            price_tolerance_pct=float(data.get("priceTolerancePct", 1.0)),
            max_list_per_cycle=int(data.get("maxListPerCycle", 5)),
            max_buy_per_cycle=int(data.get("maxBuyPerCycle", 3)),
            cycle_interval_minutes=int(data.get("cycleIntervalMinutes", 15)),
            listing_check_interval_minutes=int(data.get("listingCheckIntervalMinutes", 5)),
            dry_run=_as_bool(data.get("dryRun"), True),
            steam_context_id=str(data.get("steamContextId", "2")),
            steam_currency=int(data.get("steamCurrency", 23)),
            steam_country=str(data.get("steamCountry", "CN")),
            steam_language=str(data.get("steamLanguage", "schinese")),
            listing_wall_min_count=int(data.get("listingWallMinCount", 20)),
            listing_price_offset=float(data.get("listingPriceOffset", 0.01)),
            force_refresh_before_execution=_as_bool(data.get("forceRefreshBeforeExecution"), True),
            steam_price_cache_ttl=float(data.get("steamPriceCacheTtl", 60.0)),
            verify_steam_before_rebuy=_as_bool(data.get("verifySteamBeforeRebuy"), True),
            rebuy_steam_drop_tolerance_pct=float(data.get("rebuySteamDropTolerancePct", 5.0)),
        )


@dataclass(slots=True)
class StrategyCandidate:
    #
    name: str
    market_hash_name: str
    inventory_count: int
    tradable_count: int
    rebuy_price: float
    rebuy_price_source: str
    steam_sell_price: float
    steam_price_source: str
    steam_after_tax_price: float
    listing_ratio: float
    transfer_real_ratio: float
    recommended_strategies: list[str]
    steam_accounts: list[str]

    @property
    def cooldown_count(self) -> int:
        return max(0, self.inventory_count - self.tradable_count)

    @property
    def primary_strategy(self) -> str:
        if not self.recommended_strategies:
            return STRATEGY_HOLD
        return self.recommended_strategies[0]

    @property
    def primary_strategy_label(self) -> str:
        return STRATEGY_LABELS.get(self.primary_strategy, self.primary_strategy)

    @property
    def listing_ratio_pct(self) -> float:
        return self.listing_ratio * 100

    @property
    def transfer_real_ratio_pct(self) -> float:
        return self.transfer_real_ratio * 100

    def to_dict(self, *, rank: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "marketHashName": self.market_hash_name,
            "inventoryCount": self.inventory_count,
            "tradableCount": self.tradable_count,
            "cooldownCount": self.cooldown_count,
            "rebuyPrice": self.rebuy_price,
            "rebuyPriceSource": self.rebuy_price_source,
            "steamSellPrice": self.steam_sell_price,
            "steamPriceSource": self.steam_price_source,
            "steamAfterTaxPrice": round(self.steam_after_tax_price, 2),
            "listingRatio": round(self.listing_ratio, 4),
            "listingRatioPct": round(self.listing_ratio_pct, 2),
            "transferRealRatio": round(self.transfer_real_ratio, 4),
            "transferRealRatioPct": round(self.transfer_real_ratio_pct, 2),
            "recommendedStrategies": self.recommended_strategies,
            "primaryStrategy": self.primary_strategy,
            "primaryStrategyLabel": self.primary_strategy_label,
            "steamAccounts": list(self.steam_accounts),
        }
        if rank is not None:
            payload["rank"] = rank
        return payload


@dataclass(slots=True)
class StrategyScanReport:
    # Strategy scan report
    generated_at: str
    inventory_source: str
    config: StrategyConfig
    guadao_candidates: list[StrategyCandidate]
    transfer_candidates: list[StrategyCandidate]
    hold_items: list[StrategyCandidate]
    all_evaluated: list[StrategyCandidate]
    total_pool_types: int
    missing_price_count: int

    @property
    def guadao_count(self) -> int:
        return len(self.guadao_candidates)

    @property
    def transfer_count(self) -> int:
        return len(self.transfer_candidates)

    @property
    def hold_count(self) -> int:
        return len(self.hold_items)
