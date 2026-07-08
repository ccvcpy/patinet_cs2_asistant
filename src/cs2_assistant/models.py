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
STRATEGY_PROFIT_TRADE = "profit_trade"  # 搬砖做T
STRATEGY_HOLD = "hold"            # 持有不动（不满足任何策略）

STRATEGY_LABELS: dict[str, str] = {
    STRATEGY_GUADAO: "挂刀导余额",
    STRATEGY_TRANSFER: "旧导余额做T",
    STRATEGY_PROFIT_TRADE: "搬砖做T",
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
OP_PROFIT_AUDIT = "profit_audit"      # 搬砖做T：机会审计
OP_PROFIT_LOCK_ASSET = "profit_lock_asset"  # 搬砖做T：锁定旧底仓 A
OP_PROFIT_BUY_STEAM = "profit_buy_steam"    # 搬砖做T：Steam 买入替换 B
OP_PROFIT_SELL_C5 = "profit_sell_c5"        # 搬砖做T：C5 卖出 A

GUADAO_ITEM_SCOPE_CASE_ONLY = "case_only"
GUADAO_ITEM_SCOPE_NON_CASE_ONLY = "non_case_only"
GUADAO_ITEM_SCOPES = {
    GUADAO_ITEM_SCOPE_CASE_ONLY,
    GUADAO_ITEM_SCOPE_NON_CASE_ONLY,
}


def normalize_guadao_item_scope(value: object) -> str:
    raw = str(value or GUADAO_ITEM_SCOPE_CASE_ONLY).strip().lower().replace("-", "_")
    aliases = {
        "case": GUADAO_ITEM_SCOPE_CASE_ONLY,
        "cases": GUADAO_ITEM_SCOPE_CASE_ONLY,
        "case_only": GUADAO_ITEM_SCOPE_CASE_ONLY,
        "box": GUADAO_ITEM_SCOPE_CASE_ONLY,
        "boxes": GUADAO_ITEM_SCOPE_CASE_ONLY,
        "non_case": GUADAO_ITEM_SCOPE_NON_CASE_ONLY,
        "non_cases": GUADAO_ITEM_SCOPE_NON_CASE_ONLY,
        "non_case_only": GUADAO_ITEM_SCOPE_NON_CASE_ONLY,
    }
    return aliases.get(raw, GUADAO_ITEM_SCOPE_CASE_ONLY)


def looks_like_weapon_case_name(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized.endswith(" case") or "武器箱" in value


def guadao_scope_allows_item(scope: object, *, is_weapon_case: bool) -> bool:
    normalized = normalize_guadao_item_scope(scope)
    if normalized == GUADAO_ITEM_SCOPE_CASE_ONLY:
        return is_weapon_case
    return not is_weapon_case


@dataclass(slots=True)
class StrategyConfig:
    #
    steam_net_factor: float = 0.869
    c5_settlement_factor: float = 0.869
    balance_discount: float = 0.73
    guadao_max_listing_ratio: float = 0.95
    transfer_min_real_ratio: float = 0.05
    profit_trade_enabled: bool = False
    profit_trade_allow_real_execution: bool = False
    profit_trade_allow_reprice_execution: bool = False
    profit_trade_balance_discount: float = 0.69
    profit_trade_min_roi: float = 0.07
    profit_trade_min_item_value: float = 50.0
    profit_trade_max_buy_per_cycle: int = 1
    profit_trade_daily_steam_budget: float = 1000.0
    profit_trade_scan_max_items: int = 80
    profit_trade_reservation_seconds: int = 60
    profit_trade_steam_buy_price_tolerance_pct: float = 1.0
    profit_trade_c5_current_sale_net_factor: float = 0.99
    profit_trade_recent_sold_fee_already_deducted: bool = True
    profit_trade_liquidity_min_recent_sales: int = 3
    profit_trade_require_c5_recent_sales: bool = True
    profit_trade_require_c5_market_depth: bool = True
    profit_trade_c5_min_on_sale_count: int = 3
    profit_trade_c5_min_purchase_count: int = 1
    profit_trade_c5_min_purchase_sell_ratio: float = 0.70
    profit_trade_c5_max_listing_premium_pct: float = 3.0
    profit_trade_manual_review_roi: float = 0.20
    profit_trade_reprice_enabled: bool = True
    profit_trade_reprice_discount_pct: float = 1.0
    profit_trade_reprice_min_discount: float = 0.1
    profit_trade_reprice_max_discount: float = 50.0
    profit_trade_reprice_cooldown_hours: float = 3.0
    profit_trade_stale_reprice_after_hours: float = 12.0
    profit_trade_stale_manual_review_after_hours: float = 24.0
    profit_trade_stale_min_roi: float = 0.04
    profit_trade_stale_price_offset: float = 0.01
    profit_trade_sticker_slab_status: str = "blocked"
    profit_trade_sticker_status: str = "blocked"
    profit_trade_protected_asset_ids: list[str] | None = None
    profit_trade_protected_market_hash_names: list[str] | None = None
    profit_trade_protected_steam_ids: list[str] | None = None
    profit_trade_ai_audit_enabled: bool = False
    profit_trade_ai_audit_provider: str = "deepseek"
    profit_trade_ai_audit_model: str = ""
    min_price: float = 10.0
    poll_interval_minutes: int = 30
    top_n: int = 20
    execution_enabled: bool = False
    auto_list_enabled: bool = True
    auto_rebuy_enabled: bool = True
    guadao_item_scope: str = GUADAO_ITEM_SCOPE_CASE_ONLY
    price_tolerance_pct: float = 1.0
    max_list_per_cycle: int = 5
    max_transfer_buy_per_cycle: int = 3
    cycle_interval_minutes: int = 15
    listing_check_interval_minutes: float = 5.0
    dry_run: bool = True
    steam_context_id: str = "2"
    steam_currency: int = 23
    steam_country: str = "CN"
    steam_language: str = "schinese"
    listing_wall_min_count: int = 20
    listing_price_offset: float = 0.01
    case_listing_price_offset: float | None = -0.01
    case_max_open_guadao_count: int = 100
    force_refresh_before_execution: bool = True
    steam_price_cache_ttl: float = 60.0
    rebuy_steam_drop_tolerance_pct: float = 5.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "common": {
                "steamNetFactor": self.steam_net_factor,
                "c5SettlementFactor": self.c5_settlement_factor,
                "balanceDiscount": self.balance_discount,
                "minPrice": self.min_price,
                "executionEnabled": self.execution_enabled,
                "dryRun": self.dry_run,
                "cycleIntervalMinutes": self.cycle_interval_minutes,
                "listingCheckIntervalMinutes": self.listing_check_interval_minutes,
                "steamContextId": self.steam_context_id,
                "steamCurrency": self.steam_currency,
                "steamCountry": self.steam_country,
                "steamLanguage": self.steam_language,
                "forceRefreshBeforeExecution": self.force_refresh_before_execution,
                "steamPriceCacheTtl": self.steam_price_cache_ttl,
            },
            "guadaoBalance": {
                "guadaoMaxListingRatio": self.guadao_max_listing_ratio,
                "autoListEnabled": self.auto_list_enabled,
                "autoRebuyEnabled": self.auto_rebuy_enabled,
                "guadaoItemScope": normalize_guadao_item_scope(self.guadao_item_scope),
                "priceTolerancePct": self.price_tolerance_pct,
                "maxListPerCycle": self.max_list_per_cycle,
                "listingWallMinCount": self.listing_wall_min_count,
                "listingPriceOffset": self.listing_price_offset,
                "caseListingPriceOffset": self.case_listing_price_offset,
                "caseMaxOpenGuadaoCount": self.case_max_open_guadao_count,
                "rebuySteamDropTolerancePct": self.rebuy_steam_drop_tolerance_pct,
            },
            "profitTrade": {
                "enabled": self.profit_trade_enabled,
                "allowRealExecution": self.profit_trade_allow_real_execution,
                "allowRepriceExecution": self.profit_trade_allow_reprice_execution,
                "balanceDiscount": self.profit_trade_balance_discount,
                "minRoi": self.profit_trade_min_roi,
                "minItemValue": self.profit_trade_min_item_value,
                "maxBuyPerCycle": self.profit_trade_max_buy_per_cycle,
                "dailySteamBudget": self.profit_trade_daily_steam_budget,
                "scanMaxItems": self.profit_trade_scan_max_items,
                "reservationSeconds": self.profit_trade_reservation_seconds,
                "steamBuyPriceTolerancePct": self.profit_trade_steam_buy_price_tolerance_pct,
                "c5CurrentSaleNetFactor": self.profit_trade_c5_current_sale_net_factor,
                "recentSoldFeeAlreadyDeducted": self.profit_trade_recent_sold_fee_already_deducted,
                "liquidityMinRecentSales": self.profit_trade_liquidity_min_recent_sales,
                "requireC5RecentSales": self.profit_trade_require_c5_recent_sales,
                "requireC5MarketDepth": self.profit_trade_require_c5_market_depth,
                "c5MinOnSaleCount": self.profit_trade_c5_min_on_sale_count,
                "c5MinPurchaseCount": self.profit_trade_c5_min_purchase_count,
                "c5MinPurchaseSellRatio": self.profit_trade_c5_min_purchase_sell_ratio,
                "c5MaxListingPremiumPct": self.profit_trade_c5_max_listing_premium_pct,
                "manualReviewRoi": self.profit_trade_manual_review_roi,
                "repriceEnabled": self.profit_trade_reprice_enabled,
                "repriceDiscountPct": self.profit_trade_reprice_discount_pct,
                "repriceMinDiscount": self.profit_trade_reprice_min_discount,
                "repriceMaxDiscount": self.profit_trade_reprice_max_discount,
                "repriceCooldownHours": self.profit_trade_reprice_cooldown_hours,
                "staleRepriceAfterHours": self.profit_trade_stale_reprice_after_hours,
                "staleManualReviewAfterHours": self.profit_trade_stale_manual_review_after_hours,
                "staleMinRoi": self.profit_trade_stale_min_roi,
                "stalePriceOffset": self.profit_trade_stale_price_offset,
                "stickerSlabStatus": self.profit_trade_sticker_slab_status,
                "stickerStatus": self.profit_trade_sticker_status,
                "protectedAssetIds": list(self.profit_trade_protected_asset_ids or []),
                "protectedMarketHashNames": list(self.profit_trade_protected_market_hash_names or []),
                "protectedSteamIds": list(self.profit_trade_protected_steam_ids or []),
            },
            "notifications": {
                "pollIntervalMinutes": self.poll_interval_minutes,
                "topN": self.top_n,
            },
            "aiAudit": {
                "enabled": self.profit_trade_ai_audit_enabled,
                "provider": self.profit_trade_ai_audit_provider,
                "model": self.profit_trade_ai_audit_model,
            },
            "legacyTransfer": {
                "transferMinRealRatio": self.transfer_min_real_ratio,
                "maxTransferBuyPerCycle": self.max_transfer_buy_per_cycle,
            },
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

        def _section(name: str) -> dict[str, Any]:
            value = data.get(name)
            return value if isinstance(value, dict) else {}

        common = _section("common")
        guadao_balance = _section("guadaoBalance")
        profit_trade = _section("profitTrade")
        notifications = _section("notifications")
        ai_audit = _section("aiAudit")
        legacy_transfer = _section("legacyTransfer")

        def _get(section: dict[str, Any], key: str, default: Any) -> Any:
            if key in section:
                return section[key]
            return data.get(key, default)

        def _as_str_list(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value.strip()] if value.strip() else []
            if isinstance(value, (list, tuple, set)):
                return [str(item).strip() for item in value if str(item).strip()]
            return []

        def _as_item_type_status(status_value: Any, legacy_allow_value: Any) -> str:
            text = str(status_value or "").strip().lower()
            if text in {"active", "enabled", "on", "scan", "include"}:
                return "active"
            if text in {"blocked", "disabled", "off", "hide", "exclude"}:
                return "blocked"
            if legacy_allow_value is not None:
                return "active" if _as_bool(legacy_allow_value, False) else "blocked"
            return "blocked"

        return cls(
            steam_net_factor=float(_get(common, "steamNetFactor", 0.869)),
            c5_settlement_factor=float(_get(common, "c5SettlementFactor", 0.869)),
            balance_discount=float(_get(common, "balanceDiscount", 0.73)),
            guadao_max_listing_ratio=float(_get(guadao_balance, "guadaoMaxListingRatio", 0.95)),
            transfer_min_real_ratio=float(_get(legacy_transfer, "transferMinRealRatio", 0.05)),
            profit_trade_enabled=_as_bool(_get(profit_trade, "enabled", False), False),
            profit_trade_allow_real_execution=_as_bool(
                _get(profit_trade, "allowRealExecution", False),
                False,
            ),
            profit_trade_allow_reprice_execution=_as_bool(
                _get(profit_trade, "allowRepriceExecution", False),
                False,
            ),
            profit_trade_balance_discount=float(
                _get(profit_trade, "balanceDiscount", _get(common, "balanceDiscount", 0.69))
            ),
            profit_trade_min_roi=float(_get(profit_trade, "minRoi", 0.07)),
            profit_trade_min_item_value=float(_get(profit_trade, "minItemValue", 50.0)),
            profit_trade_max_buy_per_cycle=int(_get(profit_trade, "maxBuyPerCycle", 1)),
            profit_trade_daily_steam_budget=float(_get(profit_trade, "dailySteamBudget", 1000.0)),
            profit_trade_scan_max_items=int(_get(profit_trade, "scanMaxItems", 80)),
            profit_trade_reservation_seconds=int(_get(profit_trade, "reservationSeconds", 60)),
            profit_trade_steam_buy_price_tolerance_pct=float(
                _get(profit_trade, "steamBuyPriceTolerancePct", 1.0)
            ),
            profit_trade_c5_current_sale_net_factor=float(
                _get(profit_trade, "c5CurrentSaleNetFactor", 0.99)
            ),
            profit_trade_recent_sold_fee_already_deducted=_as_bool(
                _get(profit_trade, "recentSoldFeeAlreadyDeducted", True),
                True,
            ),
            profit_trade_liquidity_min_recent_sales=int(
                _get(profit_trade, "liquidityMinRecentSales", 3)
            ),
            profit_trade_require_c5_recent_sales=_as_bool(
                _get(profit_trade, "requireC5RecentSales", True),
                True,
            ),
            profit_trade_require_c5_market_depth=_as_bool(
                _get(profit_trade, "requireC5MarketDepth", True),
                True,
            ),
            profit_trade_c5_min_on_sale_count=int(
                _get(profit_trade, "c5MinOnSaleCount", 3)
            ),
            profit_trade_c5_min_purchase_count=int(
                _get(profit_trade, "c5MinPurchaseCount", 1)
            ),
            profit_trade_c5_min_purchase_sell_ratio=float(
                _get(profit_trade, "c5MinPurchaseSellRatio", 0.70)
            ),
            profit_trade_c5_max_listing_premium_pct=float(
                _get(profit_trade, "c5MaxListingPremiumPct", 3.0)
            ),
            profit_trade_manual_review_roi=float(_get(profit_trade, "manualReviewRoi", 0.20)),
            profit_trade_reprice_enabled=_as_bool(
                _get(profit_trade, "repriceEnabled", True),
                True,
            ),
            profit_trade_reprice_discount_pct=float(_get(profit_trade, "repriceDiscountPct", 1.0)),
            profit_trade_reprice_min_discount=float(_get(profit_trade, "repriceMinDiscount", 0.1)),
            profit_trade_reprice_max_discount=float(_get(profit_trade, "repriceMaxDiscount", 50.0)),
            profit_trade_reprice_cooldown_hours=float(_get(profit_trade, "repriceCooldownHours", 3.0)),
            profit_trade_stale_reprice_after_hours=float(
                _get(profit_trade, "staleRepriceAfterHours", 12.0)
            ),
            profit_trade_stale_manual_review_after_hours=float(
                _get(profit_trade, "staleManualReviewAfterHours", 24.0)
            ),
            profit_trade_stale_min_roi=float(_get(profit_trade, "staleMinRoi", 0.04)),
            profit_trade_stale_price_offset=float(_get(profit_trade, "stalePriceOffset", 0.01)),
            profit_trade_sticker_slab_status=_as_item_type_status(
                _get(profit_trade, "stickerSlabStatus", None),
                _get(profit_trade, "allowStickerSlab", None),
            ),
            profit_trade_sticker_status=_as_item_type_status(
                _get(profit_trade, "stickerStatus", None),
                _get(profit_trade, "allowSticker", None),
            ),
            profit_trade_protected_asset_ids=_as_str_list(
                _get(profit_trade, "protectedAssetIds", [])
            ),
            profit_trade_protected_market_hash_names=_as_str_list(
                _get(profit_trade, "protectedMarketHashNames", [])
            ),
            profit_trade_protected_steam_ids=_as_str_list(
                _get(profit_trade, "protectedSteamIds", [])
            ),
            profit_trade_ai_audit_enabled=_as_bool(_get(ai_audit, "enabled", False), False),
            profit_trade_ai_audit_provider=str(_get(ai_audit, "provider", "deepseek")),
            profit_trade_ai_audit_model=str(_get(ai_audit, "model", "")),
            min_price=float(_get(common, "minPrice", 10.0)),
            poll_interval_minutes=int(_get(notifications, "pollIntervalMinutes", 30)),
            top_n=int(_get(notifications, "topN", 20)),
            execution_enabled=_as_bool(_get(common, "executionEnabled", False), False),
            auto_list_enabled=_as_bool(_get(guadao_balance, "autoListEnabled", True), True),
            auto_rebuy_enabled=_as_bool(_get(guadao_balance, "autoRebuyEnabled", True), True),
            guadao_item_scope=normalize_guadao_item_scope(
                _get(guadao_balance, "guadaoItemScope", GUADAO_ITEM_SCOPE_CASE_ONLY)
            ),
            price_tolerance_pct=float(_get(guadao_balance, "priceTolerancePct", 1.0)),
            max_list_per_cycle=int(_get(guadao_balance, "maxListPerCycle", 5)),
            max_transfer_buy_per_cycle=int(
                _get(
                    legacy_transfer,
                    "maxTransferBuyPerCycle",
                    data.get("maxBuyPerCycle", 3),
                )
            ),
            cycle_interval_minutes=int(_get(common, "cycleIntervalMinutes", 15)),
            listing_check_interval_minutes=float(_get(common, "listingCheckIntervalMinutes", 5.0)),
            dry_run=_as_bool(_get(common, "dryRun", True), True),
            steam_context_id=str(_get(common, "steamContextId", "2")),
            steam_currency=int(_get(common, "steamCurrency", 23)),
            steam_country=str(_get(common, "steamCountry", "CN")),
            steam_language=str(_get(common, "steamLanguage", "schinese")),
            listing_wall_min_count=int(_get(guadao_balance, "listingWallMinCount", 20)),
            listing_price_offset=float(_get(guadao_balance, "listingPriceOffset", 0.01)),
            case_listing_price_offset=(
                float(_get(guadao_balance, "caseListingPriceOffset", -0.01))
                if _get(guadao_balance, "caseListingPriceOffset", -0.01) is not None
                else None
            ),
            case_max_open_guadao_count=int(_get(guadao_balance, "caseMaxOpenGuadaoCount", 100)),
            force_refresh_before_execution=_as_bool(
                _get(common, "forceRefreshBeforeExecution", True),
                True,
            ),
            steam_price_cache_ttl=float(_get(common, "steamPriceCacheTtl", 60.0)),
            rebuy_steam_drop_tolerance_pct=float(
                _get(guadao_balance, "rebuySteamDropTolerancePct", 5.0)
            ),
        )

    @property
    def max_buy_per_cycle(self) -> int:
        return self.max_transfer_buy_per_cycle

    @max_buy_per_cycle.setter
    def max_buy_per_cycle(self, value: int) -> None:
        self.max_transfer_buy_per_cycle = int(value)


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
