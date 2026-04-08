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
POOL_STATUS_LISTED = "listed"             # 已挂卖 Steam
POOL_STATUS_SOLD = "sold"                 # 已卖出
POOL_STATUS_PENDING_REBUY = "pending_rebuy"  # 待补仓

POOL_STATUS_LABELS: dict[str, str] = {
    POOL_STATUS_HOLDING: "持有中",
    POOL_STATUS_LISTED: "已挂卖",
    POOL_STATUS_SOLD: "已卖出",
    POOL_STATUS_PENDING_REBUY: "待补仓",
}

OP_SELL_STEAM = "sell_on_steam"       # 在 Steam 挂卖
OP_REBUY_C5 = "rebuy_on_c5"          # 在 C5 补仓
OP_TRANSFER_BUY = "transfer_buy"     # 导余额：用余额在 Steam 买入
OP_TRANSFER_SELL = "transfer_sell"   # 导余额：在 C5 卖出


@dataclass(slots=True)
class StrategyConfig:
    """策略配置"""
    steam_net_factor: float = 0.85
    c5_settlement_factor: float = 0.869
    balance_discount: float = 0.73
    guadao_max_listing_ratio: float = 0.95
    transfer_min_real_ratio: float = 0.05
    min_price: float = 10.0
    poll_interval_minutes: int = 30
    top_n: int = 20

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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyConfig":
        return cls(
            steam_net_factor=float(data.get("steamNetFactor", 0.85)),
            c5_settlement_factor=float(data.get("c5SettlementFactor", 0.869)),
            balance_discount=float(data.get("balanceDiscount", 0.73)),
            guadao_max_listing_ratio=float(data.get("guadaoMaxListingRatio", 0.95)),
            transfer_min_real_ratio=float(data.get("transferMinRealRatio", 0.05)),
            min_price=float(data.get("minPrice", 10.0)),
            poll_interval_minutes=int(data.get("pollIntervalMinutes", 30)),
            top_n=int(data.get("topN", 20)),
        )


@dataclass(slots=True)
class StrategyCandidate:
    """单个饰品的策略评估结果"""
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
    guadao_profit_per_unit: float
    transfer_profit_per_unit: float

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
            "guadaoProfitPerUnit": round(self.guadao_profit_per_unit, 2),
            "transferProfitPerUnit": round(self.transfer_profit_per_unit, 2),
        }
        if rank is not None:
            payload["rank"] = rank
        return payload


@dataclass(slots=True)
class StrategyScanReport:
    """策略扫描报告"""
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
