from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cs2_assistant.accounts import Account, AccountStore
from cs2_assistant.accounts.steam_auth import try_steam_auto_relogin
from cs2_assistant.clients import C5GameClient, C5GameError, SteamMarketClient, SteamMarketError
from cs2_assistant.clients.serverchan import ServerChanClient
from cs2_assistant.config import PROJECT_ROOT, Settings
from cs2_assistant.db import Database
from cs2_assistant.models import MarketState, StrategyConfig
from cs2_assistant.services.market import MarketService
from cs2_assistant.services.pricing import summarize_orderbook_prices
from cs2_assistant.services.strategy import load_strategy_config, save_strategy_config
from cs2_assistant.services.t_yield_scan import (
    fetch_all_c5_inventories,
    summarize_inventory_types,
)
from cs2_assistant.utils import ensure_parent_dir, safe_float, safe_int, utc_now_iso


PROFIT_TRADE_OWNER = "profit_trade"

PROFIT_TRADE_STEPS: list[dict[str, Any]] = [
    {"key": "discovered", "label": "发现机会", "index": 0},
    {"key": "audited", "label": "审计通过", "index": 1},
    {"key": "asset_locked", "label": "锁定A", "index": 2},
    {"key": "steam_bought", "label": "买入B", "index": 3},
    {"key": "c5_listed", "label": "C5上架", "index": 4},
    {"key": "c5_sold", "label": "C5售出", "index": 5},
    {"key": "settled", "label": "收益结算", "index": 6},
]

TERMINAL_PROFIT_TRADE_STATUSES = {"completed", "failed", "manual_required", "cancelled"}
PRE_STEAM_BUY_PROFIT_TRADE_STATUSES = {"candidate", "audited", "locked"}
PRE_STEAM_BUY_STEP_KEYS = {"discovered", "audited", "asset_locked"}
STEAM_BUY_VERIFY_ATTEMPTS = 8
STEAM_BUY_VERIFY_DELAY_SECONDS = 2.0
STEAM_BUY_LISTING_RETRY_ATTEMPTS = 3
STEAM_BUY_FAILED_LISTING_TTL_SECONDS = 300.0
_STEAM_BUY_FAILED_LISTING_BLACKLIST: dict[tuple[str, str], float] = {}


def _build_profit_trade_market_service(settings: Settings) -> MarketService:
    """ProfitTrade execution only trusts Steam orderbook and C5 batch prices."""
    store = AccountStore(PROJECT_ROOT / "config")
    usable_accounts: list[Account] = []
    for account in store.list_accounts():
        if account.username and account.password:
            ok, _, refreshed_account = try_steam_auto_relogin(store, account_id=account.id)
            if ok and refreshed_account is not None:
                account = refreshed_account
        if account.cookies:
            usable_accounts.append(account)

    steam_market_client: SteamMarketClient | None = None
    for account in usable_accounts:
        try:
            steam_market_client = SteamMarketClient(
                cookies=account.cookies,
                steam_id64=account.steam_id64,
                identity_secret=account.identity_secret,
                device_id=account.device_id,
                account_id=account.id,
                base_url=settings.steam_market_base_url,
            )
            break
        except Exception:
            continue
    if steam_market_client is None and settings.steam_cookies:
        try:
            steam_market_client = SteamMarketClient(
                cookies=settings.steam_cookies,
                identity_secret=settings.steam_identity_secret,
                device_id=settings.steam_device_id,
                base_url=settings.steam_market_base_url,
            )
        except Exception:
            pass

    return MarketService(
        steamdt_client=None,
        csqaq_client=None,
        c5_client=C5GameClient(settings.c5_api_key, settings.c5_base_url)
        if settings.c5_api_key
        else None,
        steam_market_client=steam_market_client,
        app_id=settings.app_id,
        include_c5_purchase_prices=False,
    )

@dataclass(slots=True)
class SteamBuyTarget:
    listing_id: str
    subtotal: int
    fee: int
    total: int

    @property
    def total_price(self) -> float:
        return self.total / 100.0


@dataclass(slots=True)
class SteamBuyAccountSelection:
    account: Account | None
    client: Any
    wallet_balance: float | None
    wallet: dict[str, Any]


@dataclass(slots=True)
class SteamBuyVerification:
    confirmed: bool
    wallet_after: dict[str, Any] | None
    wallet_delta: float | None
    active_buy_orders: list[dict[str, Any]]
    verified_by: list[str]
    reason: str | None = None
    inventory_after_asset_ids: list[str] | None = None
    new_inventory_asset_ids: list[str] | None = None


@dataclass(slots=True)
class ProfitTradeOpportunity:
    market_hash_name: str
    name: str
    asset_id: str
    steam_id: str | None
    token: str | None
    style_token: str | None
    steam_buy_price: float
    steam_price_source: str
    c5_listing_price: float
    c5_price_source: str
    c5_expected_net_price: float
    steam_real_cost: float
    expected_profit: float
    expected_roi: float
    inventory_count: int
    tradable_count: int
    c5_recent_sold_net_price: float | None = None
    c5_recent_sold_count: int | None = None
    c5_current_sell_price: float | None = None
    c5_on_sale_count: int | None = None
    c5_purchase_max_price: float | None = None
    c5_purchase_count: int | None = None
    liquidity_status: str = "unknown_no_c5_recent_sales_api"
    audit_status: str = "passed"
    audit_reason: str = "rule_based"

    @property
    def expected_roi_pct(self) -> float:
        return self.expected_roi * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "marketHashName": self.market_hash_name,
            "name": self.name,
            "assetId": self.asset_id,
            "steamId": self.steam_id,
            "steamBuyPrice": round(self.steam_buy_price, 2),
            "steamPriceSource": self.steam_price_source,
            "c5ListingPrice": round(self.c5_listing_price, 2),
            "c5PriceSource": self.c5_price_source,
            "c5ExpectedNetPrice": round(self.c5_expected_net_price, 2),
            "steamRealCost": round(self.steam_real_cost, 2),
            "expectedProfit": round(self.expected_profit, 2),
            "expectedRoi": round(self.expected_roi, 4),
            "expectedRoiPct": round(self.expected_roi_pct, 2),
            "inventoryCount": self.inventory_count,
            "tradableCount": self.tradable_count,
            "c5RecentSoldNetPrice": self.c5_recent_sold_net_price,
            "c5RecentSoldCount": self.c5_recent_sold_count,
            "c5CurrentSellPrice": self.c5_current_sell_price,
            "c5OnSaleCount": self.c5_on_sale_count,
            "c5PurchaseMaxPrice": self.c5_purchase_max_price,
            "c5PurchaseCount": self.c5_purchase_count,
            "liquidityStatus": self.liquidity_status,
            "auditStatus": self.audit_status,
            "auditReason": self.audit_reason,
        }


@dataclass(slots=True)
class ProfitTradeScanReport:
    generated_at: str
    inventory_source: str
    inventory_count: int
    evaluated_count: int
    opportunity_count: int
    missing_price_count: int
    skipped_count: int
    opportunities: list[ProfitTradeOpportunity]
    created_trade_ids: list[int]
    locked_trade_ids: list[int]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generatedAt": self.generated_at,
            "inventorySource": self.inventory_source,
            "inventoryCount": self.inventory_count,
            "evaluatedCount": self.evaluated_count,
            "opportunityCount": self.opportunity_count,
            "missingPriceCount": self.missing_price_count,
            "skippedCount": self.skipped_count,
            "createdTradeIds": list(self.created_trade_ids),
            "lockedTradeIds": list(self.locked_trade_ids),
            "notes": list(self.notes),
            "opportunities": [opportunity.to_dict() for opportunity in self.opportunities],
        }


@dataclass(slots=True)
class ProfitTradeRunReport:
    generated_at: str
    enabled: bool
    allow_real_execution: bool
    scanned: ProfitTradeScanReport | None
    bought_trade_ids: list[int]
    listed_trade_ids: list[int]
    settled_trade_ids: list[int]
    skipped_trade_ids: list[int]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generatedAt": self.generated_at,
            "enabled": self.enabled,
            "allowRealExecution": self.allow_real_execution,
            "scanned": self.scanned.to_dict() if self.scanned is not None else None,
            "boughtTradeIds": list(self.bought_trade_ids),
            "listedTradeIds": list(self.listed_trade_ids),
            "settledTradeIds": list(self.settled_trade_ids),
            "skippedTradeIds": list(self.skipped_trade_ids),
            "errors": list(self.errors),
        }


@dataclass(slots=True)
class C5RecentSaleRisk:
    recent_sold_net_price: float | None
    recent_sold_count: int | None
    status: str
    reason: str
    raw: dict[str, Any]
    current_sell_price: float | None = None
    on_sale_count: int | None = None
    purchase_max_price: float | None = None
    purchase_count: int | None = None


@dataclass(slots=True)
class ActiveC5SaleLookup:
    active_ids: set[str]
    covered_steam_ids: set[str] | None = None
    errors: list[str] | None = None

    def covers(self, steam_id: str | None) -> bool:
        if self.covered_steam_ids is None:
            return True
        normalized = str(steam_id or "").strip()
        return bool(normalized) and normalized in self.covered_steam_ids


@dataclass(slots=True)
class C5SellerOrderLookup:
    sold_orders_by_product_id: dict[str, dict[str, Any]]
    covered_steam_ids: set[str]
    errors: list[str]

    def covers(self, steam_id: str | None) -> bool:
        normalized = str(steam_id or "").strip()
        return bool(normalized) and normalized in self.covered_steam_ids


def _default_frontend_payload_path() -> Path:
    return PROJECT_ROOT / "frontend" / "public" / "profit_trade_dashboard.json"


def _profit_trade_last_run_path(settings: Settings) -> Path:
    return settings.db_path.parent / "profit_trade_last_run.json"


def _summarize_profit_trade_run(report: "ProfitTradeRunReport") -> dict[str, Any]:
    bought = len(report.bought_trade_ids)
    listed = len(report.listed_trade_ids)
    settled = len(report.settled_trade_ids)
    skipped = len(report.skipped_trade_ids)
    errors = len(report.errors)
    return {
        "generatedAt": report.generated_at,
        "summary": f"买入B {bought} 笔，C5上架 {listed} 笔，结算 {settled} 笔，跳过 {skipped} 笔，错误 {errors} 个",
        "boughtCount": bought,
        "listedCount": listed,
        "settledCount": settled,
        "skippedCount": skipped,
        "errorCount": errors,
        "boughtTradeIds": list(report.bought_trade_ids),
        "listedTradeIds": list(report.listed_trade_ids),
        "settledTradeIds": list(report.settled_trade_ids),
        "skippedTradeIds": list(report.skipped_trade_ids),
        "errors": list(report.errors),
    }


def _write_profit_trade_last_run(settings: Settings, report: "ProfitTradeRunReport") -> None:
    path = _profit_trade_last_run_path(settings)
    ensure_parent_dir(path)
    path.write_text(
        json.dumps(_summarize_profit_trade_run(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_profit_trade_last_run(settings: Settings) -> dict[str, Any] | None:
    path = _profit_trade_last_run_path(settings)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _record_profit_trade_run(settings: Settings, report: "ProfitTradeRunReport") -> "ProfitTradeRunReport":
    try:
        _write_profit_trade_last_run(settings, report)
    except OSError:
        pass
    return report


def _config_payload(config: StrategyConfig) -> dict[str, Any]:
    return {
        "enabled": config.profit_trade_enabled,
        "allowRealExecution": config.profit_trade_allow_real_execution,
        "allowRepriceExecution": config.profit_trade_allow_reprice_execution,
        "balanceDiscount": config.profit_trade_balance_discount,
        "balanceDiscountPct": round(config.profit_trade_balance_discount * 100, 2),
        "minRoi": config.profit_trade_min_roi,
        "minRoiPct": round(config.profit_trade_min_roi * 100, 2),
        "minItemValue": config.profit_trade_min_item_value,
        "maxBuyPerCycle": config.profit_trade_max_buy_per_cycle,
        "dailySteamBudget": config.profit_trade_daily_steam_budget,
        "scanMaxItems": config.profit_trade_scan_max_items,
        "reservationSeconds": config.profit_trade_reservation_seconds,
        "steamBuyPriceTolerancePct": config.profit_trade_steam_buy_price_tolerance_pct,
        "c5CurrentSaleNetFactor": config.profit_trade_c5_current_sale_net_factor,
        "recentSoldFeeAlreadyDeducted": config.profit_trade_recent_sold_fee_already_deducted,
        "liquidityMinRecentSales": config.profit_trade_liquidity_min_recent_sales,
        "requireC5RecentSales": config.profit_trade_require_c5_recent_sales,
        "requireC5MarketDepth": config.profit_trade_require_c5_market_depth,
        "c5MinOnSaleCount": config.profit_trade_c5_min_on_sale_count,
        "c5MinPurchaseCount": config.profit_trade_c5_min_purchase_count,
        "c5MinPurchaseSellRatio": config.profit_trade_c5_min_purchase_sell_ratio,
        "c5MaxListingPremiumPct": config.profit_trade_c5_max_listing_premium_pct,
        "manualReviewRoi": config.profit_trade_manual_review_roi,
        "manualReviewRoiPct": round(config.profit_trade_manual_review_roi * 100, 2),
        "repriceEnabled": config.profit_trade_reprice_enabled,
        "repriceDiscountPct": config.profit_trade_reprice_discount_pct,
        "repriceMinDiscount": config.profit_trade_reprice_min_discount,
        "repriceMaxDiscount": config.profit_trade_reprice_max_discount,
        "repriceCooldownHours": config.profit_trade_reprice_cooldown_hours,
        "staleRepriceAfterHours": config.profit_trade_stale_reprice_after_hours,
        "staleManualReviewAfterHours": config.profit_trade_stale_manual_review_after_hours,
        "staleMinRoi": config.profit_trade_stale_min_roi,
        "stalePriceOffset": config.profit_trade_stale_price_offset,
        "stickerSlabStatus": config.profit_trade_sticker_slab_status,
        "stickerStatus": config.profit_trade_sticker_status,
        "protectedAssetIds": list(config.profit_trade_protected_asset_ids or []),
        "protectedMarketHashNames": list(config.profit_trade_protected_market_hash_names or []),
        "protectedSteamIds": list(config.profit_trade_protected_steam_ids or []),
        "aiAudit": {
            "enabled": config.profit_trade_ai_audit_enabled,
            "provider": config.profit_trade_ai_audit_provider,
            "model": config.profit_trade_ai_audit_model,
        },
    }


def _read_note(raw_note: Any) -> dict[str, Any]:
    if not raw_note:
        return {}
    if isinstance(raw_note, dict):
        return raw_note
    try:
        value = json.loads(str(raw_note))
    except ValueError:
        return {"text": str(raw_note)}
    return value if isinstance(value, dict) else {"value": value}


def _build_note(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _build_steam_client(settings: Settings) -> SteamMarketClient:
    store = AccountStore(PROJECT_ROOT / "config")
    current = store.get_current()
    cookies = (current.cookies if current else None) or settings.steam_cookies
    identity_secret = (current.identity_secret if current else None) or settings.steam_identity_secret
    device_id = (current.device_id if current else None) or settings.steam_device_id
    steam_id64 = (current.steam_id64 if current else None) or None
    if not cookies:
        raise RuntimeError("missing Steam cookies")
    return SteamMarketClient(
        cookies=cookies,
        steam_id64=steam_id64,
        identity_secret=identity_secret,
        device_id=device_id,
        account_id=current.id if current else None,
        base_url=settings.steam_market_base_url,
    )


def _build_steam_client_for_account(settings: Settings, account: Account) -> SteamMarketClient:
    return SteamMarketClient(
        cookies=account.cookies,
        steam_id64=account.steam_id64,
        identity_secret=account.identity_secret,
        device_id=account.device_id,
        account_id=account.id,
        base_url=settings.steam_market_base_url,
    )


def _select_steam_buy_account(
    settings: Settings,
    *,
    required_balance: float,
    preferred_steam_id: str | None = None,
    account_store: AccountStore | None = None,
) -> SteamBuyAccountSelection:
    store = account_store or AccountStore(PROJECT_ROOT / "config")
    candidates: list[tuple[float, str, Account, SteamMarketClient, dict[str, Any]]] = []
    errors: list[str] = []
    preferred = str(preferred_steam_id or "").strip()
    for account in store.list_accounts():
        if not account.cookies or not account.steam_id64:
            continue
        try:
            client = _build_steam_client_for_account(settings, account)
            wallet = client.wallet_balance()
            balance = safe_float(wallet.get("balance")) or 0.0
        except Exception as exc:
            errors.append(f"{account.name}: {exc}")
            continue
        if preferred and str(account.steam_id64 or "").strip() == preferred and balance + 1e-9 >= required_balance:
            return SteamBuyAccountSelection(account=account, client=client, wallet_balance=balance, wallet=wallet)
        if balance + 1e-9 >= required_balance:
            candidates.append((balance, account.name, account, client, wallet))

    if candidates:
        balance, _, account, client, wallet = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
        return SteamBuyAccountSelection(account=account, client=client, wallet_balance=balance, wallet=wallet)

    if settings.steam_cookies:
        client = _build_steam_client(settings)
        wallet = client.wallet_balance()
        balance = safe_float(wallet.get("balance")) or 0.0
        if balance + 1e-9 >= required_balance:
            return SteamBuyAccountSelection(account=None, client=client, wallet_balance=balance, wallet=wallet)

    detail = "; ".join(errors[:5])
    suffix = f" ({detail})" if detail else ""
    raise RuntimeError(f"no Steam account has enough wallet balance for CNY {required_balance:.2f}{suffix}")


def _require_profit_trade_real_execution(config: StrategyConfig) -> None:
    if not config.profit_trade_enabled:
        raise RuntimeError("profitTrade.enabled is false")
    if not config.profit_trade_allow_real_execution:
        raise RuntimeError("profitTrade.allowRealExecution is false")


def _require_profit_trade_reprice_execution(config: StrategyConfig) -> None:
    if not config.profit_trade_enabled:
        raise RuntimeError("profitTrade.enabled is false")
    if not (config.profit_trade_allow_real_execution or config.profit_trade_allow_reprice_execution):
        raise RuntimeError("profitTrade.allowRepriceExecution is false")


def _profit_trade_steam_cost_ratio(config: StrategyConfig) -> float:
    return float(config.profit_trade_balance_discount)


def _profit_trade_transfer_roi(
    *,
    c5_expected_net: float,
    steam_buy_price: float,
    steam_cost_ratio: float,
) -> float | None:
    if steam_buy_price <= 0:
        return None
    return (c5_expected_net / steam_buy_price) - steam_cost_ratio


def _first_float(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = safe_float(payload.get(key))
        if value is not None and value > 0:
            return value
    return None


def _first_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = safe_int(payload.get(key))
        if value is not None and value >= 0:
            return value
    return None


def _normalize_c5_statistics_payload(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        return {}
    rows: list[Any]
    if all(isinstance(value, dict) for value in data.values()):
        rows = list(data.values())
    else:
        raw_rows = data.get("list") or data.get("items") or data.get("records") or []
        rows = raw_rows if isinstance(raw_rows, list) else []

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        market_hash_name = str(
            row.get("marketHashName")
            or row.get("market_hash_name")
            or row.get("hashName")
            or row.get("name")
            or ""
        ).strip()
        if market_hash_name:
            result[market_hash_name] = row
    for market_hash_name, row in data.items():
        if isinstance(row, dict) and str(market_hash_name).strip() and market_hash_name not in result:
            result[str(market_hash_name)] = row
    return result


def _fetch_c5_recent_sale_risks(
    c5_client: Any,
    *,
    app_id: int,
    market_hash_names: list[str],
) -> dict[str, C5RecentSaleRisk]:
    if not market_hash_names:
        return {}
    fetcher = getattr(c5_client, "price_statistics_batch", None)
    if fetcher is None:
        return {}
    try:
        raw_data = fetcher(market_hash_names, app_id=app_id)
    except Exception:
        return {}

    rows = _normalize_c5_statistics_payload(raw_data)
    risks: dict[str, C5RecentSaleRisk] = {}
    for market_hash_name in market_hash_names:
        row = rows.get(market_hash_name) or {}
        recent_price = _first_float(
            row,
            (
                "recentSoldNetPrice",
                "recent_sold_net_price",
                "lastSoldNetPrice",
                "last_sold_net_price",
                "soldNetPrice",
                "dealNetPrice",
                "recentSoldPrice",
                "lastSoldPrice",
                "soldPrice",
                "dealPrice",
                "avgPrice",
                "averagePrice",
            ),
        )
        recent_count = _first_int(
            row,
            (
                "recentSoldCount",
                "recent_sold_count",
                "soldCount",
                "dealCount",
                "tradeCount",
                "volume",
            ),
        )
        current_sell_price = _first_float(
            row,
            (
                "currentSellPrice",
                "current_sell_price",
                "sellPrice",
                "price",
                "lowestSellPrice",
                "minSellPrice",
            ),
        )
        on_sale_count = _first_int(
            row,
            (
                "onSaleCount",
                "on_sale_count",
                "sellCount",
                "listingCount",
                "saleCount",
                "count",
            ),
        )
        purchase_max_price = _first_float(
            row,
            (
                "purchaseMaxPrice",
                "purchase_max_price",
                "buyMaxPrice",
                "maxPurchasePrice",
                "bidPrice",
                "highestBidPrice",
            ),
        )
        purchase_count = _first_int(
            row,
            (
                "purchaseCount",
                "purchase_count",
                "buyCount",
                "bidCount",
            ),
        )
        risks[market_hash_name] = C5RecentSaleRisk(
            recent_sold_net_price=recent_price,
            recent_sold_count=recent_count,
            status="raw",
            reason="statistics fetched",
            raw=dict(row),
            current_sell_price=current_sell_price,
            on_sale_count=on_sale_count,
            purchase_max_price=purchase_max_price,
            purchase_count=purchase_count,
        )
    return risks


def _evaluate_c5_recent_sale_risk(
    config: StrategyConfig,
    *,
    c5_listing_price: float,
    risk: C5RecentSaleRisk | None,
) -> C5RecentSaleRisk:
    if risk is None or risk.recent_sold_net_price is None:
        return C5RecentSaleRisk(
            recent_sold_net_price=None,
            recent_sold_count=None,
            status="blocked_no_c5_recent_sales",
            reason="missing C5 recent sale statistics",
            raw={},
        )

    recent_net = float(risk.recent_sold_net_price)
    if not config.profit_trade_recent_sold_fee_already_deducted:
        recent_net *= float(config.profit_trade_c5_current_sale_net_factor)
    count = int(risk.recent_sold_count or 0)
    min_count = int(config.profit_trade_liquidity_min_recent_sales)
    if count < min_count:
        return C5RecentSaleRisk(
            recent_sold_net_price=recent_net,
            recent_sold_count=count,
            status="blocked_low_c5_liquidity",
            reason=f"C5 recent sold count {count} < {min_count}",
            raw=risk.raw,
        )

    listing_net = c5_listing_price * float(config.profit_trade_c5_current_sale_net_factor)
    max_ratio = 1.0 + max(0.0, float(config.profit_trade_c5_max_listing_premium_pct)) / 100.0
    if listing_net > recent_net * max_ratio:
        return C5RecentSaleRisk(
            recent_sold_net_price=recent_net,
            recent_sold_count=count,
            status="blocked_c5_listing_above_recent_sale",
            reason=f"C5 listing net {listing_net:.2f} > recent sold net {recent_net:.2f} * {max_ratio:.4f}",
            raw=risk.raw,
        )

    return C5RecentSaleRisk(
        recent_sold_net_price=recent_net,
        recent_sold_count=count,
        status="passed",
        reason="C5 recent sale risk passed",
        raw=risk.raw,
    )


def _evaluate_c5_market_depth_risk(
    config: StrategyConfig,
    *,
    c5_listing_price: float,
    risk: C5RecentSaleRisk | None,
) -> C5RecentSaleRisk:
    if risk is None:
        return C5RecentSaleRisk(
            recent_sold_net_price=None,
            recent_sold_count=None,
            status="blocked_no_c5_market_stats",
            reason="missing C5 market statistics",
            raw={},
        )

    current_price = safe_float(risk.current_sell_price)
    if current_price is None or current_price <= 0:
        return C5RecentSaleRisk(
            recent_sold_net_price=risk.recent_sold_net_price,
            recent_sold_count=risk.recent_sold_count,
            status="blocked_no_c5_current_sell_price",
            reason="missing C5 current sell price in statistics",
            raw=risk.raw,
            current_sell_price=risk.current_sell_price,
            on_sale_count=risk.on_sale_count,
        )

    on_sale_count = int(risk.on_sale_count or 0)
    min_count = max(0, int(config.profit_trade_c5_min_on_sale_count))
    if on_sale_count < min_count:
        return C5RecentSaleRisk(
            recent_sold_net_price=risk.recent_sold_net_price,
            recent_sold_count=risk.recent_sold_count,
            status="blocked_low_c5_listing_depth",
            reason=f"C5 on-sale count {on_sale_count} < {min_count}",
            raw=risk.raw,
            current_sell_price=current_price,
            on_sale_count=on_sale_count,
            purchase_max_price=risk.purchase_max_price,
            purchase_count=risk.purchase_count,
        )
    purchase_count = int(risk.purchase_count or 0)
    min_purchase_count = max(0, int(config.profit_trade_c5_min_purchase_count))
    if purchase_count < min_purchase_count:
        return C5RecentSaleRisk(
            recent_sold_net_price=risk.recent_sold_net_price,
            recent_sold_count=risk.recent_sold_count,
            status="blocked_low_c5_purchase_depth",
            reason=f"C5 purchase count {purchase_count} < {min_purchase_count}",
            raw=risk.raw,
            current_sell_price=current_price,
            on_sale_count=on_sale_count,
            purchase_max_price=risk.purchase_max_price,
            purchase_count=purchase_count,
        )
    purchase_max = safe_float(risk.purchase_max_price)
    min_purchase_ratio = max(0.0, float(config.profit_trade_c5_min_purchase_sell_ratio))
    if purchase_max is None or purchase_max <= 0 or purchase_max / current_price < min_purchase_ratio:
        ratio = (purchase_max / current_price) if purchase_max is not None and current_price > 0 else 0.0
        return C5RecentSaleRisk(
            recent_sold_net_price=risk.recent_sold_net_price,
            recent_sold_count=risk.recent_sold_count,
            status="blocked_c5_purchase_price_gap",
            reason=f"C5 purchase/sell ratio {ratio:.4f} < {min_purchase_ratio:.4f}",
            raw=risk.raw,
            current_sell_price=current_price,
            on_sale_count=on_sale_count,
            purchase_max_price=purchase_max,
            purchase_count=purchase_count,
        )

    max_ratio = 1.0 + max(0.0, float(config.profit_trade_c5_max_listing_premium_pct)) / 100.0
    if c5_listing_price > current_price * max_ratio:
        return C5RecentSaleRisk(
            recent_sold_net_price=risk.recent_sold_net_price,
            recent_sold_count=risk.recent_sold_count,
            status="blocked_c5_listing_above_current_market",
            reason=f"C5 listing {c5_listing_price:.2f} > current sell {current_price:.2f} * {max_ratio:.4f}",
            raw=risk.raw,
            current_sell_price=current_price,
            on_sale_count=on_sale_count,
        )

    return C5RecentSaleRisk(
        recent_sold_net_price=risk.recent_sold_net_price,
        recent_sold_count=risk.recent_sold_count,
        status="passed",
        reason="C5 current market depth risk passed",
        raw=risk.raw,
        current_sell_price=current_price,
        on_sale_count=on_sale_count,
        purchase_max_price=purchase_max,
        purchase_count=purchase_count,
    )


def _failed_steam_buy_listing_key(market_hash_name: str, listing_id: str) -> tuple[str, str]:
    return (str(market_hash_name or "").strip(), str(listing_id or "").strip())


def _remember_failed_steam_buy_listing(
    market_hash_name: str,
    listing_id: str,
    *,
    now: float | None = None,
) -> None:
    key = _failed_steam_buy_listing_key(market_hash_name, listing_id)
    if not key[0] or not key[1]:
        return
    current = time.time() if now is None else float(now)
    _STEAM_BUY_FAILED_LISTING_BLACKLIST[key] = current + max(
        0.0,
        float(STEAM_BUY_FAILED_LISTING_TTL_SECONDS),
    )


def _active_failed_steam_buy_listing_ids(
    market_hash_name: str,
    *,
    now: float | None = None,
) -> set[str]:
    name = str(market_hash_name or "").strip()
    current = time.time() if now is None else float(now)
    expired = [key for key, expires_at in _STEAM_BUY_FAILED_LISTING_BLACKLIST.items() if expires_at <= current]
    for key in expired:
        _STEAM_BUY_FAILED_LISTING_BLACKLIST.pop(key, None)
    if not name:
        return set()
    return {
        listing_id
        for (stored_name, listing_id), expires_at in _STEAM_BUY_FAILED_LISTING_BLACKLIST.items()
        if stored_name == name and listing_id and expires_at > current
    }


def _steam_listing_already_purchased_error(exc: BaseException) -> bool:
    fragments: list[str] = [str(exc)]
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if value not in (None, ""):
                fragments.append(str(value))
    text = " ".join(fragments).lower()
    return (
        "已经购买" in text
        or "已被购买" in text
        or "already purchased" in text
        or "someone else has already purchased" in text
        or "because another user has purchased" in text
    )

def _pick_lowest_steam_orderbook_buy_target(payload: dict[str, Any]) -> SteamBuyTarget | None:
    summary = summarize_orderbook_prices(payload)
    if summary.seller_floor_price is None or summary.seller_floor_price <= 0:
        return None
    total = int(round(summary.seller_floor_price * 100))
    if total <= 0:
        return None
    return SteamBuyTarget(
        listing_id="orderbook_floor",
        subtotal=total,
        fee=0,
        total=total,
    )


def _steam_market_should_use_buy_order(market_hash_name: str) -> bool:
    name = str(market_hash_name or "").strip()
    if not name:
        return False
    lower = name.lower()
    if lower.startswith(("sticker |", "patch |", "sealed graffiti |", "graffiti |", "music kit |")):
        return True
    return any(
        token in lower
        for token in (
            " case",
            " capsule",
            " souvenir package",
            " sticker capsule",
            " patch pack",
            " graffiti box",
        )
    )


def _pick_lowest_steam_listing_buy_target(
    payload: dict[str, Any],
    *,
    market_hash_name: str,
    currency: int | None = None,
    excluded_listing_ids: set[str] | None = None,
) -> SteamBuyTarget | None:
    raw_listinginfo = payload.get("listinginfo") or payload.get("listings") or {}
    rows: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw_listinginfo, dict):
        for raw_listing_id, raw_entry in raw_listinginfo.items():
            if isinstance(raw_entry, dict):
                rows.append((str(raw_listing_id), raw_entry))
    elif isinstance(raw_listinginfo, list):
        for raw_entry in raw_listinginfo:
            if isinstance(raw_entry, dict):
                rows.append((str(raw_entry.get("listingid") or ""), raw_entry))

    candidates: list[SteamBuyTarget] = []
    for raw_listing_id, raw_entry in rows:
        listing_id = str(raw_entry.get("listingid") or raw_listing_id or "").strip()
        if not listing_id:
            continue
        if excluded_listing_ids and listing_id in excluded_listing_ids:
            continue
        description = raw_entry.get("description")
        row_hash_name = ""
        if isinstance(description, dict):
            row_hash_name = str(description.get("market_hash_name") or "").strip()
        row_hash_name = row_hash_name or str(
            raw_entry.get("market_hash_name") or raw_entry.get("marketHashName") or ""
        ).strip()
        if row_hash_name and row_hash_name != market_hash_name:
            continue
        row_currency = safe_int(
            raw_entry.get("converted_currencyid")
            or raw_entry.get("currencyid")
            or raw_entry.get("eCurrency")
            or raw_entry.get("currency")
        )
        if currency is not None and row_currency is not None and int(row_currency) != int(currency):
            continue
        subtotal = safe_int(
            raw_entry.get("converted_price")
            or raw_entry.get("unPricePerUnit")
            or raw_entry.get("unPrice")
            or raw_entry.get("subtotal")
            or raw_entry.get("price")
        )
        fee = safe_int(
            raw_entry.get("converted_fee")
            or raw_entry.get("unFeePerUnit")
            or raw_entry.get("unFee")
            or raw_entry.get("fee")
        )
        total = safe_int(
            raw_entry.get("converted_total")
            or raw_entry.get("unTotal")
            or raw_entry.get("unTotalPerUnit")
            or raw_entry.get("total")
        )
        if total is None and subtotal is not None and fee is not None:
            total = subtotal + fee
        if subtotal is None and total is not None and fee is not None:
            subtotal = total - fee
        if fee is None and total is not None and subtotal is not None:
            fee = total - subtotal
        if subtotal is None or fee is None or total is None or total <= 0:
            continue
        candidates.append(
            SteamBuyTarget(
                listing_id=listing_id,
                subtotal=int(subtotal),
                fee=int(fee),
                total=int(total),
            )
        )
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.total, item.subtotal, item.listing_id))[0]


def _compact_steam_orderbook_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    sell_rows = data.get("rgCompactSellOrders") if isinstance(data, dict) else None
    buy_rows = data.get("rgCompactBuyOrders") if isinstance(data, dict) else None
    return {
        "eCurrency": data.get("eCurrency") if isinstance(data, dict) else None,
        "amtMinSellOrder": data.get("amtMinSellOrder") if isinstance(data, dict) else None,
        "cSellOrders": data.get("cSellOrders") if isinstance(data, dict) else None,
        "rgCompactSellOrders": sell_rows[:10] if isinstance(sell_rows, list) else sell_rows,
        "rgCompactBuyOrders": buy_rows[:10] if isinstance(buy_rows, list) else buy_rows,
    }


def _compact_steam_listing_snapshot(
    payload: dict[str, Any],
    target: SteamBuyTarget,
) -> dict[str, Any]:
    listinginfo = payload.get("listinginfo")
    row = None
    if isinstance(listinginfo, dict):
        maybe = listinginfo.get(target.listing_id)
        if isinstance(maybe, dict):
            row = maybe
    if row is None and isinstance(payload.get("listings"), list):
        for value in payload.get("listings") or []:
            if isinstance(value, dict) and str(value.get("listingid") or "") == target.listing_id:
                row = value
                break
    if not isinstance(row, dict):
        return {
            "listingid": target.listing_id,
            "subtotal": target.subtotal,
            "fee": target.fee,
            "total": target.total,
        }
    description = row.get("description") if isinstance(row.get("description"), dict) else {}
    asset = row.get("asset")
    if isinstance(asset, dict):
        asset = {
            key: asset.get(key)
            for key in ("id", "assetid", "classid", "instanceid", "amount", "appid", "contextid")
            if key in asset
        }
    return {
        "listingid": target.listing_id,
        "subtotal": target.subtotal,
        "fee": target.fee,
        "total": target.total,
        "currency": row.get("converted_currencyid") or row.get("eCurrency") or row.get("currencyid"),
        "marketHashName": description.get("market_hash_name") if isinstance(description, dict) else None,
        "strSubtotal": row.get("strSubtotal"),
        "asset": asset,
    }


def _compact_steam_buy_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "buyOrderId": (
            row.get("buy_orderid")
            or row.get("buy_order_id")
            or row.get("buyOrderId")
            or row.get("orderid")
        ),
        "marketHashName": (
            row.get("market_hash_name")
            or row.get("marketHashName")
            or row.get("hash_name")
            or row.get("name")
        ),
        "price": row.get("price") or row.get("price_total") or row.get("priceTotal"),
        "quantity": row.get("quantity"),
        "quantityRemaining": (
            row.get("quantity_remaining")
            or row.get("quantityRemaining")
            or row.get("remaining_quantity")
        ),
    }


def _matching_active_steam_buy_orders(
    payload: dict[str, Any],
    *,
    market_hash_name: str,
    buy_order_id: str | None = None,
) -> list[dict[str, Any]]:
    raw_orders = payload.get("buy_orders") or payload.get("buyOrders") or []
    if isinstance(raw_orders, dict):
        rows = [row for row in raw_orders.values() if isinstance(row, dict)]
    elif isinstance(raw_orders, list):
        rows = [row for row in raw_orders if isinstance(row, dict)]
    else:
        rows = []

    target_order_id = str(buy_order_id or "").strip()
    target_name = str(market_hash_name or "").strip()
    result: list[dict[str, Any]] = []
    for row in rows:
        row_order_id = str(
            row.get("buy_orderid")
            or row.get("buy_order_id")
            or row.get("buyOrderId")
            or row.get("orderid")
            or ""
        ).strip()
        row_name = str(
            row.get("market_hash_name")
            or row.get("marketHashName")
            or row.get("hash_name")
            or row.get("name")
            or ""
        ).strip()
        if target_order_id:
            if row_order_id != target_order_id:
                continue
        elif target_name:
            if row_name != target_name:
                continue
        else:
            continue
        remaining = safe_int(
            row.get("quantity_remaining")
            or row.get("quantityRemaining")
            or row.get("remaining_quantity")
            or row.get("quantity")
        )
        if remaining is not None and remaining <= 0:
            continue
        result.append(_compact_steam_buy_order(row))
    return result


def _verify_steam_buy_completed(
    client: Any,
    *,
    market_hash_name: str,
    method: str,
    expected_total: int,
    wallet_before_balance: float | None,
    buy_order_id: str | None = None,
    attempts: int = 3,
    delay_seconds: float = 0.4,
) -> SteamBuyVerification:
    expected_delta = max(0.0, float(expected_total) / 100.0)
    wallet_after: dict[str, Any] | None = None
    wallet_delta: float | None = None
    active_buy_orders: list[dict[str, Any]] = []
    verified_by: list[str] = []
    reasons: list[str] = []

    for attempt in range(max(1, int(attempts))):
        reasons = []
        verified_by = []
        active_buy_orders = []

        try:
            wallet_after = client.wallet_balance()
            after_balance = safe_float(wallet_after.get("balance")) if isinstance(wallet_after, dict) else None
            if wallet_before_balance is not None and after_balance is not None:
                wallet_delta = round(float(wallet_before_balance) - float(after_balance), 2)
                if wallet_delta + 0.02 >= expected_delta:
                    verified_by.append("wallet_balance_delta")
                else:
                    reasons.append(
                        f"wallet delta {wallet_delta:.2f} is below expected {expected_delta:.2f}"
                    )
            else:
                reasons.append("wallet balance before/after is unavailable")
        except Exception as exc:
            reasons.append(f"wallet verification failed: {exc}")

        must_check_buy_order = method == "createbuyorder"
        if hasattr(client, "my_listings"):
            try:
                listings_payload = client.my_listings(start=0, count=100)
                active_buy_orders = _matching_active_steam_buy_orders(
                    listings_payload if isinstance(listings_payload, dict) else {},
                    market_hash_name=market_hash_name,
                    buy_order_id=buy_order_id,
                )
                if active_buy_orders:
                    reasons.append("matching Steam buy order is still active")
                else:
                    verified_by.append("no_active_matching_buy_order")
            except Exception as exc:
                if must_check_buy_order:
                    reasons.append(f"active buy order verification failed: {exc}")
                else:
                    reasons.append(f"active buy order verification skipped after error: {exc}")
        elif must_check_buy_order:
            reasons.append("client cannot verify active buy orders")

        confirmed = "wallet_balance_delta" in verified_by
        if must_check_buy_order:
            confirmed = confirmed and "no_active_matching_buy_order" in verified_by
        if confirmed:
            return SteamBuyVerification(
                confirmed=True,
                wallet_after=wallet_after,
                wallet_delta=wallet_delta,
                active_buy_orders=active_buy_orders,
                verified_by=verified_by,
            )
        if attempt + 1 < max(1, int(attempts)):
            time.sleep(max(0.0, float(delay_seconds)))

    return SteamBuyVerification(
        confirmed=False,
        wallet_after=wallet_after,
        wallet_delta=wallet_delta,
        active_buy_orders=active_buy_orders,
        verified_by=verified_by,
        reason="; ".join(reasons) if reasons else "Steam buy completion could not be verified",
    )


def _inventory_asset_ids_for_market_hash(
    inventory_payload: dict[str, Any],
    *,
    market_hash_name: str,
    steam_id: str | None = None,
) -> list[str]:
    target_name = str(market_hash_name or "").strip()
    target_steam_id = str(steam_id or "").strip()
    items = inventory_payload.get("list") or []
    if not isinstance(items, list) or not target_name:
        return []
    result: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("marketHashName") or item.get("market_hash_name") or "").strip()
        if item_name != target_name:
            continue
        item_steam_id = str(item.get("steamId") or item.get("steam_id") or "").strip()
        if target_steam_id and item_steam_id and item_steam_id != target_steam_id:
            continue
        asset_id = str(item.get("assetId") or item.get("asset_id") or "").strip()
        if asset_id:
            result.append(asset_id)
    return sorted(set(result))


def _fetch_c5_inventory_asset_ids_for_steam_buy(
    settings: Settings,
    *,
    market_hash_name: str,
    steam_id: str | None,
    c5_client: Any | None = None,
) -> tuple[list[str], str | None]:
    target_steam_id = str(steam_id or "").strip()
    if not target_steam_id:
        return [], "missing Steam id for inventory verification"
    if c5_client is None:
        if not settings.c5_api_key:
            return [], "missing C5GAME_API_KEY / C5_API_KEY for inventory verification"
        c5_client = C5GameClient(str(settings.c5_api_key), settings.c5_base_url)
    try:
        payload = c5_client.inventory(target_steam_id, app_id=settings.app_id)
    except Exception as exc:
        return [], f"C5 inventory verification failed: {exc}"
    if not isinstance(payload, dict):
        return [], "C5 inventory verification returned invalid payload"
    items = payload.get("list") or []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                item.setdefault("steamId", target_steam_id)
    return _inventory_asset_ids_for_market_hash(
        payload,
        market_hash_name=market_hash_name,
        steam_id=target_steam_id,
    ), None


def _verify_steam_buy_completed_with_inventory(
    client: Any,
    settings: Settings,
    *,
    market_hash_name: str,
    method: str,
    expected_total: int,
    wallet_before_balance: float | None,
    buy_order_id: str | None,
    before_asset_ids: list[str],
    steam_id: str | None,
    c5_client: Any | None = None,
    attempts: int | None = None,
    delay_seconds: float | None = None,
) -> SteamBuyVerification:
    max_attempts = max(1, int(attempts or STEAM_BUY_VERIFY_ATTEMPTS))
    wait_seconds = max(0.0, float(
        STEAM_BUY_VERIFY_DELAY_SECONDS if delay_seconds is None else delay_seconds
    ))
    latest = SteamBuyVerification(
        confirmed=False,
        wallet_after=None,
        wallet_delta=None,
        active_buy_orders=[],
        verified_by=[],
        reason="Steam buy completion could not be verified",
    )
    before_asset_id_set = {str(value) for value in before_asset_ids if str(value or "").strip()}

    for attempt in range(max_attempts):
        latest = _verify_steam_buy_completed(
            client,
            market_hash_name=market_hash_name,
            method=method,
            expected_total=expected_total,
            wallet_before_balance=wallet_before_balance,
            buy_order_id=buy_order_id,
            attempts=1,
            delay_seconds=0.0,
        )
        if latest.confirmed:
            return latest

        if method == "createbuyorder" and "wallet_balance_delta" in latest.verified_by:
            inventory_after_asset_ids, inventory_error = _fetch_c5_inventory_asset_ids_for_steam_buy(
                settings,
                market_hash_name=market_hash_name,
                steam_id=steam_id,
                c5_client=c5_client,
            )
            new_inventory_asset_ids = [
                asset_id for asset_id in inventory_after_asset_ids if asset_id not in before_asset_id_set
            ]
            if new_inventory_asset_ids:
                return SteamBuyVerification(
                    confirmed=True,
                    wallet_after=latest.wallet_after,
                    wallet_delta=latest.wallet_delta,
                    active_buy_orders=latest.active_buy_orders,
                    verified_by=[*latest.verified_by, "c5_inventory_new_asset"],
                    inventory_after_asset_ids=inventory_after_asset_ids,
                    new_inventory_asset_ids=new_inventory_asset_ids,
                )
            latest.inventory_after_asset_ids = inventory_after_asset_ids
            latest.new_inventory_asset_ids = new_inventory_asset_ids
            if inventory_error:
                latest.reason = "; ".join(
                    reason for reason in (latest.reason, inventory_error) if reason
                )

        if attempt + 1 < max_attempts:
            time.sleep(wait_seconds)

    return latest
def _extract_c5_sale_id(payload: dict[str, Any]) -> str | None:
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


def _load_active_c5_sale_ids_page(
    c5_client: Any,
    settings: Settings,
    *,
    steam_id: str | None = None,
) -> set[str]:
    active_ids: set[str] = set()
    page = 1
    limit = 100
    while True:
        kwargs: dict[str, Any] = {
            "app_id": settings.app_id,
            "page": page,
            "limit": limit,
        }
        if steam_id:
            kwargs["steam_id"] = steam_id
        payload = c5_client.sale_search(**kwargs)
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


def _load_active_c5_sale_lookup(
    c5_client: Any,
    settings: Settings,
    *,
    steam_ids: list[str] | None = None,
) -> ActiveC5SaleLookup:
    try:
        return ActiveC5SaleLookup(active_ids=_load_active_c5_sale_ids_page(c5_client, settings))
    except Exception as global_exc:
        requested = sorted({str(value).strip() for value in steam_ids or [] if str(value).strip()})
        if not requested:
            raise
        active_ids: set[str] = set()
        covered: set[str] = set()
        errors: list[str] = []
        for steam_id in requested:
            try:
                active_ids.update(_load_active_c5_sale_ids_page(c5_client, settings, steam_id=steam_id))
                covered.add(steam_id)
            except Exception as exc:
                errors.append(f"C5 active sale lookup failed for steamId {steam_id}: {exc}")
        if not covered:
            raise global_exc
        return ActiveC5SaleLookup(active_ids=active_ids, covered_steam_ids=covered, errors=errors)


def _load_active_c5_sale_ids(c5_client: Any, settings: Settings) -> set[str]:
    return _load_active_c5_sale_lookup(c5_client, settings).active_ids


def _extract_c5_order_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    rows = payload.get("list") or payload.get("items") or payload.get("records") or payload.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("items") or rows.get("records") or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _c5_seller_order_id(row: dict[str, Any]) -> str | None:
    value = row.get("orderId") or row.get("order_id") or row.get("id")
    return str(value).strip() if value not in (None, "") else None


def _c5_seller_order_product_id(row: dict[str, Any]) -> str | None:
    value = row.get("productId") or row.get("product_id") or row.get("saleProductId")
    return str(value).strip() if value not in (None, "") else None


def _c5_seller_order_status(row: dict[str, Any]) -> int | None:
    return safe_int(row.get("status") or row.get("orderStatus") or row.get("order_status"))


def _is_c5_seller_order_sold(row: dict[str, Any]) -> bool:
    status = _c5_seller_order_status(row)
    if status in {10, 200}:
        return True
    status_name = str(row.get("statusName") or row.get("status_name") or "").strip().lower()
    return any(value in status_name for value in ("出售成功", "结算成功", "success"))


def _c5_seller_order_sold_net_price(
    row: dict[str, Any],
    *,
    config: StrategyConfig,
) -> tuple[float | None, str | None]:
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    for payload, source, keys in (
        (detail, "seller_order_detail_get_money", ("getMoney", "get_money", "sellerGetMoney")),
        (row, "seller_order_list_price", ("price", "getMoney", "get_money", "sellerPrice")),
        (detail, "seller_order_detail_price", ("price",)),
    ):
        price = _first_float(payload, keys)
        if price is not None and price > 0:
            return price, source
    gross = _first_float(detail, ("actualPay", "actual_pay", "salePrice", "sellPrice"))
    if gross is not None and gross > 0:
        return gross * float(config.profit_trade_c5_current_sale_net_factor), "estimated_from_seller_order_gross"
    return None, None


def _load_c5_seller_sold_order_lookup(
    c5_client: Any,
    settings: Settings,
    rows: list[Any],
) -> C5SellerOrderLookup:
    fetcher = getattr(c5_client, "seller_order_list", None)
    if fetcher is None:
        return C5SellerOrderLookup(sold_orders_by_product_id={}, covered_steam_ids=set(), errors=["C5 seller order API unavailable"])

    product_ids_by_steam: dict[str, set[str]] = {}
    for row in rows:
        note = _read_note(row["note"])
        product_id = str(row["c5_product_id"] or note.get("c5ProductId") or "").strip()
        steam_id = str(row["a_steam_id"] or note.get("steamId") or "").strip()
        if product_id and steam_id:
            product_ids_by_steam.setdefault(steam_id, set()).add(product_id)

    sold_orders: dict[str, dict[str, Any]] = {}
    covered_steam_ids: set[str] = set()
    errors: list[str] = []
    detail_fetcher = getattr(c5_client, "seller_order_detail", None)
    for steam_id, wanted_product_ids in product_ids_by_steam.items():
        found_for_steam: set[str] = set()
        for status in (10, 200):
            page = 1
            limit = 100
            while True:
                try:
                    payload = fetcher(
                        app_id=settings.app_id,
                        steam_id=steam_id,
                        status=status,
                        page=page,
                        limit=limit,
                    )
                    covered_steam_ids.add(steam_id)
                except Exception as exc:
                    errors.append(f"C5 seller order lookup failed for steamId {steam_id}, status {status}: {exc}")
                    break

                order_rows = _extract_c5_order_rows(payload)
                if not order_rows:
                    break
                for order_row in order_rows:
                    if not _is_c5_seller_order_sold(order_row):
                        continue
                    product_id = _c5_seller_order_product_id(order_row)
                    if not product_id or product_id not in wanted_product_ids or product_id in sold_orders:
                        continue
                    merged = dict(order_row)
                    order_id = _c5_seller_order_id(order_row)
                    if order_id and detail_fetcher is not None:
                        try:
                            detail = detail_fetcher(order_id)
                        except Exception as exc:
                            merged["detailError"] = str(exc)
                        else:
                            merged["detail"] = detail
                    sold_orders[product_id] = merged
                    found_for_steam.add(product_id)

                total = safe_int(payload.get("total")) if isinstance(payload, dict) else None
                if wanted_product_ids.issubset(found_for_steam):
                    break
                if len(order_rows) < limit:
                    break
                if total is not None and page * limit >= total:
                    break
                page += 1
        if wanted_product_ids.issubset(found_for_steam):
            continue

    return C5SellerOrderLookup(
        sold_orders_by_product_id=sold_orders,
        covered_steam_ids=covered_steam_ids,
        errors=errors,
    )


def _beijing_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    tz = timezone(timedelta(hours=8))
    current = now or datetime.now(timezone.utc)
    start = current.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _profit_trade_daily_steam_spent(db: Database, *, now: datetime | None = None) -> float:
    start, end = _beijing_day_bounds(now)
    total = 0.0
    for row in db.list_profit_trades(limit=5000):
        note = _read_note(row["note"])
        bought_at = _parse_iso(note.get("steamBuySucceededAt"))
        if bought_at is None:
            continue
        if bought_at.tzinfo is None:
            bought_at = bought_at.replace(tzinfo=timezone.utc)
        if start <= bought_at.astimezone(timezone.utc) < end:
            total += safe_float(row["steam_buy_price"]) or safe_float(note.get("steamBuyPrice")) or 0.0
    return total


def _profit_trade_reprice_discount(config: StrategyConfig, lowest_price: float) -> float:
    pct_discount = lowest_price * max(0.0, float(config.profit_trade_reprice_discount_pct)) / 100.0
    min_discount = max(0.0, float(config.profit_trade_reprice_min_discount))
    max_discount = max(min_discount, float(config.profit_trade_reprice_max_discount))
    return min(max(pct_discount, min_discount), max_discount)


def _profit_trade_competitive_listing_price(
    config: StrategyConfig,
    *,
    current_lowest_price: float | None,
    fallback_price: float,
) -> float:
    lowest = safe_float(current_lowest_price)
    if lowest is None or lowest <= 0:
        return round(float(fallback_price), 2)
    discount = _profit_trade_reprice_discount(config, lowest)
    target = max(0.01, lowest - discount)
    return round(min(float(fallback_price), target), 2)


def _profit_trade_stale_listing_age_hours(note: dict[str, Any], *, now: datetime | None = None) -> float | None:
    listed_at = _parse_iso(note.get("c5ListedAt"))
    if listed_at is None:
        return None
    if listed_at.tzinfo is None:
        listed_at = listed_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current.astimezone(timezone.utc) - listed_at.astimezone(timezone.utc)).total_seconds() / 3600.0)


def _profit_trade_stale_listing_price(
    config: StrategyConfig,
    *,
    current_lowest_price: float | None,
    fallback_price: float,
) -> float:
    lowest = safe_float(current_lowest_price)
    if lowest is None or lowest <= 0:
        return round(float(fallback_price), 2)
    offset = max(0.0, float(config.profit_trade_stale_price_offset))
    target = max(0.01, lowest - offset)
    return round(min(float(fallback_price), target), 2)


def _profit_trade_reprice_cooldown_passed(
    config: StrategyConfig,
    note: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    cooldown_hours = max(0.0, float(config.profit_trade_reprice_cooldown_hours))
    if cooldown_hours <= 0:
        return True, None
    anchor = _parse_iso(note.get("repriceAt") or note.get("c5ListedAt"))
    if anchor is None:
        return True, None
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    remaining = timedelta(hours=cooldown_hours) - (current.astimezone(timezone.utc) - anchor.astimezone(timezone.utc))
    if remaining <= timedelta(0):
        return True, None
    minutes = max(1, int(remaining.total_seconds() // 60))
    return False, f"reprice cooldown not passed, {minutes} minutes remaining"


def _extract_c5_price_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("list") or payload.get("items") or payload.get("records") or payload.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("items") or rows.get("records") or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _row_price(row: dict[str, Any]) -> float | None:
    return _first_float(
        row,
        (
            "price",
            "sellPrice",
            "sellerPrice",
            "salePrice",
            "lowestPrice",
            "minPrice",
        ),
    )


def _fetch_c5_listing_depth(
    c5_client: Any,
    settings: Settings,
    *,
    market_hash_name: str,
) -> dict[str, Any]:
    payload = c5_client.goods_search(
        app_id=settings.app_id,
        market_hash_name=market_hash_name,
        delivery=1,
        page=1,
        limit=20,
    )
    rows = _extract_c5_price_rows(payload)
    prices = sorted(
        price
        for price in (_row_price(row) for row in rows)
        if price is not None and price > 0
    )
    return {
        "lowestPrice": prices[0] if prices else None,
        "secondLowestPrice": prices[1] if len(prices) > 1 else None,
        "onSaleCount": safe_int(payload.get("total")) or len(prices),
        "sampleCount": len(prices),
        "rows": rows[:5],
    }


def _listing_depth_from_c5_statistics(statistics: C5RecentSaleRisk | None) -> dict[str, Any] | None:
    if statistics is None:
        return None
    lowest = safe_float(statistics.current_sell_price)
    if lowest is None or lowest <= 0:
        return None
    count = int(statistics.on_sale_count or 0)
    return {
        "lowestPrice": lowest,
        "secondLowestPrice": None,
        "onSaleCount": count,
        "sampleCount": count,
        "rows": [],
        "source": "c5_statistics",
    }


def _merge_c5_listing_depth_and_statistics(
    *,
    depth: dict[str, Any],
    statistics: C5RecentSaleRisk | None,
) -> C5RecentSaleRisk:
    lowest = safe_float(depth.get("lowestPrice"))
    depth_count = safe_int(depth.get("onSaleCount"))
    if statistics is None:
        return C5RecentSaleRisk(
            recent_sold_net_price=None,
            recent_sold_count=None,
            status="raw",
            reason="listing depth fetched without statistics",
            raw={"listingDepth": depth},
            current_sell_price=lowest,
            on_sale_count=depth_count,
        )
    return C5RecentSaleRisk(
        recent_sold_net_price=statistics.recent_sold_net_price,
        recent_sold_count=statistics.recent_sold_count,
        status=statistics.status,
        reason=statistics.reason,
        raw={"statistics": statistics.raw, "listingDepth": depth},
        current_sell_price=lowest if lowest is not None else statistics.current_sell_price,
        on_sale_count=depth_count if depth_count is not None else statistics.on_sale_count,
        purchase_max_price=statistics.purchase_max_price,
        purchase_count=statistics.purchase_count,
    )


def _c5_risk_note(risk: C5RecentSaleRisk | None) -> dict[str, Any] | None:
    if risk is None:
        return None
    return {
        "status": risk.status,
        "reason": risk.reason,
        "currentSellPrice": risk.current_sell_price,
        "onSaleCount": risk.on_sale_count,
        "purchaseMaxPrice": risk.purchase_max_price,
        "purchaseCount": risk.purchase_count,
        "raw": risk.raw,
    }


def _evaluate_c5_orderbook_depth_risk(
    config: StrategyConfig,
    *,
    depth: dict[str, Any],
) -> tuple[bool, str]:
    lowest = safe_float(depth.get("lowestPrice"))
    count = safe_int(depth.get("onSaleCount")) or 0
    sample_count = safe_int(depth.get("sampleCount")) or 0
    min_count = max(0, int(config.profit_trade_c5_min_on_sale_count))
    if lowest is None or lowest <= 0:
        return False, "C5 orderbook has no usable lowest sell price"
    if count < min_count or sample_count < min_count:
        return False, f"C5 orderbook depth too low: sale={count}, sample={sample_count}, min={min_count}"
    second = safe_float(depth.get("secondLowestPrice"))
    if second is not None and second > 0:
        gap_pct = (second - lowest) / lowest
        if gap_pct > 0.2:
            return False, f"C5 sell price gap too large: {(gap_pct * 100):.2f}%"
    return True, "C5 orderbook depth passed"


def _realized_values(
    *,
    sold_net_price: float,
    steam_buy_price: float,
    steam_cost_ratio: float,
) -> tuple[float, float]:
    steam_real_cost = steam_buy_price * steam_cost_ratio
    realized_profit = sold_net_price - steam_real_cost
    realized_roi = _profit_trade_transfer_roi(
        c5_expected_net=sold_net_price,
        steam_buy_price=steam_buy_price,
        steam_cost_ratio=steam_cost_ratio,
    )
    return realized_profit, float(realized_roi or 0.0)


def _inventory_item_key(item: dict[str, Any]) -> str:
    return str(item.get("assetId") or item.get("asset_id") or "").strip()


def _profit_trade_protection_reason(
    config: StrategyConfig,
    *,
    asset_id: str | None,
    market_hash_name: str | None,
    steam_id: str | None,
) -> str | None:
    protected_assets = {str(value).strip() for value in config.profit_trade_protected_asset_ids or [] if str(value).strip()}
    protected_names = {
        str(value).strip().lower()
        for value in config.profit_trade_protected_market_hash_names or []
        if str(value).strip()
    }
    protected_steam_ids = {
        str(value).strip()
        for value in config.profit_trade_protected_steam_ids or []
        if str(value).strip()
    }
    normalized_asset_id = str(asset_id or "").strip()
    normalized_name = str(market_hash_name or "").strip().lower()
    normalized_steam_id = str(steam_id or "").strip()
    if normalized_asset_id and normalized_asset_id in protected_assets:
        return f"protected assetId: {normalized_asset_id}"
    if normalized_name and normalized_name in protected_names:
        return f"protected marketHashName: {market_hash_name}"
    if normalized_steam_id and normalized_steam_id in protected_steam_ids:
        return f"protected steamId: {normalized_steam_id}"
    return None


def _profit_trade_builtin_block_reason(market_hash_name: str | None) -> str | None:
    return None


def _profit_trade_type_block_reason(config: StrategyConfig, market_hash_name: str | None) -> str | None:
    normalized_name = str(market_hash_name or "").strip().lower()
    if not normalized_name:
        return None
    if normalized_name.startswith("sticker slab |") and config.profit_trade_sticker_slab_status != "active":
        return "profitTrade item type status blocked: Sticker Slab"
    if normalized_name.startswith("sticker |") and config.profit_trade_sticker_status != "active":
        return "profitTrade item type status blocked: Sticker"
    return None


def _inventory_item_protection_reason(config: StrategyConfig, item: dict[str, Any]) -> str | None:
    configured_reason = _profit_trade_protection_reason(
        config,
        asset_id=_inventory_item_key(item),
        market_hash_name=str(item.get("marketHashName") or "").strip(),
        steam_id=str(item.get("steamId") or "").strip(),
    )
    if configured_reason is not None:
        return configured_reason
    type_reason = _profit_trade_type_block_reason(
        config,
        str(item.get("marketHashName") or "").strip(),
    )
    if type_reason is not None:
        return type_reason
    return _profit_trade_builtin_block_reason(str(item.get("marketHashName") or "").strip())


def _is_tradable_c5_item(item: dict[str, Any]) -> bool:
    if item.get("ifTradable") is True:
        return True
    tradable_time = _parse_iso(item.get("tradableTime"))
    if tradable_time is None:
        return False
    if tradable_time.tzinfo is None:
        tradable_time = tradable_time.replace(tzinfo=timezone.utc)
    return tradable_time <= datetime.now(timezone.utc)


def _pick_sell_asset(
    db: Database,
    config: StrategyConfig,
    inventory_items: list[dict[str, Any]],
    *,
    market_hash_name: str,
) -> dict[str, Any] | None:
    for item in sorted(inventory_items, key=_inventory_item_key):
        if str(item.get("marketHashName") or "").strip() != market_hash_name:
            continue
        asset_id = _inventory_item_key(item)
        if not asset_id:
            continue
        if _inventory_item_protection_reason(config, item):
            continue
        if not _is_tradable_c5_item(item):
            continue
        if not str(item.get("token") or "").strip():
            continue
        if not str(item.get("styleToken") or item.get("style_token") or "").strip():
            continue
        if db.get_active_asset_reservation(asset_id) is not None:
            continue
        if db.get_live_profit_trade_for_asset(asset_id) is not None:
            continue
        return item
    return None


def _state_price_is_usable_for_profit_trade(state: MarketState) -> bool:
    return (
        state.steam_sell_price is not None
        and state.steam_price_source == "steam_orderbook"
        and state.c5_sell_price is not None
        and state.c5_price_source == "c5_batch"
    )


def _build_opportunity(
    *,
    config: StrategyConfig,
    item_type: dict[str, Any],
    state: MarketState,
    sell_item: dict[str, Any],
    c5_risk: C5RecentSaleRisk | None = None,
) -> ProfitTradeOpportunity | None:
    if not _state_price_is_usable_for_profit_trade(state):
        return None
    c5_listing_price = safe_float(state.c5_sell_price)
    steam_buy_price = safe_float(state.steam_sell_price)
    if c5_listing_price is None or steam_buy_price is None:
        return None
    if c5_listing_price <= 0 or steam_buy_price <= 0:
        return None
    if c5_risk is not None:
        c5_listing_price = _profit_trade_competitive_listing_price(
            config,
            current_lowest_price=c5_risk.current_sell_price,
            fallback_price=float(c5_listing_price),
        )
    if c5_listing_price < float(config.profit_trade_min_item_value):
        return None

    listing_net = c5_listing_price * float(config.profit_trade_c5_current_sale_net_factor)
    evaluated_depth = (
        _evaluate_c5_market_depth_risk(
            config,
            c5_listing_price=float(c5_listing_price),
            risk=c5_risk,
        )
        if config.profit_trade_require_c5_market_depth
        else C5RecentSaleRisk(
            recent_sold_net_price=None,
            recent_sold_count=None,
            status="disabled",
            reason="C5 current market depth risk is disabled",
            raw={},
        )
    )
    if config.profit_trade_require_c5_recent_sales:
        evaluated_risk = _evaluate_c5_recent_sale_risk(
            config,
            c5_listing_price=float(c5_listing_price),
            risk=c5_risk,
        )
        if evaluated_risk.recent_sold_net_price is not None:
            c5_expected_net = min(listing_net, float(evaluated_risk.recent_sold_net_price))
        else:
            c5_expected_net = listing_net
    else:
        c5_expected_net = listing_net
        evaluated_risk = C5RecentSaleRisk(
            recent_sold_net_price=None,
            recent_sold_count=None,
            status="disabled",
            reason="C5 market depth/recent sale risk is disabled",
            raw={},
            current_sell_price=evaluated_depth.current_sell_price,
            on_sale_count=evaluated_depth.on_sale_count,
        )
    steam_cost_ratio = _profit_trade_steam_cost_ratio(config)
    steam_real_cost = steam_buy_price * steam_cost_ratio
    if steam_real_cost <= 0:
        return None
    expected_profit = c5_expected_net - steam_real_cost
    expected_roi = _profit_trade_transfer_roi(
        c5_expected_net=c5_expected_net,
        steam_buy_price=steam_buy_price,
        steam_cost_ratio=steam_cost_ratio,
    )
    if expected_roi is None:
        return None
    manual_review_roi = float(config.profit_trade_manual_review_roi)
    if manual_review_roi > 0 and expected_roi > manual_review_roi:
        return ProfitTradeOpportunity(
            market_hash_name=str(item_type["market_hash_name"]),
            name=str(state.name_cn or item_type.get("name_cn") or item_type["market_hash_name"]),
            asset_id=_inventory_item_key(sell_item),
            steam_id=str(sell_item.get("steamId") or "").strip() or None,
            token=str(sell_item.get("token") or "").strip() or None,
            style_token=str(sell_item.get("styleToken") or sell_item.get("style_token") or "").strip() or None,
            steam_buy_price=float(steam_buy_price),
            steam_price_source=state.steam_price_source or "unknown",
            c5_listing_price=float(c5_listing_price),
            c5_price_source=state.c5_price_source or "unknown",
            c5_expected_net_price=float(c5_expected_net),
            steam_real_cost=float(steam_real_cost),
            expected_profit=float(expected_profit),
            expected_roi=float(expected_roi),
            inventory_count=int(item_type["inventory_count"]),
            tradable_count=int(item_type["tradable_count"]),
            c5_recent_sold_net_price=evaluated_risk.recent_sold_net_price,
            c5_recent_sold_count=evaluated_risk.recent_sold_count,
            c5_current_sell_price=evaluated_depth.current_sell_price,
            c5_on_sale_count=evaluated_depth.on_sale_count,
            c5_purchase_max_price=evaluated_depth.purchase_max_price,
            c5_purchase_count=evaluated_depth.purchase_count,
            liquidity_status=evaluated_depth.status if config.profit_trade_require_c5_market_depth else evaluated_risk.status,
            audit_status="manual_required",
            audit_reason=f"ROI {expected_roi * 100:.2f}% > manual review threshold {manual_review_roi * 100:.2f}%",
        )
    if config.profit_trade_require_c5_market_depth and evaluated_depth.status != "passed":
        return None
    if config.profit_trade_require_c5_recent_sales and evaluated_risk.status != "passed":
        return None
    if expected_roi < float(config.profit_trade_min_roi):
        return None
    if config.profit_trade_ai_audit_enabled:
        return None

    return ProfitTradeOpportunity(
        market_hash_name=str(item_type["market_hash_name"]),
        name=str(state.name_cn or item_type.get("name_cn") or item_type["market_hash_name"]),
        asset_id=_inventory_item_key(sell_item),
        steam_id=str(sell_item.get("steamId") or "").strip() or None,
        token=str(sell_item.get("token") or "").strip() or None,
        style_token=str(sell_item.get("styleToken") or sell_item.get("style_token") or "").strip() or None,
        steam_buy_price=float(steam_buy_price),
        steam_price_source=state.steam_price_source or "unknown",
        c5_listing_price=float(c5_listing_price),
        c5_price_source=state.c5_price_source or "unknown",
        c5_expected_net_price=float(c5_expected_net),
        steam_real_cost=float(steam_real_cost),
        expected_profit=float(expected_profit),
        expected_roi=float(expected_roi),
        inventory_count=int(item_type["inventory_count"]),
        tradable_count=int(item_type["tradable_count"]),
        c5_recent_sold_net_price=evaluated_risk.recent_sold_net_price,
        c5_recent_sold_count=evaluated_risk.recent_sold_count,
        c5_current_sell_price=evaluated_depth.current_sell_price,
        c5_on_sale_count=evaluated_depth.on_sale_count,
        c5_purchase_max_price=evaluated_depth.purchase_max_price,
        c5_purchase_count=evaluated_depth.purchase_count,
        liquidity_status=evaluated_risk.status
        if config.profit_trade_require_c5_recent_sales
        else evaluated_depth.status,
    )


def _trade_no() -> str:
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    return f"PT-{today}-{uuid.uuid4().hex[:10]}"


def _reservation_until(config: StrategyConfig) -> str:
    seconds = max(1, int(config.profit_trade_reservation_seconds))
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def _opportunity_note(opportunity: ProfitTradeOpportunity, *, source: str) -> str:
    return _build_note(
        {
            "source": source,
            "name": opportunity.name,
            "assetId": opportunity.asset_id,
            "steamId": opportunity.steam_id,
            "token": opportunity.token,
            "styleToken": opportunity.style_token,
            "steamBuyPrice": round(opportunity.steam_buy_price, 2),
            "steamPriceSource": opportunity.steam_price_source,
            "steamBalanceDiscount": opportunity.steam_real_cost / opportunity.steam_buy_price
            if opportunity.steam_buy_price > 0
            else None,
            "c5ListingPrice": round(opportunity.c5_listing_price, 2),
            "c5ExpectedNetPrice": round(opportunity.c5_expected_net_price, 2),
            "c5RecentSoldNetPrice": opportunity.c5_recent_sold_net_price,
            "c5RecentSoldCount": opportunity.c5_recent_sold_count,
            "c5CurrentSellPrice": getattr(opportunity, "c5_current_sell_price", None),
            "c5OnSaleCount": getattr(opportunity, "c5_on_sale_count", None),
            "c5PurchaseMaxPrice": getattr(opportunity, "c5_purchase_max_price", None),
            "c5PurchaseCount": getattr(opportunity, "c5_purchase_count", None),
            "liquidityStatus": opportunity.liquidity_status,
            "auditStatus": opportunity.audit_status,
            "auditReason": opportunity.audit_reason,
        }
    )


def _create_profit_trade_from_opportunity(
    db: Database,
    config: StrategyConfig,
    opportunity: ProfitTradeOpportunity,
    *,
    lock_asset: bool,
) -> int | None:
    if _profit_trade_protection_reason(
        config,
        asset_id=opportunity.asset_id,
        market_hash_name=opportunity.market_hash_name,
        steam_id=opportunity.steam_id,
    ):
        return None
    if db.get_live_profit_trade_for_asset(opportunity.asset_id) is not None:
        return None
    if db.get_active_asset_reservation(opportunity.asset_id) is not None:
        return None

    status = "candidate"
    step_key = "discovered"
    step_index = 0
    error: str | None = None
    reservation_id: int | None = None
    note = _opportunity_note(opportunity, source="profit_trade_scan")

    if opportunity.audit_status == "manual_required":
        status = "manual_required"
        step_key = "audited"
        step_index = 1
        error = opportunity.audit_reason
        note = _build_note(
            {
                **_read_note(note),
                "manualReviewAt": utc_now_iso(),
                "manualReviewReason": opportunity.audit_reason,
            }
        )

    if lock_asset:
        if opportunity.audit_status == "manual_required":
            lock_asset = False
        else:
            status = "locked"
            step_key = "asset_locked"
            step_index = 2

    if lock_asset:
        reserved_until = _reservation_until(config)
        reservation_id = db.reserve_asset(
            asset_id=opportunity.asset_id,
            market_hash_name=opportunity.market_hash_name,
            owner=PROFIT_TRADE_OWNER,
            purpose="sell_existing_a",
            reserved_until=reserved_until,
            note=_build_note({"source": "profit_trade_scan", "trade": "pending"}),
        )
        if reservation_id is None:
            return None
        note = _build_note(
            {
                **_read_note(note),
                "reservationId": reservation_id,
                "reservedUntil": reserved_until,
            }
        )

    trade_id = db.add_profit_trade(
        trade_no=_trade_no(),
        market_hash_name=opportunity.market_hash_name,
        status=status,
        step_key=step_key,
        step_index=step_index,
        a_asset_id=opportunity.asset_id,
        a_steam_id=opportunity.steam_id,
        steam_buy_price=opportunity.steam_buy_price,
        steam_balance_discount=opportunity.steam_real_cost / opportunity.steam_buy_price
        if opportunity.steam_buy_price > 0
        else None,
        steam_real_cost=opportunity.steam_real_cost,
        c5_listing_price=opportunity.c5_listing_price,
        c5_expected_net_price=opportunity.c5_expected_net_price,
        expected_profit=opportunity.expected_profit,
        expected_roi=opportunity.expected_roi,
        error=error,
        note=note,
    )
    if reservation_id is not None:
        db.attach_asset_reservation_operation(
            reservation_id=reservation_id,
            operation_id=trade_id,
            note=_build_note(
                {
                    "source": "profit_trade_scan",
                    "tradeId": trade_id,
                    "reservedUntil": _read_note(note).get("reservedUntil"),
                }
            ),
        )
    return trade_id


def _send_profit_trade_manual_review_alert(
    settings: Settings,
    opportunity: ProfitTradeOpportunity,
    *,
    trade_id: int,
) -> bool:
    if not settings.serverchan_sendkey:
        return False
    body = "\n".join(
        [
            "## 搬砖做T异常收益拦截",
            "",
            f"- 流水ID: {trade_id}",
            f"- 饰品: {opportunity.name}",
            f"- Hash: {opportunity.market_hash_name}",
            f"- ROI: {opportunity.expected_roi_pct:.2f}%",
            f"- Steam买入价: CNY {opportunity.steam_buy_price:.2f}",
            f"- C5挂价: CNY {opportunity.c5_listing_price:.2f}",
            f"- C5预计到手: CNY {opportunity.c5_expected_net_price:.2f}",
            f"- 真实成本: CNY {opportunity.steam_real_cost:.2f}",
            f"- 预计收益: CNY {opportunity.expected_profit:.2f}",
            f"- C5当前统计价: {opportunity.c5_current_sell_price if opportunity.c5_current_sell_price is not None else '-'}",
            f"- C5在售数量: {opportunity.c5_on_sale_count if opportunity.c5_on_sale_count is not None else '-'}",
            f"- 风控状态: {opportunity.liquidity_status}",
            f"- 拦截原因: {opportunity.audit_reason}",
            "",
            "程序已拦截，未买入 B，未上架 C5；需要人工确认。",
        ]
    )
    client = ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url)
    client.send("搬砖做T异常收益需人工确认", body)
    return True


def _send_profit_trade_listing_alert(
    settings: Settings,
    *,
    title: str,
    row: Any,
    body_lines: list[str],
) -> bool:
    if not settings.serverchan_sendkey:
        return False
    body = "\n".join(
        [
            "## 搬砖做T上架风控",
            "",
            f"- 流水: {row['trade_no']}",
            f"- 饰品: {row['market_hash_name']}",
            *body_lines,
        ]
    )
    client = ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url)
    client.send(title, body)
    return True


def _mark_expired_locked_trades(db: Database) -> None:
    for row in db.list_profit_trades(status="locked", limit=500):
        asset_id = str(row["a_asset_id"] or "").strip()
        if not asset_id:
            continue
        if db.get_active_asset_reservation(asset_id) is not None:
            continue
        note = _read_note(row["note"])
        db.update_profit_trade(
            int(row["id"]),
            status="cancelled",
            error=None,
            note=_build_note(
                {
                    **note,
                    "cancelReason": "asset reservation expired before Steam buy step",
                    "reservationExpiredAt": utc_now_iso(),
                }
            ),
        )


def _trade_is_before_steam_buy(row: Any) -> bool:
    status = str(row["status"] or "").strip()
    step_key = str(row["step_key"] or "").strip()
    step_index = int(row["step_index"] or 0)
    if str(row["b_asset_id"] or "").strip():
        return False
    if str(row["steam_listing_id"] or "").strip():
        return False
    if str(row["c5_product_id"] or "").strip():
        return False
    if status in PRE_STEAM_BUY_PROFIT_TRADE_STATUSES:
        return True
    return status == "manual_required" and step_index <= 2 and step_key in PRE_STEAM_BUY_STEP_KEYS


def _cancel_pre_steam_buy_trade(
    db: Database,
    row: Any,
    *,
    reason: str,
    source: str,
    extra_note: dict[str, Any] | None = None,
    update_fields: dict[str, Any] | None = None,
) -> bool:
    if row is None or not _trade_is_before_steam_buy(row):
        return False
    trade_id = int(row["id"])
    asset_id = str(row["a_asset_id"] or "").strip()
    if asset_id:
        reservation = db.get_active_asset_reservation(asset_id)
        reservation_status = str(reservation["status"] or "") if reservation is not None else ""
        reservation_owner = str(reservation["owner"] or "") if reservation is not None else ""
        reservation_operation_id = int(reservation["operation_id"]) if reservation is not None and reservation["operation_id"] is not None else None
        if (
            reservation is not None
            and reservation_status == "active"
            and reservation_owner == PROFIT_TRADE_OWNER
            and reservation_operation_id == trade_id
        ):
            db.release_asset_reservation(
                asset_id=asset_id,
                owner=PROFIT_TRADE_OWNER,
                reason=_build_note(
                    {
                        "source": source,
                        "tradeId": trade_id,
                        "reason": reason,
                    }
                ),
            )
    note = _read_note(row["note"])
    db.update_profit_trade(
        trade_id,
        status="cancelled",
        error=None,
        **(update_fields or {}),
        note=_build_note(
            {
                **note,
                "cancelReason": reason,
                "cancelSource": source,
                "cancelledBeforeSteamBuyAt": utc_now_iso(),
                **(extra_note or {}),
            }
        ),
    )
    return True


def _cancel_locked_trade_before_steam_buy(db: Database, trade_id: int, *, reason: str) -> bool:
    row = db.get_profit_trade(trade_id)
    if row is None or str(row["status"] or "") != "locked":
        return False
    return _cancel_pre_steam_buy_trade(
        db,
        row,
        reason=reason,
        source="profit_trade_pre_buy_cancel",
    )


def _cancel_locked_trade_before_steam_buy_by_id(settings: Settings, trade_id: int, *, reason: str) -> bool:
    db = Database(settings.db_path)
    try:
        db.initialize()
        return _cancel_locked_trade_before_steam_buy(db, trade_id, reason=reason)
    finally:
        db.close()


def _cancel_stale_pre_buy_manual_trades(db: Database) -> None:
    for row in db.list_profit_trades(status="manual_required", limit=1000):
        if not _trade_is_before_steam_buy(row):
            continue
        note = _read_note(row["note"])
        error = str(row["error"] or "").strip()
        if error != "A asset reservation is not active before Steam buy" and not note.get("reservationMissingAt"):
            continue
        _cancel_pre_steam_buy_trade(
            db,
            row,
            reason=error or "A asset reservation was not active before Steam buy",
            source="profit_trade_stale_pre_buy_cleanup",
        )


def _cancel_stale_buying_trades_without_steam_evidence(db: Database) -> None:
    now = datetime.now(timezone.utc)
    for row in db.list_profit_trades(status="buying", limit=1000):
        note = _read_note(row["note"])
        if str(row["b_asset_id"] or "").strip():
            continue
        if str(row["c5_product_id"] or "").strip():
            continue
        if str(row["steam_listing_id"] or "").strip():
            continue
        if any(
            str(note.get(key) or "").strip()
            for key in (
                "steamBuySucceededAt",
                "steamBuyMethod",
                "steamBuyOrderId",
                "steamListingId",
            )
        ):
            continue
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"] or "").replace("Z", "+00:00"))
        except ValueError:
            updated_at = None
        if updated_at is not None:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if (now - updated_at).total_seconds() < 30 * 60:
                continue
        trade_id = int(row["id"])
        asset_id = str(row["a_asset_id"] or "").strip()
        if asset_id:
            reservation = db.get_active_asset_reservation(asset_id)
            if (
                reservation is not None
                and str(reservation["owner"] or "") == PROFIT_TRADE_OWNER
                and str(reservation["status"] or "") == "active"
                and (
                    reservation["operation_id"] is None
                    or int(reservation["operation_id"]) == trade_id
                )
            ):
                db.release_asset_reservation(
                    asset_id=asset_id,
                    owner=PROFIT_TRADE_OWNER,
                    reason=_build_note(
                        {
                            "source": "profit_trade_stale_buying_cleanup",
                            "tradeId": trade_id,
                            "reason": "stale buying state without Steam buy evidence",
                        }
                    ),
                )
        db.update_profit_trade(
            trade_id,
            status="cancelled",
            error=None,
            note=_build_note(
                {
                    **note,
                    "cancelReason": "stale buying state without Steam buy evidence",
                    "cancelSource": "profit_trade_stale_buying_cleanup",
                    "cancelledBeforeSteamBuyAt": utc_now_iso(),
                    "staleBuyingCleanupAt": utc_now_iso(),
                }
            ),
        )


def _cancel_recorded_pre_buy_candidates(db: Database, *, reason: str) -> None:
    for status in ("candidate", "audited"):
        for row in db.list_profit_trades(status=status, limit=1000):
            _cancel_pre_steam_buy_trade(
                db,
                row,
                reason=reason,
                source="profit_trade_fresh_run",
            )


def _cancel_protected_pre_buy_trades(db: Database, config: StrategyConfig) -> None:
    for status in ("candidate", "audited", "locked"):
        for row in db.list_profit_trades(status=status, limit=1000):
            reason = _profit_trade_protection_reason(
                config,
                asset_id=str(row["a_asset_id"] or "").strip(),
                market_hash_name=str(row["market_hash_name"] or "").strip(),
                steam_id=str(row["a_steam_id"] or "").strip(),
            )
            if reason is None:
                reason = _profit_trade_type_block_reason(
                    config,
                    str(row["market_hash_name"] or "").strip(),
                )
            if reason is None:
                continue
            asset_id = str(row["a_asset_id"] or "").strip()
            if asset_id:
                db.release_asset_reservation(
                    asset_id=asset_id,
                    owner=PROFIT_TRADE_OWNER,
                    reason=_build_note(
                        {
                            "source": "profit_trade_protection",
                            "tradeId": int(row["id"]),
                            "reason": reason,
                        }
                    ),
                )
            note = _read_note(row["note"])
            db.update_profit_trade(
                int(row["id"]),
                status="cancelled",
                error=None,
                note=_build_note(
                    {
                        **note,
                        "cancelReason": reason,
                        "protectedAt": utc_now_iso(),
                    }
                ),
            )


def _trade_c5_risk_block_reason(config: StrategyConfig, row: Any) -> str | None:
    if not config.profit_trade_require_c5_recent_sales and not config.profit_trade_require_c5_market_depth:
        return None
    note = _read_note(row["note"])
    liquidity_status = str(note.get("liquidityStatus") or "").strip()
    if liquidity_status == "passed":
        return None
    return f"C5 risk is not passed: {liquidity_status or 'missing'}"


def _cancel_c5_risk_failed_pre_buy_trades(db: Database, config: StrategyConfig) -> None:
    if not config.profit_trade_require_c5_recent_sales and not config.profit_trade_require_c5_market_depth:
        return
    for status in ("candidate", "audited", "locked"):
        for row in db.list_profit_trades(status=status, limit=1000):
            reason = _trade_c5_risk_block_reason(config, row)
            if reason is None:
                continue
            asset_id = str(row["a_asset_id"] or "").strip()
            if asset_id:
                db.release_asset_reservation(
                    asset_id=asset_id,
                    owner=PROFIT_TRADE_OWNER,
                    reason=_build_note(
                        {
                            "source": "profit_trade_c5_risk",
                            "tradeId": int(row["id"]),
                            "reason": reason,
                        }
                    ),
                )
            note = _read_note(row["note"])
            db.update_profit_trade(
                int(row["id"]),
                status="cancelled",
                error=None,
                note=_build_note(
                    {
                        **note,
                        "cancelReason": reason,
                        "cancelledByC5RiskAt": utc_now_iso(),
                    }
                ),
            )


def _trade_manual_review_block_reason(config: StrategyConfig, row: Any) -> str | None:
    threshold = float(config.profit_trade_manual_review_roi)
    if threshold <= 0:
        return None
    expected_roi = safe_float(row["expected_roi"])
    if expected_roi is None or expected_roi <= threshold:
        return None
    return f"ROI {expected_roi * 100:.2f}% > manual review threshold {threshold * 100:.2f}%"


def _mark_manual_review_pre_buy_trades(db: Database, config: StrategyConfig) -> None:
    for status in ("candidate", "audited", "locked"):
        for row in db.list_profit_trades(status=status, limit=1000):
            reason = _trade_manual_review_block_reason(config, row)
            if reason is None:
                continue
            asset_id = str(row["a_asset_id"] or "").strip()
            if asset_id:
                db.release_asset_reservation(
                    asset_id=asset_id,
                    owner=PROFIT_TRADE_OWNER,
                    reason=_build_note(
                        {
                            "source": "profit_trade_manual_review",
                            "tradeId": int(row["id"]),
                            "reason": reason,
                        }
                    ),
                )
            note = _read_note(row["note"])
            db.update_profit_trade(
                int(row["id"]),
                status="manual_required",
                step_key="audited",
                step_index=1,
                error=reason,
                note=_build_note(
                    {
                        **note,
                        "auditStatus": "manual_required",
                        "auditReason": reason,
                        "manualReviewAt": utc_now_iso(),
                        "manualReviewReason": reason,
                    }
                ),
            )


def scan_profit_trade_opportunities(
    settings: Settings,
    config: StrategyConfig | None = None,
    *,
    allow_cached_fallback: bool = True,
    cache_max_age_minutes: int | None = 180,
    limit: int = 20,
    scan_max_items: int | None = None,
    record: bool = False,
    lock_asset: bool = False,
    inventory_payload: dict[str, Any] | None = None,
    market_service: MarketService | None = None,
    c5_client: Any | None = None,
) -> ProfitTradeScanReport:
    if config is None:
        config = load_strategy_config(settings)
    if not settings.c5_api_key and inventory_payload is None:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    if limit <= 0:
        raise ValueError("--limit 必须大于 0")

    db = Database(settings.db_path)
    db.initialize()
    created_trade_ids: list[int] = []
    locked_trade_ids: list[int] = []
    notes: list[str] = []
    try:
        db.release_expired_asset_reservations()
        _mark_expired_locked_trades(db)
        _cancel_stale_pre_buy_manual_trades(db)
        _cancel_stale_buying_trades_without_steam_evidence(db)
        if record and not lock_asset:
            _cancel_recorded_pre_buy_candidates(
                db,
                reason="fresh scan superseded recorded pre-buy candidate",
            )
        _cancel_protected_pre_buy_trades(db, config)
        _mark_manual_review_pre_buy_trades(db, config)
        _cancel_c5_risk_failed_pre_buy_trades(db, config)
        if inventory_payload is None:
            c5_client = c5_client or C5GameClient(str(settings.c5_api_key), settings.c5_base_url)
            inventory_payload = fetch_all_c5_inventories(
                c5_client,
                settings,
                allow_cached_fallback=allow_cached_fallback,
                cache_max_age_minutes=cache_max_age_minutes,
        )
        inventory_items = [
            item for item in list(inventory_payload.get("list") or []) if isinstance(item, dict)
        ]
        db.upsert_inventory_assets(inventory_items)
        all_inventory_types = summarize_inventory_types(inventory_items)
        inventory_types = [
            item_type
            for item_type in all_inventory_types
            if int(item_type.get("tradable_count") or 0) > 0
            and (
                safe_float(item_type.get("reference_price")) is None
                or float(safe_float(item_type.get("reference_price")) or 0) >= float(config.profit_trade_min_item_value)
            )
        ]
        inventory_types.sort(
            key=lambda item_type: (
                -(safe_float(item_type.get("reference_price")) or 0.0),
                str(item_type.get("market_hash_name") or ""),
            )
        )
        max_items = int(scan_max_items or config.profit_trade_scan_max_items)
        max_items = max(limit, max(1, max_items))
        if len(inventory_types) > max_items:
            notes.append(
                f"Prefiltered {len(inventory_types)} tradable item types to top {max_items} by C5 reference price."
            )
            inventory_types = inventory_types[:max_items]
        if market_service is None:
            market_service = _build_profit_trade_market_service(settings)
        states = market_service.refresh_items(inventory_types) if inventory_types else []
        state_map = {state.market_hash_name: state for state in states}
        if c5_client is None and settings.c5_api_key:
            c5_client = C5GameClient(str(settings.c5_api_key), settings.c5_base_url)
        c5_risks = (
            _fetch_c5_recent_sale_risks(
                c5_client,
                app_id=settings.app_id,
                market_hash_names=[str(item["market_hash_name"]) for item in inventory_types],
            )
            if c5_client is not None
            else {}
        )

        opportunities: list[ProfitTradeOpportunity] = []
        missing_price_count = 0
        skipped_count = 0
        for item_type in inventory_types:
            state = state_map.get(str(item_type["market_hash_name"]))
            if state is None or not _state_price_is_usable_for_profit_trade(state):
                missing_price_count += 1
                continue
            sell_item = _pick_sell_asset(
                db,
                config,
                inventory_items,
                market_hash_name=str(item_type["market_hash_name"]),
            )
            if sell_item is None:
                skipped_count += 1
                continue
            opportunity = _build_opportunity(
                config=config,
                item_type=item_type,
                state=state,
                sell_item=sell_item,
                c5_risk=c5_risks.get(str(item_type["market_hash_name"])),
            )
            if opportunity is None:
                skipped_count += 1
                continue
            opportunities.append(opportunity)

        opportunities.sort(key=lambda item: (-item.expected_roi, -item.c5_listing_price, item.market_hash_name))
        opportunities = opportunities[:limit]
        if config.profit_trade_ai_audit_enabled:
            notes.append("AI audit enabled but no AI auditor is configured; opportunities are blocked.")
        if config.profit_trade_require_c5_market_depth:
            notes.append("C5 current market depth risk is required; opportunities without usable C5 sell price/count statistics are blocked.")
        if config.profit_trade_require_c5_recent_sales:
            notes.append("C5 recent sale risk is required; opportunities without sufficient C5 recent sale statistics are blocked.")

        if record:
            for opportunity in opportunities:
                trade_id = _create_profit_trade_from_opportunity(
                    db,
                    config,
                    opportunity,
                    lock_asset=lock_asset,
                )
                if trade_id is None:
                    continue
                created_trade_ids.append(trade_id)
                if lock_asset and opportunity.audit_status != "manual_required":
                    locked_trade_ids.append(trade_id)
                if opportunity.audit_status == "manual_required":
                    alert_note: dict[str, Any] = {}
                    try:
                        sent = _send_profit_trade_manual_review_alert(
                            settings,
                            opportunity,
                            trade_id=trade_id,
                        )
                        alert_note = {
                            "manualReviewServerChanSent": bool(sent),
                            "manualReviewServerChanAt": utc_now_iso() if sent else None,
                        }
                    except Exception as exc:
                        alert_note = {
                            "manualReviewServerChanSent": False,
                            "manualReviewServerChanError": str(exc),
                        }
                    row = db.get_profit_trade(trade_id)
                    if row is not None and alert_note:
                        db.update_profit_trade(
                            trade_id,
                            note=_build_note({**_read_note(row["note"]), **alert_note}),
                        )
    finally:
        db.close()

    return ProfitTradeScanReport(
        generated_at=utc_now_iso(),
        inventory_source=str((inventory_payload or {}).get("source") or "live"),
        inventory_count=len(list((inventory_payload or {}).get("list") or [])),
        evaluated_count=len(inventory_types) if "inventory_types" in locals() else 0,
        opportunity_count=len(opportunities) if "opportunities" in locals() else 0,
        missing_price_count=missing_price_count if "missing_price_count" in locals() else 0,
        skipped_count=skipped_count if "skipped_count" in locals() else 0,
        opportunities=opportunities if "opportunities" in locals() else [],
        created_trade_ids=created_trade_ids,
        locked_trade_ids=locked_trade_ids,
        notes=notes,
    )


def lock_profit_trade(settings: Settings, trade_id: int) -> dict[str, Any]:
    config = load_strategy_config(settings)
    db = Database(settings.db_path)
    try:
        db.initialize()
        db.release_expired_asset_reservations()
        _cancel_stale_pre_buy_manual_trades(db)
        _cancel_stale_buying_trades_without_steam_evidence(db)
        _cancel_protected_pre_buy_trades(db, config)
        _mark_manual_review_pre_buy_trades(db, config)
        _cancel_c5_risk_failed_pre_buy_trades(db, config)
        row = db.get_profit_trade(trade_id)
        if row is None:
            raise RuntimeError(f"profit trade not found: {trade_id}")
        if str(row["status"]) not in {"candidate", "audited"}:
            return {"ok": True, "trade": _trade_row_to_dict(row), "changed": False}
        asset_id = str(row["a_asset_id"] or "").strip()
        market_hash_name = str(row["market_hash_name"] or "").strip()
        if not asset_id or not market_hash_name:
            raise RuntimeError("trade missing A asset")
        protected_reason = _profit_trade_protection_reason(
            config,
            asset_id=asset_id,
            market_hash_name=market_hash_name,
            steam_id=str(row["a_steam_id"] or _read_note(row["note"]).get("steamId") or "").strip(),
        )
        if protected_reason is None:
            protected_reason = _profit_trade_type_block_reason(config, market_hash_name)
        if protected_reason is not None:
            raise RuntimeError(f"profitTrade protected asset: {protected_reason}")
        c5_risk_reason = _trade_c5_risk_block_reason(config, row)
        if c5_risk_reason is not None:
            raise RuntimeError(c5_risk_reason)
        existing = db.get_active_asset_reservation(asset_id)
        if existing is not None:
            raise RuntimeError(f"asset already reserved: {asset_id}")
        reserved_until = _reservation_until(config)
        reservation_id = db.reserve_asset(
            asset_id=asset_id,
            market_hash_name=market_hash_name,
            owner=PROFIT_TRADE_OWNER,
            purpose="sell_existing_a",
            reserved_until=reserved_until,
            operation_id=trade_id,
            note=_build_note({"source": "manual_lock", "tradeId": trade_id, "reservedUntil": reserved_until}),
        )
        if reservation_id is None:
            raise RuntimeError(f"failed to reserve asset: {asset_id}")
        note = _read_note(row["note"])
        db.update_profit_trade(
            trade_id,
            status="locked",
            step_key="asset_locked",
            step_index=2,
            note=_build_note(
                {
                    **note,
                    "reservationId": reservation_id,
                    "reservedUntil": reserved_until,
                    "lockedAt": utc_now_iso(),
                }
            ),
        )
        updated = db.get_profit_trade(trade_id)
        return {"ok": True, "trade": _trade_row_to_dict(updated), "changed": True}
    finally:
        db.close()


def execute_profit_trade_buy(
    settings: Settings,
    trade_id: int,
    *,
    config: StrategyConfig | None = None,
    steam_client: Any | None = None,
    c5_client: Any | None = None,
) -> dict[str, Any]:
    config = config or load_strategy_config(settings)
    _require_profit_trade_real_execution(config)
    db = Database(settings.db_path)
    try:
        db.initialize()
        db.release_expired_asset_reservations()
        _cancel_stale_pre_buy_manual_trades(db)
        _cancel_stale_buying_trades_without_steam_evidence(db)
        _cancel_protected_pre_buy_trades(db, config)
        _mark_manual_review_pre_buy_trades(db, config)
        _cancel_c5_risk_failed_pre_buy_trades(db, config)
        row = db.get_profit_trade(trade_id)
        if row is None:
            raise RuntimeError(f"profit trade not found: {trade_id}")
        if str(row["status"]) != "locked":
            raise RuntimeError(f"trade status must be locked before Steam buy: {row['status']}")

        asset_id = str(row["a_asset_id"] or "").strip()
        market_hash_name = str(row["market_hash_name"] or "").strip()
        if not asset_id or not market_hash_name:
            raise RuntimeError("trade missing A asset or market_hash_name")
        protected_reason = _profit_trade_protection_reason(
            config,
            asset_id=asset_id,
            market_hash_name=market_hash_name,
            steam_id=str(row["a_steam_id"] or _read_note(row["note"]).get("steamId") or "").strip(),
        )
        if protected_reason is None:
            protected_reason = _profit_trade_type_block_reason(config, market_hash_name)
        if protected_reason is not None:
            raise RuntimeError(f"profitTrade protected asset: {protected_reason}")
        c5_risk_reason = _trade_c5_risk_block_reason(config, row)
        if c5_risk_reason is not None:
            raise RuntimeError(c5_risk_reason)

        reservation = db.get_active_asset_reservation(asset_id)
        if (
            reservation is None
            or str(reservation["owner"] or "") != PROFIT_TRADE_OWNER
            or str(reservation["status"] or "") != "active"
        ):
            reason = "A asset reservation is not active before Steam buy"
            _cancel_pre_steam_buy_trade(
                db,
                row,
                reason=reason,
                source="profit_trade_buy_missing_reservation",
                extra_note={"reservationMissingAt": utc_now_iso()},
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

        note = _read_note(row["note"])
        a_steam_id = str(row["a_steam_id"] or note.get("steamId") or "").strip()
        selected_account: Account | None = None
        selected_wallet: dict[str, Any] = {}
        selected_wallet_balance: float | None = None
        client = steam_client or _build_steam_client(settings)
        try:
            orderbook_payload = client.order_book(
                app_id=settings.app_id,
                market_hash_name=market_hash_name,
            )
        except SteamMarketError as exc:
            raise RuntimeError(f"Steam orderbook failed: {exc}") from exc

        orderbook_buy_target = _pick_lowest_steam_orderbook_buy_target(orderbook_payload)
        if orderbook_buy_target is None:
            raise RuntimeError("Steam orderbook returned no buyable sell order")

        if steam_client is None:
            selection = _select_steam_buy_account(
                settings,
                required_balance=orderbook_buy_target.total_price,
                preferred_steam_id=a_steam_id,
            )
            selected_account = selection.account
            selected_wallet = selection.wallet
            selected_wallet_balance = selection.wallet_balance
            client = selection.client
            try:
                orderbook_payload = client.order_book(
                    app_id=settings.app_id,
                    market_hash_name=market_hash_name,
                )
            except SteamMarketError as exc:
                raise RuntimeError(f"Steam orderbook failed for selected account: {exc}") from exc
            orderbook_buy_target = _pick_lowest_steam_orderbook_buy_target(orderbook_payload)
            if orderbook_buy_target is None:
                raise RuntimeError("Steam orderbook returned no buyable sell order for selected account")
            if (
                selected_wallet_balance is not None
                and selected_wallet_balance + 1e-9 < orderbook_buy_target.total_price
            ):
                selection = _select_steam_buy_account(
                    settings,
                    required_balance=orderbook_buy_target.total_price,
                    preferred_steam_id=a_steam_id,
                )
                selected_account = selection.account
                selected_wallet = selection.wallet
                selected_wallet_balance = selection.wallet_balance
                client = selection.client

        max_price_tolerance = 1.0 + max(
            0.0,
            float(config.profit_trade_steam_buy_price_tolerance_pct),
        ) / 100.0
        failed_listing_ids: set[str] = set(_active_failed_steam_buy_listing_ids(market_hash_name))
        stale_listing_attempts: list[dict[str, Any]] = []
        listing_retry_budget = max(1, int(STEAM_BUY_LISTING_RETRY_ATTEMPTS))
        buy_order_retry_budget = max(1, int(STEAM_BUY_LISTING_RETRY_ATTEMPTS))
        unverified_buy_order_attempts: list[dict[str, Any]] = []
        refresh_market_snapshot = False

        while True:
            if refresh_market_snapshot:
                try:
                    orderbook_payload = client.order_book(
                        app_id=settings.app_id,
                        market_hash_name=market_hash_name,
                    )
                except SteamMarketError as exc:
                    raise RuntimeError(f"Steam orderbook refresh failed before buy: {exc}") from exc
                orderbook_buy_target = _pick_lowest_steam_orderbook_buy_target(orderbook_payload)
                if orderbook_buy_target is None:
                    raise RuntimeError("Steam orderbook returned no buyable sell order before buy")
            refresh_market_snapshot = True

            try:
                listings_payload = client.search_listings(
                    app_id=settings.app_id,
                    market_hash_name=market_hash_name,
                    start=0,
                    count=10,
                    currency=config.steam_currency,
                    country=config.steam_country,
                    language=config.steam_language,
                )
            except SteamMarketError as exc:
                raise RuntimeError(f"Steam listings search failed: {exc}") from exc
            use_buy_order = _steam_market_should_use_buy_order(market_hash_name)
            buy_method = "createbuyorder" if use_buy_order else "buylisting"
            buy_target = None if use_buy_order else _pick_lowest_steam_listing_buy_target(
                listings_payload,
                market_hash_name=market_hash_name,
                currency=config.steam_currency,
                excluded_listing_ids=failed_listing_ids,
            )
            if buy_target is None:
                # Commodity items should use Steam's buy-order flow even when the new
                # market route exposes concrete listing ids; buylisting can return a
                # stale/removed-listing error for those pseudo rows. We still only
                # advance after proving the order filled.
                buy_method = "createbuyorder"
                buy_target = orderbook_buy_target

            if (
                selected_wallet_balance is not None
                and selected_wallet_balance + 1e-9 < buy_target.total_price
                and steam_client is None
            ):
                selection = _select_steam_buy_account(
                    settings,
                    required_balance=buy_target.total_price,
                    preferred_steam_id=a_steam_id,
                )
                selected_account = selection.account
                selected_wallet = selection.wallet
                selected_wallet_balance = selection.wallet_balance
                client = selection.client
                try:
                    orderbook_payload = client.order_book(
                        app_id=settings.app_id,
                        market_hash_name=market_hash_name,
                    )
                    listings_payload = client.search_listings(
                        app_id=settings.app_id,
                        market_hash_name=market_hash_name,
                        start=0,
                        count=10,
                        currency=config.steam_currency,
                        country=config.steam_country,
                        language=config.steam_language,
                    )
                except SteamMarketError as exc:
                    raise RuntimeError(f"Steam buy market refresh failed for selected account: {exc}") from exc
                orderbook_buy_target = _pick_lowest_steam_orderbook_buy_target(orderbook_payload)
                use_buy_order = _steam_market_should_use_buy_order(market_hash_name)
                buy_target = None if use_buy_order else _pick_lowest_steam_listing_buy_target(
                    listings_payload,
                    market_hash_name=market_hash_name,
                    currency=config.steam_currency,
                    excluded_listing_ids=failed_listing_ids,
                )
                buy_method = "createbuyorder" if use_buy_order else "buylisting"
                if buy_target is None:
                    buy_method = "createbuyorder"
                    buy_target = orderbook_buy_target
                if orderbook_buy_target is None or buy_target is None:
                    raise RuntimeError("Steam buy market refresh returned no buyable listing")

            current_c5_risk_reason = _trade_c5_risk_block_reason(config, row)
            if current_c5_risk_reason is not None:
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason=current_c5_risk_reason,
                    source="profit_trade_buy_c5_risk_guard",
                    extra_note={
                        "steamBuyGuardAt": utc_now_iso(),
                        "steamBuyMethod": buy_method,
                        "failedSteamListingIds": sorted(failed_listing_ids),
                        "staleSteamListingAttempts": stale_listing_attempts,
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            steam_buy_price = buy_target.total_price
            original_steam_buy_price = safe_float(row["steam_buy_price"])
            if (
                orderbook_buy_target is not None
                and orderbook_buy_target.total_price > 0
                and steam_buy_price > orderbook_buy_target.total_price * max_price_tolerance + 1e-9
            ):
                reason = (
                    f"Steam listing price moved too much above orderbook before buy: "
                    f"{steam_buy_price:.2f} > {orderbook_buy_target.total_price:.2f} * {max_price_tolerance:.4f}"
                )
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason=reason,
                    source="profit_trade_buy_listing_price_guard",
                    extra_note={
                        "steamBuyGuardAt": utc_now_iso(),
                        "steamBuyMethod": buy_method,
                        "currentOrderbookPrice": round(orderbook_buy_target.total_price, 2),
                        "currentListingPrice": round(steam_buy_price, 2),
                        "steamBuyPriceTolerancePct": config.profit_trade_steam_buy_price_tolerance_pct,
                        "steamAccountId": selected_account.id if selected_account else getattr(client, "account_id", None),
                        "steamAccountName": selected_account.name if selected_account else None,
                        "walletBalanceBefore": selected_wallet_balance,
                        "failedSteamListingIds": sorted(failed_listing_ids),
                        "staleSteamListingAttempts": stale_listing_attempts,
                    },
                    update_fields={
                        "steam_listing_id": buy_target.listing_id,
                        "steam_buy_price": steam_buy_price,
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            if (
                original_steam_buy_price is not None
                and original_steam_buy_price > 0
                and steam_buy_price > original_steam_buy_price * max_price_tolerance + 1e-9
            ):
                reason = (
                    f"Steam buy price moved too much before buy: "
                    f"{steam_buy_price:.2f} > {original_steam_buy_price:.2f} * {max_price_tolerance:.4f}"
                )
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason=reason,
                    source="profit_trade_buy_price_guard",
                    extra_note={
                        "steamBuyGuardAt": utc_now_iso(),
                        "steamBuyMethod": buy_method,
                        "originalSteamBuyPrice": round(original_steam_buy_price, 2),
                        "currentSteamBuyPrice": round(steam_buy_price, 2),
                        "steamBuyPriceTolerancePct": config.profit_trade_steam_buy_price_tolerance_pct,
                        "steamAccountId": selected_account.id if selected_account else getattr(client, "account_id", None),
                        "steamAccountName": selected_account.name if selected_account else None,
                        "walletBalanceBefore": selected_wallet_balance,
                        "failedSteamListingIds": sorted(failed_listing_ids),
                        "staleSteamListingAttempts": stale_listing_attempts,
                    },
                    update_fields={
                        "steam_listing_id": buy_target.listing_id,
                        "steam_buy_price": steam_buy_price,
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

            steam_cost_ratio = _profit_trade_steam_cost_ratio(config)
            steam_real_cost = steam_buy_price * steam_cost_ratio
            c5_expected_net = safe_float(row["c5_expected_net_price"])
            c5_listing_price = safe_float(row["c5_listing_price"])
            if c5_expected_net is None and c5_listing_price is not None:
                c5_expected_net = c5_listing_price * float(config.profit_trade_c5_current_sale_net_factor)
            if c5_expected_net is None or c5_expected_net <= 0 or steam_real_cost <= 0:
                raise RuntimeError("trade missing usable C5 expected net price or Steam cost")

            expected_profit = c5_expected_net - steam_real_cost
            expected_roi = _profit_trade_transfer_roi(
                c5_expected_net=c5_expected_net,
                steam_buy_price=steam_buy_price,
                steam_cost_ratio=steam_cost_ratio,
            )
            if expected_roi is None:
                raise RuntimeError("trade missing usable Steam buy price for ROI")
            if (
                float(config.profit_trade_manual_review_roi) > 0
                and expected_roi > float(config.profit_trade_manual_review_roi)
            ):
                reason = (
                    f"ROI exceeds manual review threshold before Steam buy: "
                    f"{expected_roi * 100:.2f}% > {config.profit_trade_manual_review_roi * 100:.2f}%"
                )
                if asset_id:
                    db.release_asset_reservation(
                        asset_id=asset_id,
                        owner=PROFIT_TRADE_OWNER,
                        reason=_build_note(
                            {
                                "source": "profit_trade_buy_manual_review",
                                "tradeId": trade_id,
                                "reason": reason,
                            }
                        ),
                    )
                try:
                    _send_profit_trade_listing_alert(
                        settings,
                        title="搬砖做T买入前异常收益需人工确认",
                        row=row,
                        body_lines=[
                            f"- Steam买入价: CNY {steam_buy_price:.2f}",
                            f"- C5预计到手: CNY {c5_expected_net:.2f}",
                            f"- ROI: {expected_roi * 100:.2f}%",
                            "- 程序已停止买入，请检查价格源。",
                        ],
                    )
                except Exception:
                    pass
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    step_key="audited",
                    step_index=1,
                    error=reason,
                    steam_buy_price=steam_buy_price,
                    steam_balance_discount=float(steam_cost_ratio),
                    steam_real_cost=steam_real_cost,
                    expected_profit=expected_profit,
                    expected_roi=expected_roi,
                    note=_build_note(
                        {
                            **note,
                            "manualReviewAt": utc_now_iso(),
                            "manualReviewReason": reason,
                            "steamBuyGuardAt": utc_now_iso(),
                            "steamBuyMethod": buy_method,
                            "failedSteamListingIds": sorted(failed_listing_ids),
                            "staleSteamListingAttempts": stale_listing_attempts,
                        }
                    ),
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            if expected_roi < float(config.profit_trade_min_roi):
                reason = (
                    f"ROI no longer meets threshold before Steam buy: "
                    f"{expected_roi * 100:.2f}% < {config.profit_trade_min_roi * 100:.2f}%"
                )
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason=reason,
                    source="profit_trade_buy_roi_guard",
                    extra_note={
                        "steamBuyGuardAt": utc_now_iso(),
                        "steamBuyMethod": buy_method,
                        "steamBuyPrice": round(steam_buy_price, 2),
                        "steamCostRatio": round(steam_cost_ratio, 4),
                        "steamAccountId": selected_account.id if selected_account else getattr(client, "account_id", None),
                        "steamAccountName": selected_account.name if selected_account else None,
                        "walletBalanceBefore": selected_wallet_balance,
                        "steamRealCost": round(steam_real_cost, 2),
                        "expectedRoi": round(expected_roi, 4),
                        "failedSteamListingIds": sorted(failed_listing_ids),
                        "staleSteamListingAttempts": stale_listing_attempts,
                    },
                    update_fields={
                        "steam_listing_id": None,
                        "steam_buy_price": steam_buy_price,
                        "steam_balance_discount": float(steam_cost_ratio),
                        "steam_real_cost": steam_real_cost,
                        "expected_profit": expected_profit,
                        "expected_roi": expected_roi,
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

            before_asset_ids: list[str] = []
            steam_id64 = str(getattr(client, "steam_id64", "") or "").strip()
            if steam_id64:
                before_asset_ids = db.list_asset_ids(market_hash_name, steam_id=steam_id64)

            wallet_before_for_buy = selected_wallet_balance
            wallet_before_payload = selected_wallet or None
            try:
                refreshed_wallet_before = client.wallet_balance()
                refreshed_balance = safe_float(refreshed_wallet_before.get("balance"))
                if refreshed_balance is not None:
                    wallet_before_for_buy = refreshed_balance
                    wallet_before_payload = refreshed_wallet_before
            except Exception:
                pass

            active_buy_orders_before: list[dict[str, Any]] = []
            if buy_method == "createbuyorder" and hasattr(client, "my_listings"):
                try:
                    before_listings_payload = client.my_listings(start=0, count=100)
                    active_buy_orders_before = _matching_active_steam_buy_orders(
                        before_listings_payload if isinstance(before_listings_payload, dict) else {},
                        market_hash_name=market_hash_name,
                        buy_order_id=None,
                    )
                except Exception:
                    active_buy_orders_before = []

            db.update_profit_trade(trade_id, status="buying", step_key="steam_bought", step_index=3)
            try:
                if buy_method == "createbuyorder":
                    payload = client.create_buy_order(
                        app_id=settings.app_id,
                        market_hash_name=market_hash_name,
                        price_total=buy_target.total,
                        quantity=1,
                        currency=config.steam_currency,
                        country=config.steam_country,
                        return_uncertain_after_confirmation=True,
                    )
                else:
                    payload = client.buy_listing(
                        listing_id=buy_target.listing_id,
                        app_id=settings.app_id,
                        subtotal=buy_target.subtotal,
                        fee=buy_target.fee,
                        total=buy_target.total,
                        currency=config.steam_currency,
                        country=config.steam_country,
                        market_hash_name=market_hash_name,
                    )
            except SteamMarketError as exc:
                if (
                    buy_method == "buylisting"
                    and _steam_listing_already_purchased_error(exc)
                    and buy_target.listing_id
                    and listing_retry_budget > 1
                ):
                    failed_listing_ids.add(buy_target.listing_id)
                    _remember_failed_steam_buy_listing(market_hash_name, buy_target.listing_id)
                    stale_listing_attempts.append(
                        {
                            "listingId": buy_target.listing_id,
                            "steamBuyPrice": round(steam_buy_price, 2),
                            "failedAt": utc_now_iso(),
                            "error": str(exc),
                        }
                    )
                    listing_retry_budget -= 1
                    continue
                latest = db.get_profit_trade(trade_id) or row
                latest_note = _read_note(latest["note"])
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error=f"Steam buy failed or is uncertain: {exc}",
                    note=_build_note(
                        {
                            **latest_note,
                            "steamBuyFailedAt": utc_now_iso(),
                            "steamBuyMethod": buy_method,
                            "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                            "steamBuyPrice": round(steam_buy_price, 2),
                            "steamAccountId": selected_account.id if selected_account else getattr(client, "account_id", None),
                            "steamAccountName": selected_account.name if selected_account else None,
                            "walletBalanceBefore": wallet_before_for_buy,
                            "activeBuyOrdersBefore": active_buy_orders_before,
                            "failedSteamListingIds": sorted(failed_listing_ids),
                            "staleSteamListingAttempts": stale_listing_attempts,
                            "steamBuyListingSnapshot": _compact_steam_listing_snapshot(listings_payload, buy_target),
                            "steamBuyOrderbookSnapshot": _compact_steam_orderbook_snapshot(orderbook_payload),
                        }
                    ),
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            buy_order_id = str(
                (payload.get("buy_orderid") if isinstance(payload, dict) else None)
                or (payload.get("buy_order_id") if isinstance(payload, dict) else None)
                or ""
            ).strip()
            steam_buy_reference_id = buy_target.listing_id
            if buy_method == "createbuyorder" and buy_order_id:
                steam_buy_reference_id = buy_order_id

            verification = _verify_steam_buy_completed_with_inventory(
                client,
                settings,
                market_hash_name=market_hash_name,
                method=buy_method,
                expected_total=buy_target.total,
                wallet_before_balance=wallet_before_for_buy,
                buy_order_id=buy_order_id or None,
                before_asset_ids=before_asset_ids,
                steam_id=steam_id64,
                c5_client=c5_client,
            )
            if not verification.confirmed:
                latest = db.get_profit_trade(trade_id) or row
                latest_note = _read_note(latest["note"])
                reason = (
                    "Steam buy request succeeded but purchase completion is not verified"
                    if not verification.reason
                    else f"Steam buy request succeeded but purchase completion is not verified: {verification.reason}"
                )
                buy_order_cancel_payload: dict[str, Any] | None = None
                buy_order_cancel_error: str | None = None
                if buy_method == "createbuyorder" and buy_order_id:
                    if hasattr(client, "cancel_buy_order"):
                        try:
                            buy_order_cancel_payload = client.cancel_buy_order(buy_order_id=buy_order_id)
                        except Exception as exc:
                            buy_order_cancel_error = str(exc)
                    else:
                        buy_order_cancel_error = "Steam client cannot cancel buy orders"
                    unverified_buy_order_attempts.append(
                        {
                            "steamBuyOrderId": buy_order_id,
                            "steamBuyPrice": round(steam_buy_price, 2),
                            "unverifiedAt": utc_now_iso(),
                            "reason": verification.reason,
                            "cancelled": buy_order_cancel_error is None,
                            "cancelError": buy_order_cancel_error,
                            "cancelPayload": buy_order_cancel_payload,
                        }
                    )
                    if buy_order_cancel_error is None and buy_order_retry_budget > 1:
                        buy_order_retry_budget -= 1
                        refresh_market_snapshot = True
                        continue
                    if buy_order_cancel_error is None:
                        if asset_id:
                            db.release_asset_reservation(
                                asset_id=asset_id,
                                owner=PROFIT_TRADE_OWNER,
                                reason=_build_note(
                                    {
                                        "source": "profit_trade_buy_order_unverified_cancel",
                                        "tradeId": trade_id,
                                        "reason": reason,
                                        "steamBuyOrderId": buy_order_id,
                                    }
                                ),
                            )
                        db.update_profit_trade(
                            trade_id,
                            status="cancelled",
                            error=None,
                            steam_listing_id=steam_buy_reference_id,
                            steam_buy_price=steam_buy_price,
                            note=_build_note(
                                {
                                    **latest_note,
                                    "steamBuyUnverifiedAt": utc_now_iso(),
                                    "steamBuyMethod": buy_method,
                                    "steamListingId": None,
                                    "steamBuyOrderId": buy_order_id,
                                    "steamBuyPrice": round(steam_buy_price, 2),
                                    "walletBalanceBefore": wallet_before_for_buy,
                                    "walletBefore": wallet_before_payload,
                                    "walletAfter": verification.wallet_after,
                                    "walletDelta": verification.wallet_delta,
                                    "activeBuyOrdersBefore": active_buy_orders_before,
                                    "activeBuyOrdersAfter": verification.active_buy_orders,
                                    "steamBuyVerifiedBy": verification.verified_by,
                                    "beforeAssetIds": before_asset_ids,
                                    "inventoryAfterAssetIds": verification.inventory_after_asset_ids,
                                    "newInventoryAssetIds": verification.new_inventory_asset_ids,
                                    "steamBuyPayload": payload if isinstance(payload, dict) else None,
                                    "steamBuyOrderCancelledAt": utc_now_iso(),
                                    "steamBuyOrderCancelPayload": buy_order_cancel_payload,
                                    "unverifiedBuyOrderAttempts": unverified_buy_order_attempts,
                                    "failedSteamListingIds": sorted(failed_listing_ids),
                                    "staleSteamListingAttempts": stale_listing_attempts,
                                    "cancelReason": reason,
                                    "cancelSource": "profit_trade_buy_order_unverified_cancel",
                                    "steamBuyListingSnapshot": _compact_steam_listing_snapshot(listings_payload, buy_target),
                                    "steamBuyOrderbookSnapshot": _compact_steam_orderbook_snapshot(orderbook_payload),
                                }
                            ),
                        )
                        updated = db.get_profit_trade(trade_id)
                        return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error=reason,
                    steam_listing_id=steam_buy_reference_id,
                    steam_buy_price=steam_buy_price,
                    note=_build_note(
                        {
                            **latest_note,
                            "steamBuyUnverifiedAt": utc_now_iso(),
                            "steamBuyMethod": buy_method,
                            "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                            "steamBuyOrderId": buy_order_id or None,
                            "steamBuyPrice": round(steam_buy_price, 2),
                            "walletBalanceBefore": wallet_before_for_buy,
                            "walletBefore": wallet_before_payload,
                            "walletAfter": verification.wallet_after,
                            "walletDelta": verification.wallet_delta,
                            "activeBuyOrdersBefore": active_buy_orders_before,
                            "activeBuyOrdersAfter": verification.active_buy_orders,
                            "steamBuyVerifiedBy": verification.verified_by,
                            "beforeAssetIds": before_asset_ids,
                            "inventoryAfterAssetIds": verification.inventory_after_asset_ids,
                            "newInventoryAssetIds": verification.new_inventory_asset_ids,
                            "steamBuyPayload": payload if isinstance(payload, dict) else None,
                            "steamBuyOrderCancelError": buy_order_cancel_error,
                            "unverifiedBuyOrderAttempts": unverified_buy_order_attempts,
                            "failedSteamListingIds": sorted(failed_listing_ids),
                            "staleSteamListingAttempts": stale_listing_attempts,
                            "steamBuyListingSnapshot": _compact_steam_listing_snapshot(listings_payload, buy_target),
                            "steamBuyOrderbookSnapshot": _compact_steam_orderbook_snapshot(orderbook_payload),
                        }
                    ),
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            break

        hold_note = _build_note(
            {
                "source": "profit_trade_buy",
                "tradeId": trade_id,
                "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                "steamBuyOrderId": buy_order_id or None,
                "steamBuyMethod": buy_method,
                "lockedAfterSteamBuy": True,
            }
        )
        if not db.update_asset_reservation_deadline(
            asset_id=asset_id,
            owner=PROFIT_TRADE_OWNER,
            operation_id=trade_id,
            reserved_until=None,
            note=hold_note,
        ):
            latest = db.get_profit_trade(trade_id) or row
            latest_note = _read_note(latest["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                error="Steam buy succeeded but failed to extend A asset reservation",
                note=_build_note(
                    {
                        **latest_note,
                        "steamBuySucceededAt": utc_now_iso(),
                        "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                        "steamBuyOrderId": buy_order_id or None,
                        "walletInfo": payload.get("wallet_info") if isinstance(payload, dict) else None,
                    }
                ),
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

        db.update_profit_trade(
            trade_id,
            status="steam_bought",
            step_key="steam_bought",
            step_index=3,
            steam_listing_id=steam_buy_reference_id,
            steam_buy_price=steam_buy_price,
            steam_balance_discount=float(steam_cost_ratio),
            steam_real_cost=steam_real_cost,
            expected_profit=expected_profit,
            expected_roi=expected_roi,
            error=None,
            note=_build_note(
                {
                    **note,
                    "steamBuySucceededAt": utc_now_iso(),
                    "steamBuyMethod": buy_method,
                    "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                    "steamBuyOrderId": buy_order_id or None,
                    "subtotal": buy_target.subtotal,
                    "fee": buy_target.fee,
                    "total": buy_target.total,
                    "steamCostRatio": round(steam_cost_ratio, 4),
                    "steamId": steam_id64 or None,
                    "steamAccountId": selected_account.id if selected_account else getattr(client, "account_id", None),
                    "steamAccountName": selected_account.name if selected_account else None,
                    "walletBalanceBefore": wallet_before_for_buy,
                    "walletBefore": wallet_before_payload,
                    "walletAfter": verification.wallet_after,
                    "walletDelta": verification.wallet_delta,
                    "activeBuyOrdersBefore": active_buy_orders_before,
                    "activeBuyOrdersAfter": verification.active_buy_orders,
                    "steamBuyVerifiedBy": verification.verified_by,
                    "beforeAssetIds": before_asset_ids,
                    "inventoryAfterAssetIds": verification.inventory_after_asset_ids,
                    "newInventoryAssetIds": verification.new_inventory_asset_ids,
                    "walletInfo": payload.get("wallet_info") if isinstance(payload, dict) else None,
                    "failedSteamListingIds": sorted(failed_listing_ids),
                    "staleSteamListingAttempts": stale_listing_attempts,
                    "unverifiedBuyOrderAttempts": unverified_buy_order_attempts,
                    "steamBuyListingSnapshot": _compact_steam_listing_snapshot(listings_payload, buy_target),
                    "steamBuyOrderbookSnapshot": _compact_steam_orderbook_snapshot(orderbook_payload),
                }
            ),
        )
        updated = db.get_profit_trade(trade_id)
        return {"ok": True, "changed": True, "trade": _trade_row_to_dict(updated)}
    finally:
        db.close()


def execute_profit_trade_list_c5(
    settings: Settings,
    trade_id: int,
    *,
    config: StrategyConfig | None = None,
    c5_client: Any | None = None,
) -> dict[str, Any]:
    config = config or load_strategy_config(settings)
    _require_profit_trade_real_execution(config)
    db = Database(settings.db_path)
    try:
        db.initialize()
        row = db.get_profit_trade(trade_id)
        if row is None:
            raise RuntimeError(f"profit trade not found: {trade_id}")
        if str(row["status"]) not in {"steam_bought", "listing_c5"}:
            raise RuntimeError(f"trade status must be steam_bought before C5 listing: {row['status']}")

        asset_id = str(row["a_asset_id"] or "").strip()
        market_hash_name = str(row["market_hash_name"] or "").strip()
        note = _read_note(row["note"])
        token = str(note.get("token") or "").strip()
        style_token = str(note.get("styleToken") or note.get("style_token") or "").strip()
        sale_price = safe_float(row["c5_listing_price"]) or safe_float(note.get("c5ListingPrice"))
        if not asset_id or not market_hash_name:
            raise RuntimeError("trade missing A asset or market_hash_name")
        protected_reason = _profit_trade_protection_reason(
            config,
            asset_id=asset_id,
            market_hash_name=market_hash_name,
            steam_id=str(row["a_steam_id"] or note.get("steamId") or "").strip(),
        )
        if protected_reason is None:
            protected_reason = _profit_trade_type_block_reason(config, market_hash_name)
        if protected_reason is not None:
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                error=f"protected asset before C5 listing: {protected_reason}",
                note=_build_note({**note, "protectedBeforeC5At": utc_now_iso()}),
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
        if not token or not style_token:
            raise RuntimeError("trade missing C5 token/styleToken for A asset")
        if sale_price is None or sale_price <= 0:
            raise RuntimeError("trade missing usable C5 listing price")

        reservation = db.get_active_asset_reservation(asset_id)
        if (
            reservation is None
            or str(reservation["owner"] or "") != PROFIT_TRADE_OWNER
            or str(reservation["status"] or "") != "active"
        ):
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                error="A asset reservation is not active before C5 listing",
                note=_build_note({**note, "reservationMissingBeforeC5At": utc_now_iso()}),
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

        if c5_client is None:
            if not settings.c5_api_key:
                raise RuntimeError("missing C5GAME_API_KEY / C5_API_KEY")
            c5_client = C5GameClient(str(settings.c5_api_key), settings.c5_base_url)

        db.update_profit_trade(trade_id, status="listing_c5", step_key="c5_listed", step_index=4)
        try:
            payload = c5_client.sale_create(
                app_id=settings.app_id,
                items=[
                    {
                        "assetId": asset_id,
                        "marketHashName": market_hash_name,
                        "price": sale_price,
                        "token": token,
                        "styleToken": style_token,
                    }
                ],
            )
        except C5GameError as exc:
            latest = db.get_profit_trade(trade_id) or row
            latest_note = _read_note(latest["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                error=f"C5 listing failed or is uncertain: {exc}",
                note=_build_note({**latest_note, "c5ListingFailedAt": utc_now_iso()}),
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

        product_id = _extract_c5_sale_id(payload if isinstance(payload, dict) else {})
        consume_note = _build_note(
            {
                "source": "profit_trade_c5_listing",
                "tradeId": trade_id,
                "productId": product_id,
                "c5SalePrice": round(float(sale_price), 2),
                "consumedAfterC5Listing": True,
            }
        )
        consumed = db.consume_asset_reservation(
            asset_id=asset_id,
            owner=PROFIT_TRADE_OWNER,
            operation_id=trade_id,
            note=consume_note,
        )
        merged_note = {
            **note,
            "c5ListedAt": utc_now_iso(),
            "c5ProductId": product_id,
            "c5SalePrice": round(float(sale_price), 2),
            "c5Raw": payload,
        }
        if not consumed:
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                error="C5 listing succeeded but failed to consume A asset reservation",
                c5_product_id=product_id,
                note=_build_note(merged_note),
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

        db.update_profit_trade(
            trade_id,
            status="c5_listed",
            step_key="c5_listed",
            step_index=4,
            c5_product_id=product_id,
            error=None,
            note=_build_note(merged_note),
        )
        updated = db.get_profit_trade(trade_id)
        return {"ok": True, "changed": True, "trade": _trade_row_to_dict(updated)}
    finally:
        db.close()


def refresh_profit_trade_sales(
    settings: Settings,
    config: StrategyConfig | None = None,
    *,
    c5_client: Any | None = None,
) -> dict[str, Any]:
    config = config or load_strategy_config(settings)
    db = Database(settings.db_path)
    try:
        db.initialize()
        rows = db.list_profit_trades(status="c5_listed", limit=500)
    finally:
        db.close()
    if not rows:
        return {
            "ok": True,
            "settledTradeIds": [],
            "skippedTradeIds": [],
            "errors": [],
        }

    if c5_client is None:
        if not settings.c5_api_key:
            raise RuntimeError("missing C5GAME_API_KEY / C5_API_KEY")
        c5_client = C5GameClient(str(settings.c5_api_key), settings.c5_base_url)

    steam_ids = sorted(
        {
            str(row["a_steam_id"] or _read_note(row["note"]).get("steamId") or "").strip()
            for row in rows
            if str(row["a_steam_id"] or _read_note(row["note"]).get("steamId") or "").strip()
        }
    )
    seller_order_lookup = _load_c5_seller_sold_order_lookup(c5_client, settings, rows)
    try:
        active_lookup = _load_active_c5_sale_lookup(c5_client, settings, steam_ids=steam_ids)
    except Exception as exc:
        active_lookup = ActiveC5SaleLookup(active_ids=set(), covered_steam_ids=set(), errors=[f"C5 active sale lookup failed: {exc}"])
    db = Database(settings.db_path)
    settled_ids: list[int] = []
    skipped_ids: list[int] = []
    errors: list[str] = list(seller_order_lookup.errors or []) + list(active_lookup.errors or [])
    try:
        db.initialize()
        now = datetime.now(timezone.utc)
        for row in rows:
            trade_id = int(row["id"])
            note = _read_note(row["note"])
            product_id = str(row["c5_product_id"] or note.get("c5ProductId") or "").strip()
            steam_id = str(row["a_steam_id"] or note.get("steamId") or "").strip()
            if not product_id:
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error="C5 listed trade missing product id",
                    note=_build_note({**note, "missingProductIdAt": utc_now_iso()}),
                )
                errors.append(f"trade {trade_id} missing product id")
                continue

            sold_order = seller_order_lookup.sold_orders_by_product_id.get(product_id)
            if sold_order is not None:
                c5_sold_net, source = _c5_seller_order_sold_net_price(sold_order, config=config)
                steam_real_cost = safe_float(row["steam_real_cost"])
                steam_buy_price = safe_float(row["steam_buy_price"]) or safe_float(note.get("steamBuyPrice"))
                steam_cost_ratio = safe_float(row["steam_balance_discount"]) or _profit_trade_steam_cost_ratio(config)
                if c5_sold_net is None or steam_real_cost is None or steam_real_cost <= 0:
                    db.update_profit_trade(
                        trade_id,
                        status="manual_required",
                        error="missing C5 seller order net or Steam real cost for settlement",
                        note=_build_note({**note, "settlementBlockedAt": utc_now_iso(), "c5SellerOrder": sold_order}),
                    )
                    errors.append(f"trade {trade_id} missing settlement prices")
                    continue
                if steam_buy_price is None:
                    db.update_profit_trade(
                        trade_id,
                        status="manual_required",
                        error="missing Steam buy price for realized ROI",
                        note=_build_note({**note, "settlementBlockedAt": utc_now_iso(), "c5SellerOrder": sold_order}),
                    )
                    errors.append(f"trade {trade_id} missing Steam buy price")
                    continue
                realized_profit = c5_sold_net - steam_real_cost
                realized_roi = _profit_trade_transfer_roi(
                    c5_expected_net=c5_sold_net,
                    steam_buy_price=steam_buy_price,
                    steam_cost_ratio=steam_cost_ratio,
                )
                if realized_roi is None:
                    db.update_profit_trade(
                        trade_id,
                        status="manual_required",
                        error="missing Steam buy price for realized ROI",
                        note=_build_note({**note, "settlementBlockedAt": utc_now_iso(), "c5SellerOrder": sold_order}),
                    )
                    errors.append(f"trade {trade_id} missing Steam buy price")
                    continue
                db.update_profit_trade(
                    trade_id,
                    status="completed",
                    step_key="settled",
                    step_index=6,
                    c5_sold_net_price=c5_sold_net,
                    realized_profit=realized_profit,
                    realized_roi=realized_roi,
                    error=None,
                    note=_build_note(
                        {
                            **note,
                            "c5SoldDetectedAt": utc_now_iso(),
                            "c5SoldNetPriceSource": source,
                            "c5SellerOrderId": _c5_seller_order_id(sold_order),
                            "c5SellerOrderStatus": _c5_seller_order_status(sold_order),
                            "c5SellerOrderStatusName": sold_order.get("statusName") or sold_order.get("status_name"),
                            "c5SellerOrderProductId": _c5_seller_order_product_id(sold_order),
                            "c5SellerOrder": sold_order,
                        }
                    ),
                )
                settled_ids.append(trade_id)
                continue

            if product_id in active_lookup.active_ids:
                skipped_ids.append(trade_id)
                continue
            listed_at = _parse_iso(str(_read_note(row["note"]).get("c5ListedAt") or row["updated_at"] or ""))
            if listed_at is None:
                listed_at = _parse_iso(str(row["updated_at"] or ""))
            if listed_at is not None:
                if listed_at.tzinfo is None:
                    listed_at = listed_at.replace(tzinfo=timezone.utc)
                wait_seconds = max(0.0, float(config.listing_check_interval_minutes) * 60.0)
                if (now - listed_at.astimezone(timezone.utc)).total_seconds() < wait_seconds:
                    skipped_ids.append(trade_id)
                    continue

            if seller_order_lookup.covers(steam_id) and active_lookup.covers(steam_id):
                reason = "C5 listed product is no longer active, but no matching seller sold order was found"
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error=reason,
                    note=_build_note(
                        {
                            **note,
                            "settlementBlockedAt": utc_now_iso(),
                            "settlementBlockedReason": reason,
                            "missingSellerOrderProductId": product_id,
                            "activeSaleMissingProductId": product_id,
                        }
                    ),
                )
                errors.append(f"trade {trade_id}: {reason}")
                continue
            if seller_order_lookup.covers(steam_id) and not active_lookup.covers(steam_id):
                skipped_ids.append(trade_id)
                errors.append(f"trade {trade_id}: active C5 sale lookup did not cover steamId {steam_id}")
                continue

            c5_listing_price = safe_float(row["c5_listing_price"])
            c5_sold_net = safe_float(row["c5_sold_net_price"])
            source = "c5_sold_net_price"
            if c5_sold_net is None and c5_listing_price is not None:
                c5_sold_net = c5_listing_price * float(config.profit_trade_c5_current_sale_net_factor)
                source = "estimated_from_listing_price"
            steam_real_cost = safe_float(row["steam_real_cost"])
            if c5_sold_net is None or steam_real_cost is None or steam_real_cost <= 0:
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error="missing C5 sold net or Steam real cost for settlement",
                    note=_build_note({**_read_note(row["note"]), "settlementBlockedAt": utc_now_iso()}),
                )
                errors.append(f"trade {trade_id} missing settlement prices")
                continue
            realized_profit = c5_sold_net - steam_real_cost
            steam_buy_price = safe_float(row["steam_buy_price"]) or safe_float(_read_note(row["note"]).get("steamBuyPrice"))
            steam_cost_ratio = safe_float(row["steam_balance_discount"]) or _profit_trade_steam_cost_ratio(config)
            realized_roi = (
                _profit_trade_transfer_roi(
                    c5_expected_net=c5_sold_net,
                    steam_buy_price=steam_buy_price,
                    steam_cost_ratio=steam_cost_ratio,
                )
                if steam_buy_price is not None
                else None
            )
            if realized_roi is None:
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error="missing Steam buy price for realized ROI",
                    note=_build_note({**_read_note(row["note"]), "settlementBlockedAt": utc_now_iso()}),
                )
                errors.append(f"trade {trade_id} missing Steam buy price")
                continue
            note = _read_note(row["note"])
            db.update_profit_trade(
                trade_id,
                status="completed",
                step_key="settled",
                step_index=6,
                c5_sold_net_price=c5_sold_net,
                realized_profit=realized_profit,
                realized_roi=realized_roi,
                error=None,
                note=_build_note(
                    {
                        **note,
                        "c5SoldDetectedAt": utc_now_iso(),
                        "c5SoldNetPriceSource": source,
                        "activeSaleMissingProductId": product_id,
                    }
                ),
            )
            settled_ids.append(trade_id)
    finally:
        db.close()
    return {
        "ok": True,
        "settledTradeIds": settled_ids,
        "skippedTradeIds": skipped_ids,
        "errors": errors,
    }



def _manual_trade_recoverable_steam_buy_reason(row: Any) -> str | None:
    status = str(row["status"] or "").strip()
    if status != "manual_required":
        return None
    note = _read_note(row["note"])
    if str(note.get("steamBuyMethod") or "").strip() != "createbuyorder":
        return None
    error = str(row["error"] or "")
    if "purchase completion is not verified" not in error and not note.get("steamBuyUnverifiedAt"):
        return None
    if str(row["b_asset_id"] or "").strip() or str(row["c5_product_id"] or "").strip():
        return None
    wallet_delta = safe_float(note.get("walletDelta"))
    steam_buy_price = safe_float(row["steam_buy_price"])
    verified_by = [str(value) for value in (note.get("steamBuyVerifiedBy") or [])]
    if "wallet_balance_delta" not in verified_by:
        if wallet_delta is None or steam_buy_price is None or wallet_delta + 0.02 < steam_buy_price:
            return None
    return "wallet delta plus local inventory proves Steam buy completed"


def recover_unverified_profit_trade_steam_buys(
    settings: Settings,
    *,
    config: StrategyConfig | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    config = config or load_strategy_config(settings)
    recovered_ids: list[int] = []
    skipped_ids: list[int] = []
    errors: list[str] = []
    db = Database(settings.db_path)
    try:
        db.initialize()
        for row in db.list_profit_trades(status="manual_required", limit=limit):
            trade_id = int(row["id"])
            reason = _manual_trade_recoverable_steam_buy_reason(row)
            if reason is None:
                continue
            asset_id = str(row["a_asset_id"] or "").strip()
            market_hash_name = str(row["market_hash_name"] or "").strip()
            note = _read_note(row["note"])
            steam_id = str(row["a_steam_id"] or note.get("steamId") or "").strip()
            if not asset_id or not market_hash_name or not steam_id:
                skipped_ids.append(trade_id)
                errors.append(f"recover-buy {trade_id}: missing A asset, market_hash_name, or Steam id")
                continue
            protected_reason = _profit_trade_protection_reason(
                config,
                asset_id=asset_id,
                market_hash_name=market_hash_name,
                steam_id=steam_id,
            )
            if protected_reason is None:
                protected_reason = _profit_trade_type_block_reason(config, market_hash_name)
            if protected_reason is not None:
                skipped_ids.append(trade_id)
                errors.append(f"recover-buy {trade_id}: protected asset: {protected_reason}")
                continue
            active_reservation = db.get_active_asset_reservation(asset_id)
            if active_reservation is not None:
                reservation_status = str(active_reservation["status"] or "").strip()
                reservation_owner = str(active_reservation["owner"] or "").strip()
                reservation_operation = active_reservation["operation_id"]
                if reservation_status != "active" or reservation_owner != PROFIT_TRADE_OWNER:
                    skipped_ids.append(trade_id)
                    errors.append(f"recover-buy {trade_id}: A asset has incompatible reservation")
                    continue
                if reservation_operation is not None and int(reservation_operation) != trade_id:
                    skipped_ids.append(trade_id)
                    errors.append(f"recover-buy {trade_id}: A asset is reserved by another profit trade")
                    continue
            inventory_asset_rows = db.list_assets(
                market_hash_name=market_hash_name,
                steam_id=steam_id,
            )
            inventory_after_asset_ids = [str(asset["asset_id"]) for asset in inventory_asset_rows]
            before_asset_ids = [str(value) for value in (note.get("beforeAssetIds") or []) if str(value or "").strip()]
            if before_asset_ids:
                before_set = set(before_asset_ids)
                candidates = [asset_id_value for asset_id_value in inventory_after_asset_ids if asset_id_value not in before_set]
            else:
                candidates = [asset_id_value for asset_id_value in inventory_after_asset_ids if asset_id_value != asset_id]
            candidates = sorted(set(candidates))
            if len(candidates) != 1:
                skipped_ids.append(trade_id)
                errors.append(
                    f"recover-buy {trade_id}: expected exactly one B candidate, got {len(candidates)}"
                )
                continue
            b_asset_id = candidates[0]
            if active_reservation is None:
                reservation_id = db.reserve_asset(
                    asset_id=asset_id,
                    market_hash_name=market_hash_name,
                    owner=PROFIT_TRADE_OWNER,
                    purpose="sell_existing_a",
                    reserved_until=None,
                    operation_id=trade_id,
                    note=_build_note(
                        {
                            "source": "profit_trade_recover_unverified_buy",
                            "tradeId": trade_id,
                            "reason": reason,
                            "recoveredAt": utc_now_iso(),
                        }
                    ),
                )
                if reservation_id is None:
                    skipped_ids.append(trade_id)
                    errors.append(f"recover-buy {trade_id}: failed to reserve A asset")
                    continue
            db.update_profit_trade(
                trade_id,
                status="steam_bought",
                step_key="steam_bought",
                step_index=3,
                b_asset_id=b_asset_id,
                error=None,
                note=_build_note(
                    {
                        **note,
                        "steamBuyRecoveredAt": utc_now_iso(),
                        "steamBuyRecoveredBy": "local_inventory_reconciliation",
                        "steamBuyRecoverReason": reason,
                        "beforeAssetIds": before_asset_ids or [asset_id],
                        "inventoryAfterAssetIds": inventory_after_asset_ids,
                        "newInventoryAssetIds": candidates,
                        "steamBuyVerifiedBy": sorted(set([
                            *[str(value) for value in (note.get("steamBuyVerifiedBy") or [])],
                            "local_inventory_reconciliation",
                        ])),
                    }
                ),
            )
            db.conn.execute(
                "UPDATE profit_trades SET completed_at = NULL WHERE id = ?",
                (trade_id,),
            )
            db.conn.commit()
            recovered_ids.append(trade_id)
    finally:
        db.close()
    return {"ok": True, "recoveredTradeIds": recovered_ids, "skippedTradeIds": skipped_ids, "errors": errors}
def dismiss_profit_trade(
    settings: Settings,
    trade_id: int,
    *,
    reason: str = "user dismissed manual review trade",
) -> dict[str, Any]:
    db = Database(settings.db_path)
    try:
        db.initialize()
        row = db.get_profit_trade(trade_id)
        if row is None:
            raise RuntimeError(f"profit trade not found: {trade_id}")
        status = str(row["status"] or "").strip()
        if status == "completed":
            raise RuntimeError("completed profit trade cannot be dismissed")
        if status in {"steam_bought", "listing_c5", "c5_listed"}:
            raise RuntimeError(f"profit trade with live follow-up state cannot be dismissed: {status}")
        asset_id = str(row["a_asset_id"] or "").strip()
        if asset_id:
            db.release_asset_reservation(
                asset_id=asset_id,
                owner=PROFIT_TRADE_OWNER,
                reason=_build_note(
                    {
                        "source": "profit_trade_dismiss",
                        "tradeId": trade_id,
                        "reason": reason,
                    }
                ),
            )
        note = _read_note(row["note"])
        db.update_profit_trade(
            trade_id,
            status="cancelled",
            error=str(reason or "dismissed"),
            note=_build_note(
                {
                    **note,
                    "dismissedAt": utc_now_iso(),
                    "dismissedReason": reason,
                    "cancelSource": "profit_trade_dismiss",
                }
            ),
        )
        updated = db.get_profit_trade(trade_id)
        return {"ok": True, "changed": True, "trade": _trade_row_to_dict(updated)}
    finally:
        db.close()


def manual_settle_profit_trade(
    settings: Settings,
    trade_id: int,
    *,
    sold_net_price: float,
    source: str = "manual_other_platform",
    memo: str | None = None,
) -> dict[str, Any]:
    if sold_net_price <= 0:
        raise ValueError("sold_net_price must be positive")
    config = load_strategy_config(settings)
    db = Database(settings.db_path)
    try:
        db.initialize()
        row = db.get_profit_trade(trade_id)
        if row is None:
            raise RuntimeError(f"profit trade not found: {trade_id}")
        steam_buy_price = safe_float(row["steam_buy_price"]) or safe_float(_read_note(row["note"]).get("steamBuyPrice"))
        if steam_buy_price is None or steam_buy_price <= 0:
            raise RuntimeError("trade missing Steam buy price")
        steam_cost_ratio = safe_float(row["steam_balance_discount"]) or _profit_trade_steam_cost_ratio(config)
        realized_profit, realized_roi = _realized_values(
            sold_net_price=float(sold_net_price),
            steam_buy_price=float(steam_buy_price),
            steam_cost_ratio=float(steam_cost_ratio),
        )
        note = _read_note(row["note"])
        db.update_profit_trade(
            trade_id,
            status="completed",
            step_key="settled",
            step_index=6,
            c5_sold_net_price=float(sold_net_price),
            realized_profit=realized_profit,
            realized_roi=realized_roi,
            error=None,
            note=_build_note(
                {
                    **note,
                    "manualSettlementAt": utc_now_iso(),
                    "manualSettlementSource": source,
                    "manualSettlementMemo": memo,
                    "manualSoldNetPrice": round(float(sold_net_price), 2),
                    "manualRealizedProfit": round(realized_profit, 2),
                    "manualRealizedRoi": round(realized_roi, 4),
                }
            ),
        )
        updated = db.get_profit_trade(trade_id)
        return {"ok": True, "trade": _trade_row_to_dict(updated), "changed": True}
    finally:
        db.close()


def refresh_profit_trade_listings(
    settings: Settings,
    config: StrategyConfig | None = None,
    *,
    c5_client: Any | None = None,
) -> dict[str, Any]:
    config = config or load_strategy_config(settings)
    _require_profit_trade_reprice_execution(config)
    if not config.profit_trade_reprice_enabled:
        return {"ok": True, "repricedTradeIds": [], "skippedTradeIds": [], "errors": ["profitTrade reprice is disabled"]}
    if not settings.c5_api_key and c5_client is None:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    c5_client = c5_client or C5GameClient(str(settings.c5_api_key), settings.c5_base_url)
    db = Database(settings.db_path)
    repriced_ids: list[int] = []
    skipped_ids: list[int] = []
    errors: list[str] = []
    try:
        db.initialize()
        rows = db.list_profit_trades(status="c5_listed", limit=500)
        market_hash_names = sorted(
            {
                str(row["market_hash_name"] or "").strip()
                for row in rows
                if str(row["market_hash_name"] or "").strip()
            }
        )
        statistics_by_name = (
            _fetch_c5_recent_sale_risks(
                c5_client,
                app_id=settings.app_id,
                market_hash_names=market_hash_names,
            )
            if market_hash_names
            else {}
        )
        for row in rows:
            trade_id = int(row["id"])
            market_hash_name = str(row["market_hash_name"] or "").strip()
            product_id = str(row["c5_product_id"] or _read_note(row["note"]).get("c5ProductId") or "").strip()
            current_price = safe_float(row["c5_listing_price"])
            steam_buy_price = safe_float(row["steam_buy_price"])
            steam_cost_ratio = safe_float(row["steam_balance_discount"]) or _profit_trade_steam_cost_ratio(config)
            if not market_hash_name or not product_id or current_price is None or steam_buy_price is None:
                skipped_ids.append(trade_id)
                continue
            note = _read_note(row["note"])
            protected_reason = _profit_trade_protection_reason(
                config,
                asset_id=str(row["a_asset_id"] or note.get("assetId") or "").strip(),
                market_hash_name=market_hash_name,
                steam_id=str(row["a_steam_id"] or note.get("steamId") or "").strip(),
            )
            if protected_reason is None:
                protected_reason = _profit_trade_type_block_reason(config, market_hash_name)
            if protected_reason is not None:
                skipped_ids.append(trade_id)
                db.update_profit_trade(
                    trade_id,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingRepriceDecision": "protected",
                            "listingRepriceBlockedReason": protected_reason,
                        }
                    ),
                )
                continue
            listed_age_hours = _profit_trade_stale_listing_age_hours(note)
            stale_reprice_after = max(0.0, float(config.profit_trade_stale_reprice_after_hours))
            stale_manual_after = max(0.0, float(config.profit_trade_stale_manual_review_after_hours))
            stale_reprice_mode = (
                listed_age_hours is not None
                and stale_reprice_after > 0
                and listed_age_hours >= stale_reprice_after
            )
            statistics_for_name = statistics_by_name.get(market_hash_name)
            try:
                depth = _fetch_c5_listing_depth(c5_client, settings, market_hash_name=market_hash_name)
            except Exception as exc:
                depth = _listing_depth_from_c5_statistics(statistics_for_name)
                if depth is None:
                    errors.append(f"listing-depth {trade_id}: {exc}")
                    continue
                depth["fallbackReason"] = str(exc)
            depth_ok, depth_reason = _evaluate_c5_orderbook_depth_risk(config, depth=depth)
            if not depth_ok:
                db.update_profit_trade(
                    trade_id,
                    error=depth_reason,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingRepriceDecision": "blocked_c5_listing_depth",
                            "listingRepriceBlockedReason": depth_reason,
                            "listingRiskAt": utc_now_iso(),
                            "listingRiskReason": depth_reason,
                            "listingDepth": depth,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue
            lowest = safe_float(depth.get("lowestPrice"))
            if (
                listed_age_hours is not None
                and stale_manual_after > 0
                and listed_age_hours >= stale_manual_after
            ):
                _send_profit_trade_listing_alert(
                    settings,
                    title="搬砖做T上架超过一天未售出",
                    row=row,
                    body_lines=[
                        f"- 已上架: {listed_age_hours:.1f} 小时",
                        f"- 当前挂价: CNY {current_price:.2f}",
                        f"- C5最低价: CNY {lowest:.2f}" if lowest is not None else "- C5最低价: -",
                        "- 程序已停止自动改价，请人工处理。",
                    ],
                )
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error="C5 listed for more than stale manual review hours",
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingDepth": depth,
                            "listingRepriceDecision": "stale_manual_review",
                            "listingRepriceBlockedReason": "listed too long without sale",
                            "staleListedAgeHours": round(listed_age_hours, 2),
                            "staleManualReviewAfterHours": stale_manual_after,
                            "staleManualReviewServerChanSent": bool(settings.serverchan_sendkey),
                            "staleManualReviewAt": utc_now_iso(),
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue
            if lowest is None or lowest >= current_price - 0.009:
                skipped_ids.append(trade_id)
                db.update_profit_trade(
                    trade_id,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingDepth": depth,
                            "listingRepriceDecision": "kept_current_price",
                        }
                    ),
                )
                continue
            cooldown_ok, cooldown_reason = _profit_trade_reprice_cooldown_passed(config, note)
            if not stale_reprice_mode and not cooldown_ok:
                skipped_ids.append(trade_id)
                db.update_profit_trade(
                    trade_id,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingDepth": depth,
                            "listingRepriceDecision": "cooldown",
                            "listingRepriceCooldownReason": cooldown_reason,
                        }
                    ),
                )
                continue
            if stale_reprice_mode:
                target_price = _profit_trade_stale_listing_price(
                    config,
                    current_lowest_price=lowest,
                    fallback_price=current_price,
                )
            else:
                target_price = _profit_trade_competitive_listing_price(
                    config,
                    current_lowest_price=lowest,
                    fallback_price=current_price,
                )
            market_stats = _merge_c5_listing_depth_and_statistics(
                depth=depth,
                statistics=statistics_for_name,
            )
            purchase_floor = safe_float(market_stats.purchase_max_price)
            if purchase_floor is not None and purchase_floor > 0 and target_price < purchase_floor:
                if purchase_floor >= current_price - 0.009:
                    skipped_ids.append(trade_id)
                    db.update_profit_trade(
                        trade_id,
                        note=_build_note(
                            {
                                **note,
                                "lastListingCheckAt": utc_now_iso(),
                                "listingDepth": depth,
                                "listingMarketStats": _c5_risk_note(market_stats),
                                "listingRepriceDecision": "kept_at_purchase_floor",
                                "listingRepriceBlockedReason": "purchase max price is at or above current listing price",
                                "purchaseFloorPrice": round(purchase_floor, 2),
                                "repriceTargetPrice": round(target_price, 2),
                            }
                        ),
                    )
                    continue
                target_price = purchase_floor
            evaluated_market_stats = market_stats
            if config.profit_trade_require_c5_market_depth:
                evaluated_market_stats = _evaluate_c5_market_depth_risk(
                    config,
                    c5_listing_price=target_price,
                    risk=market_stats,
                )
                if evaluated_market_stats.status != "passed":
                    db.update_profit_trade(
                        trade_id,
                        error=evaluated_market_stats.reason,
                        note=_build_note(
                            {
                                **note,
                                "lastListingCheckAt": utc_now_iso(),
                                "listingRepriceDecision": "blocked_c5_market_depth",
                                "listingRepriceBlockedReason": evaluated_market_stats.reason,
                                "listingRiskAt": utc_now_iso(),
                                "listingRiskReason": evaluated_market_stats.reason,
                                "listingDepth": depth,
                                "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                            }
                        ),
                    )
                    skipped_ids.append(trade_id)
                    continue
            target_net = target_price * float(config.profit_trade_c5_current_sale_net_factor)
            expected_profit, expected_roi = _realized_values(
                sold_net_price=target_net,
                steam_buy_price=steam_buy_price,
                steam_cost_ratio=float(steam_cost_ratio),
            )
            roi_floor = (
                max(0.0, float(config.profit_trade_stale_min_roi))
                if stale_reprice_mode
                else float(config.profit_trade_min_roi)
            )
            if expected_roi > float(config.profit_trade_manual_review_roi):
                _send_profit_trade_listing_alert(
                    settings,
                    title="搬砖做T改价异常收益需人工确认",
                    row=row,
                    body_lines=[
                        f"- 当前价: CNY {current_price:.2f}",
                        f"- C5最低价: CNY {lowest:.2f}",
                        f"- 目标价: CNY {target_price:.2f}",
                        f"- 目标ROI: {expected_roi * 100:.2f}%",
                        "- 程序已停止改价，请检查价格源。",
                    ],
                )
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error=f"reprice ROI {expected_roi * 100:.2f}% > manual review threshold",
                    note=_build_note(
                        {
                            **note,
                            "listingDepth": depth,
                            "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                            "repriceBlockedAt": utc_now_iso(),
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue
            if expected_roi < roi_floor:
                if stale_reprice_mode:
                    _send_profit_trade_listing_alert(
                        settings,
                        title="搬砖做T滞销改价低于4%收益",
                        row=row,
                        body_lines=[
                            f"- 已上架: {listed_age_hours:.1f} 小时" if listed_age_hours is not None else "- 已上架: -",
                            f"- 当前挂价: CNY {current_price:.2f}",
                            f"- C5最低价: CNY {lowest:.2f}",
                            f"- 目标底价: CNY {target_price:.2f}",
                            f"- 目标ROI: {expected_roi * 100:.2f}%",
                            f"- 最低允许ROI: {roi_floor * 100:.2f}%",
                            "- 程序已停止自动改价，请人工处理。",
                        ],
                    )
                    db.update_profit_trade(
                        trade_id,
                        status="manual_required",
                        error=(
                            f"stale reprice ROI {expected_roi * 100:.2f}% < "
                            f"min stale ROI {roi_floor * 100:.2f}%"
                        ),
                        note=_build_note(
                            {
                                **note,
                                "lastListingCheckAt": utc_now_iso(),
                                "listingDepth": depth,
                                "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                                "listingRepriceDecision": "stale_below_min_roi",
                                "listingRepriceBlockedReason": "stale target ROI below min stale ROI",
                                "repriceBlockedAt": utc_now_iso(),
                                "repriceBlockedReason": "stale target ROI below min stale ROI",
                                "repriceTargetPrice": round(target_price, 2),
                                "repriceExpectedRoi": round(expected_roi, 4),
                                "staleListedAgeHours": round(listed_age_hours, 2) if listed_age_hours is not None else None,
                                "staleMinRoi": roi_floor,
                                "staleServerChanSent": bool(settings.serverchan_sendkey),
                                "staleManualReviewAt": utc_now_iso(),
                            }
                        ),
                    )
                    skipped_ids.append(trade_id)
                    continue
                db.update_profit_trade(
                    trade_id,
                    error=(
                        f"reprice ROI {expected_roi * 100:.2f}% < "
                        f"min ROI {roi_floor * 100:.2f}%"
                    ),
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingDepth": depth,
                            "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                            "listingRepriceDecision": "below_min_roi",
                            "listingRepriceBlockedReason": "target ROI below min ROI",
                            "repriceBlockedAt": utc_now_iso(),
                            "repriceBlockedReason": "target ROI below min ROI",
                            "repriceTargetPrice": round(target_price, 2),
                            "repriceExpectedRoi": round(expected_roi, 4),
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue
            try:
                payload = c5_client.sale_modify(
                    app_id=settings.app_id,
                    data_list=[{"productId": int(product_id), "price": round(target_price, 2)}],
                )
            except Exception as exc:
                reason = f"reprice modify failed: {exc}"
                db.update_profit_trade(
                    trade_id,
                    error=reason,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingDepth": depth,
                            "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                            "listingRepriceDecision": "modify_failed",
                            "listingRepriceBlockedReason": reason,
                            "repriceBlockedAt": utc_now_iso(),
                            "repriceBlockedReason": reason,
                            "repriceTargetPrice": round(target_price, 2),
                            "repriceExpectedRoi": round(expected_roi, 4),
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue
            modify_failed = False
            modify_failed_reason = ""
            if isinstance(payload, dict):
                failed_list = payload.get("failedList")
                success_list = payload.get("successList")
                succeed = payload.get("succeed")
                if failed_list:
                    modify_failed = True
                    modify_failed_reason = f"reprice modify failed: {failed_list}"
                elif succeed == 0:
                    modify_failed = True
                    modify_failed_reason = "reprice modify failed: C5 returned succeed=0"
                elif isinstance(success_list, list) and not success_list:
                    modify_failed = True
                    modify_failed_reason = "reprice modify failed: empty successList"
            if modify_failed:
                db.update_profit_trade(
                    trade_id,
                    error=modify_failed_reason,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingDepth": depth,
                            "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                            "listingRepriceDecision": "modify_failed",
                            "listingRepriceBlockedReason": modify_failed_reason,
                            "repriceBlockedAt": utc_now_iso(),
                            "repriceBlockedReason": modify_failed_reason,
                            "repriceTargetPrice": round(target_price, 2),
                            "repriceExpectedRoi": round(expected_roi, 4),
                            "repriceRaw": payload,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue
            db.update_profit_trade(
                trade_id,
                c5_listing_price=target_price,
                c5_expected_net_price=target_net,
                expected_profit=expected_profit,
                expected_roi=expected_roi,
                error=None,
                note=_build_note(
                    {
                        **note,
                        "c5SalePrice": round(target_price, 2),
                        "c5ListingPrice": round(target_price, 2),
                        "c5ExpectedNetPrice": round(target_net, 2),
                        "expectedProfit": round(expected_profit, 2),
                        "expectedRoi": round(expected_roi, 4),
                        "listingDepth": depth,
                        "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                        "listingRepriceDecision": "repriced",
                        "listingRepriceBlockedReason": None,
                        "listingRepriceMode": "stale" if stale_reprice_mode else "normal",
                        "repriceAt": utc_now_iso(),
                        "repriceFrom": round(current_price, 2),
                        "repriceTo": round(target_price, 2),
                        "repriceExpectedNet": round(target_net, 2),
                        "repriceExpectedRoi": round(expected_roi, 4),
                        "staleListedAgeHours": round(listed_age_hours, 2) if stale_reprice_mode and listed_age_hours is not None else None,
                        "staleMinRoi": roi_floor if stale_reprice_mode else None,
                        "repriceRaw": payload,
                    }
                ),
            )
            repriced_ids.append(trade_id)
    finally:
        db.close()
    return {
        "ok": True,
        "repricedTradeIds": repriced_ids,
        "skippedTradeIds": skipped_ids,
        "errors": errors,
    }


def run_profit_trade_once(
    settings: Settings,
    config: StrategyConfig | None = None,
    *,
    allow_cached_fallback: bool = True,
    cache_max_age_minutes: int | None = 180,
    scan_max_items: int | None = None,
    inventory_payload: dict[str, Any] | None = None,
    market_service: MarketService | None = None,
    steam_client: Any | None = None,
    c5_client: Any | None = None,
) -> ProfitTradeRunReport:
    config = config or load_strategy_config(settings)
    errors: list[str] = []
    bought_trade_ids: list[int] = []
    listed_trade_ids: list[int] = []
    settled_trade_ids: list[int] = []
    skipped_trade_ids: list[int] = []
    scanned: ProfitTradeScanReport | None = None

    if not config.profit_trade_enabled:
        return _record_profit_trade_run(settings, ProfitTradeRunReport(
            generated_at=utc_now_iso(),
            enabled=False,
            allow_real_execution=config.profit_trade_allow_real_execution,
            scanned=None,
            bought_trade_ids=[],
            listed_trade_ids=[],
            settled_trade_ids=[],
            skipped_trade_ids=[],
            errors=["profitTrade.enabled is false"],
        ))

    max_buy = max(0, int(config.profit_trade_max_buy_per_cycle))
    try:
        refresh_result = refresh_profit_trade_sales(
            settings,
            config,
            c5_client=c5_client,
        )
        settled_trade_ids.extend(int(value) for value in refresh_result.get("settledTradeIds", []))
        skipped_trade_ids.extend(int(value) for value in refresh_result.get("skippedTradeIds", []))
        errors.extend(str(value) for value in refresh_result.get("errors", []))
    except Exception as exc:
        errors.append(f"refresh-sales: {exc}")

    if not config.profit_trade_allow_real_execution:
        if config.profit_trade_allow_reprice_execution:
            try:
                reprice_result = refresh_profit_trade_listings(
                    settings,
                    config,
                    c5_client=c5_client,
                )
                skipped_trade_ids.extend(int(value) for value in reprice_result.get("skippedTradeIds", []))
                errors.extend(str(value) for value in reprice_result.get("errors", []))
            except Exception as exc:
                errors.append(f"refresh-listings: {exc}")
        scanned = scan_profit_trade_opportunities(
            settings,
            config,
            allow_cached_fallback=allow_cached_fallback,
            cache_max_age_minutes=cache_max_age_minutes,
            limit=max(1, max_buy or 1),
            scan_max_items=scan_max_items,
            record=True,
            lock_asset=False,
            inventory_payload=inventory_payload,
            market_service=market_service,
            c5_client=c5_client,
        )
        return _record_profit_trade_run(settings, ProfitTradeRunReport(
            generated_at=utc_now_iso(),
            enabled=True,
            allow_real_execution=False,
            scanned=scanned,
            bought_trade_ids=[],
            listed_trade_ids=[],
            settled_trade_ids=settled_trade_ids,
            skipped_trade_ids=[*skipped_trade_ids, *list(scanned.created_trade_ids)],
            errors=[
                *errors,
                (
                    "profitTrade.allowRealExecution is false; reprice-only enabled, "
                    "buy/list actions disabled"
                    if config.profit_trade_allow_reprice_execution
                    else "profitTrade.allowRealExecution is false; recorded candidates only"
                ),
            ],
        ))

    try:
        reprice_result = refresh_profit_trade_listings(
            settings,
            config,
            c5_client=c5_client,
        )
        skipped_trade_ids.extend(int(value) for value in reprice_result.get("skippedTradeIds", []))
        errors.extend(str(value) for value in reprice_result.get("errors", []))
    except Exception as exc:
        errors.append(f"refresh-listings: {exc}")

    try:
        recover_result = recover_unverified_profit_trade_steam_buys(settings, config=config)
        skipped_trade_ids.extend(int(value) for value in recover_result.get("skippedTradeIds", []))
        errors.extend(str(value) for value in recover_result.get("errors", []))
    except Exception as exc:
        errors.append(f"recover-buys: {exc}")

    db = Database(settings.db_path)
    try:
        db.initialize()
        daily_spent = _profit_trade_daily_steam_spent(db)
        daily_budget = max(0.0, float(config.profit_trade_daily_steam_budget))
        remaining_budget = max(0.0, daily_budget - daily_spent)
        if daily_budget > 0 and remaining_budget <= 0:
            buy_capacity = 0
            errors.append(f"daily Steam budget reached: spent CNY {daily_spent:.2f} / {daily_budget:.2f}")
        db.release_expired_asset_reservations()
        _mark_expired_locked_trades(db)
        _cancel_stale_pre_buy_manual_trades(db)
        _cancel_stale_buying_trades_without_steam_evidence(db)
        _cancel_recorded_pre_buy_candidates(
            db,
            reason="fresh automatic run superseded recorded pre-buy candidate",
        )
        _mark_manual_review_pre_buy_trades(db, config)
        pending_c5 = [int(row["id"]) for row in db.list_profit_trades(status="steam_bought", limit=200)]
        locked = [int(row["id"]) for row in db.list_profit_trades(status="locked", limit=200)]
    finally:
        db.close()

    for trade_id in pending_c5:
        try:
            result = execute_profit_trade_list_c5(
                settings,
                trade_id,
                config=config,
                c5_client=c5_client,
            )
        except Exception as exc:
            errors.append(f"list-c5 {trade_id}: {exc}")
            continue
        if result.get("ok"):
            listed_trade_ids.append(trade_id)
        else:
            skipped_trade_ids.append(trade_id)

    buy_capacity = max_buy
    if "remaining_budget" in locals() and float(remaining_budget) <= 0:
        buy_capacity = 0
    for trade_id in locked:
        if buy_capacity <= 0:
            break
        row_for_budget = None
        db_budget = Database(settings.db_path)
        try:
            db_budget.initialize()
            row_for_budget = db_budget.get_profit_trade(trade_id)
        finally:
            db_budget.close()
        planned_buy_price = safe_float(row_for_budget["steam_buy_price"]) if row_for_budget is not None else None
        if (
            "remaining_budget" in locals()
            and planned_buy_price is not None
            and planned_buy_price > float(remaining_budget) + 1e-9
        ):
            skipped_trade_ids.append(trade_id)
            errors.append(
                f"buy {trade_id}: daily Steam budget would be exceeded "
                f"(planned CNY {planned_buy_price:.2f}, remaining CNY {float(remaining_budget):.2f})"
            )
            continue
        try:
            result = execute_profit_trade_buy(
                settings,
                trade_id,
                config=config,
                steam_client=steam_client,
                c5_client=c5_client,
            )
        except Exception as exc:
            errors.append(f"buy {trade_id}: {exc}")
            _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                trade_id,
                reason=f"automatic run cancelled locked trade before Steam buy after error: {exc}",
            )
            continue
        if result.get("ok"):
            bought_trade_ids.append(trade_id)
            buy_capacity -= 1
            if "remaining_budget" in locals():
                bought_price = safe_float(result.get("trade", {}).get("steamBuyPrice")) or planned_buy_price or 0.0
                remaining_budget = max(0.0, float(remaining_budget) - bought_price)
            try:
                list_result = execute_profit_trade_list_c5(
                    settings,
                    trade_id,
                    config=config,
                    c5_client=c5_client,
                )
            except Exception as exc:
                errors.append(f"list-c5 {trade_id}: {exc}")
                continue
            if list_result.get("ok"):
                listed_trade_ids.append(trade_id)
            else:
                skipped_trade_ids.append(trade_id)
        else:
            skipped_trade_ids.append(trade_id)

    if buy_capacity > 0:
        scanned = scan_profit_trade_opportunities(
            settings,
            config,
            allow_cached_fallback=allow_cached_fallback,
            cache_max_age_minutes=cache_max_age_minutes,
            limit=buy_capacity,
            scan_max_items=scan_max_items,
            record=True,
            lock_asset=True,
            inventory_payload=inventory_payload,
            market_service=market_service,
            c5_client=c5_client,
        )
        for trade_id in scanned.locked_trade_ids[:buy_capacity]:
            row_for_budget = None
            db_budget = Database(settings.db_path)
            try:
                db_budget.initialize()
                row_for_budget = db_budget.get_profit_trade(trade_id)
            finally:
                db_budget.close()
            planned_buy_price = safe_float(row_for_budget["steam_buy_price"]) if row_for_budget is not None else None
            if (
                "remaining_budget" in locals()
                and planned_buy_price is not None
                and planned_buy_price > float(remaining_budget) + 1e-9
            ):
                skipped_trade_ids.append(trade_id)
                _cancel_locked_trade_before_steam_buy_by_id(
                    settings,
                    trade_id,
                    reason=(
                        f"daily Steam budget would be exceeded "
                        f"(planned CNY {planned_buy_price:.2f}, remaining CNY {float(remaining_budget):.2f})"
                    ),
                )
                continue
            try:
                result = execute_profit_trade_buy(
                    settings,
                    trade_id,
                    config=config,
                    steam_client=steam_client,
                    c5_client=c5_client,
                )
            except Exception as exc:
                errors.append(f"buy {trade_id}: {exc}")
                _cancel_locked_trade_before_steam_buy_by_id(
                    settings,
                    trade_id,
                    reason=f"automatic run cancelled locked trade before Steam buy after error: {exc}",
                )
                continue
            if result.get("ok"):
                bought_trade_ids.append(trade_id)
                buy_capacity -= 1
                if "remaining_budget" in locals():
                    bought_price = safe_float(result.get("trade", {}).get("steamBuyPrice")) or planned_buy_price or 0.0
                    remaining_budget = max(0.0, float(remaining_budget) - bought_price)
                try:
                    list_result = execute_profit_trade_list_c5(
                        settings,
                        trade_id,
                        config=config,
                        c5_client=c5_client,
                    )
                except Exception as exc:
                    errors.append(f"list-c5 {trade_id}: {exc}")
                    continue
                if list_result.get("ok"):
                    listed_trade_ids.append(trade_id)
                else:
                    skipped_trade_ids.append(trade_id)
            else:
                skipped_trade_ids.append(trade_id)

    return _record_profit_trade_run(settings, ProfitTradeRunReport(
        generated_at=utc_now_iso(),
        enabled=True,
        allow_real_execution=True,
        scanned=scanned,
        bought_trade_ids=bought_trade_ids,
        listed_trade_ids=listed_trade_ids,
        settled_trade_ids=settled_trade_ids,
        skipped_trade_ids=skipped_trade_ids,
        errors=errors,
    ))


def _step_progress(step_index: int) -> int:
    if not PROFIT_TRADE_STEPS:
        return 0
    max_index = max(1, len(PROFIT_TRADE_STEPS) - 1)
    bounded = min(max(0, int(step_index)), max_index)
    return int(round((bounded / max_index) * 100))


def _trade_row_to_dict(row: Any) -> dict[str, Any]:
    step_index = int(row["step_index"] or 0)
    status = str(row["status"] or "candidate")
    note = _read_note(row["note"])
    expected_roi = safe_float(row["expected_roi"])
    realized_roi = safe_float(row["realized_roi"])
    market_hash_name = str(row["market_hash_name"] or "")
    name = str(note.get("name") or market_hash_name)
    return {
        "id": int(row["id"]),
        "tradeNo": row["trade_no"],
        "marketHashName": market_hash_name,
        "name": name,
        "status": status,
        "stepKey": row["step_key"],
        "stepIndex": step_index,
        "progressPct": 100 if status == "completed" else _step_progress(step_index),
        "requiresManualAction": status in {"manual_required", "failed"},
        "aAssetId": row["a_asset_id"],
        "aSteamId": row["a_steam_id"],
        "bAssetId": row["b_asset_id"],
        "steamListingId": row["steam_listing_id"],
        "c5ProductId": row["c5_product_id"],
        "steamBuyPrice": row["steam_buy_price"],
        "steamBalanceDiscount": row["steam_balance_discount"],
        "steamRealCost": row["steam_real_cost"],
        "c5ListingPrice": row["c5_listing_price"],
        "c5ExpectedNetPrice": row["c5_expected_net_price"],
        "c5SoldNetPrice": row["c5_sold_net_price"],
        "expectedProfit": row["expected_profit"],
        "realizedProfit": row["realized_profit"],
        "expectedRoi": expected_roi,
        "expectedRoiPct": round(expected_roi * 100, 2) if expected_roi is not None else None,
        "realizedRoi": realized_roi,
        "realizedRoiPct": round(realized_roi * 100, 2) if realized_roi is not None else None,
        "error": row["error"],
        "note": note,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "completedAt": row["completed_at"],
    }


def _hydrate_trade_names(db: Database, trades: list[dict[str, Any]]) -> None:
    for trade in trades:
        current_name = str(trade.get("name") or "").strip()
        market_hash_name = str(trade.get("marketHashName") or "").strip()
        if current_name and current_name != market_hash_name:
            continue
        if not market_hash_name:
            continue
        item = db.get_item(market_hash_name)
        if item is None:
            continue
        name_cn = str(item["name_cn"] or "").strip()
        if name_cn:
            trade["name"] = name_cn


def _reservation_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "assetId": row["asset_id"],
        "marketHashName": row["market_hash_name"],
        "owner": row["owner"],
        "purpose": row["purpose"],
        "status": row["status"],
        "operationId": row["operation_id"],
        "reservedUntil": row["reserved_until"],
        "note": _read_note(row["note"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "releasedAt": row["released_at"],
    }


def build_profit_trade_dashboard_payload(
    settings: Settings,
    *,
    config: StrategyConfig | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    config = config or load_strategy_config(settings)
    db = Database(settings.db_path)
    try:
        db.initialize()
        db.release_expired_asset_reservations()
        _mark_expired_locked_trades(db)
        _cancel_stale_pre_buy_manual_trades(db)
        _cancel_stale_buying_trades_without_steam_evidence(db)
        _cancel_protected_pre_buy_trades(db, config)
        _mark_manual_review_pre_buy_trades(db, config)
        _cancel_c5_risk_failed_pre_buy_trades(db, config)
    finally:
        db.close()

    recover_unverified_profit_trade_steam_buys(settings, config=config)

    db = Database(settings.db_path)
    try:
        db.initialize()
        db.release_expired_asset_reservations()
        trades = [
            _trade_row_to_dict(row)
            for row in db.list_profit_trades(limit=limit)
            if str(row["status"] or "") != "cancelled"
        ]
        _hydrate_trade_names(db, trades)
        reservations = [
            _reservation_row_to_dict(row)
            for row in db.list_asset_reservations(
                owner=PROFIT_TRADE_OWNER,
                statuses=["active", "consumed"],
                limit=limit,
            )
        ]
        daily_steam_spent = _profit_trade_daily_steam_spent(db)
    finally:
        db.close()

    active_count = sum(1 for trade in trades if trade["status"] not in TERMINAL_PROFIT_TRADE_STATUSES)
    failed_count = sum(1 for trade in trades if trade["status"] in {"failed", "manual_required"})
    completed_count = sum(1 for trade in trades if trade["status"] == "completed")
    realized_profit = sum(
        float(trade["realizedProfit"] or 0)
        for trade in trades
        if trade["status"] == "completed"
    )
    expected_profit = sum(
        float(trade["expectedProfit"] or 0)
        for trade in trades
        if trade["status"] not in TERMINAL_PROFIT_TRADE_STATUSES
    )
    daily_budget = max(0.0, float(config.profit_trade_daily_steam_budget))
    return {
        "generatedAt": utc_now_iso(),
        "config": _config_payload(config),
        "steps": PROFIT_TRADE_STEPS,
        "summary": {
            "activeCount": active_count,
            "completedCount": completed_count,
            "failedCount": failed_count,
            "reservedAssetCount": len(reservations),
            "expectedProfit": round(expected_profit, 2),
            "realizedProfit": round(realized_profit, 2),
            "dailySteamSpent": round(daily_steam_spent, 2),
            "dailySteamRemaining": round(max(0.0, daily_budget - daily_steam_spent), 2),
        },
        "trades": trades,
        "reservations": reservations,
        "lastRun": _read_profit_trade_last_run(settings),
    }


def write_profit_trade_dashboard_payload(
    settings: Settings,
    *,
    output_path: Path | None = None,
    limit: int = 100,
) -> Path:
    path = output_path or _default_frontend_payload_path()
    payload = build_profit_trade_dashboard_payload(settings, limit=limit)
    ensure_parent_dir(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def set_profit_trade_enabled(settings: Settings, enabled: bool) -> StrategyConfig:
    config = load_strategy_config(settings)
    config.profit_trade_enabled = bool(enabled)
    save_strategy_config(settings, config)
    return config


def set_profit_trade_real_execution(settings: Settings, allow_real_execution: bool) -> StrategyConfig:
    config = load_strategy_config(settings)
    config.profit_trade_allow_real_execution = bool(allow_real_execution)
    save_strategy_config(settings, config)
    return config


def set_profit_trade_config(
    settings: Settings,
    *,
    enabled: bool | None = None,
    allow_real_execution: bool | None = None,
    allow_reprice_execution: bool | None = None,
    sticker_slab_status: str | None = None,
    sticker_status: str | None = None,
    daily_steam_budget: float | None = None,
) -> StrategyConfig:
    def _normalize_status(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"blocked", "active"}:
            raise ValueError("item type status must be blocked or active")
        return normalized

    config = load_strategy_config(settings)
    if enabled is not None:
        config.profit_trade_enabled = bool(enabled)
    if allow_real_execution is not None:
        config.profit_trade_allow_real_execution = bool(allow_real_execution)
    if allow_reprice_execution is not None:
        config.profit_trade_allow_reprice_execution = bool(allow_reprice_execution)
    if sticker_slab_status is not None:
        config.profit_trade_sticker_slab_status = _normalize_status(sticker_slab_status)
    if sticker_status is not None:
        config.profit_trade_sticker_status = _normalize_status(sticker_status)
    if daily_steam_budget is not None:
        budget = float(daily_steam_budget)
        if budget < 0:
            raise ValueError("dailySteamBudget must be >= 0")
        config.profit_trade_daily_steam_budget = budget
    save_strategy_config(settings, config)
    return config


def _dedupe_str_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def update_profit_trade_protection(
    settings: Settings,
    *,
    action: str,
    kind: str,
    value: str,
) -> StrategyConfig:
    normalized_action = str(action or "").strip().lower()
    normalized_kind = str(kind or "").strip()
    normalized_value = str(value or "").strip()
    if normalized_action not in {"add", "remove"}:
        raise ValueError("action must be add or remove")
    if normalized_kind not in {"asset", "marketHashName", "steamId"}:
        raise ValueError("kind must be asset, marketHashName, or steamId")
    if not normalized_value:
        raise ValueError("value is required")

    config = load_strategy_config(settings)
    field_map = {
        "asset": "profit_trade_protected_asset_ids",
        "marketHashName": "profit_trade_protected_market_hash_names",
        "steamId": "profit_trade_protected_steam_ids",
    }
    field_name = field_map[normalized_kind]
    values = _dedupe_str_list(list(getattr(config, field_name) or []))
    if normalized_action == "add":
        values = _dedupe_str_list([*values, normalized_value])
    else:
        if normalized_kind == "marketHashName":
            values = [item for item in values if item.lower() != normalized_value.lower()]
        else:
            values = [item for item in values if item != normalized_value]
    setattr(config, field_name, values)
    save_strategy_config(settings, config)
    return config


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_today_beijing(row: dict[str, Any], *, now: datetime) -> bool:
    tz = timezone(timedelta(hours=8))
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    updated = _parse_iso(row.get("completedAt") or row.get("updatedAt"))
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return updated.astimezone(tz) >= start


def render_profit_trade_daily_report(settings: Settings) -> str:
    payload = build_profit_trade_dashboard_payload(settings, limit=500)
    now = datetime.now(timezone.utc)
    today_trades = [trade for trade in payload["trades"] if _is_today_beijing(trade, now=now)]
    completed = [trade for trade in today_trades if trade["status"] == "completed"]
    failed = [trade for trade in today_trades if trade["status"] in {"failed", "manual_required"}]
    active = [trade for trade in payload["trades"] if trade["status"] not in TERMINAL_PROFIT_TRADE_STATUSES]
    realized_profit = sum(float(trade["realizedProfit"] or 0) for trade in completed)
    expected_profit = sum(float(trade["expectedProfit"] or 0) for trade in active)
    lines = [
        "## 搬砖做T日报",
        "",
        f"- 今日完成: {len(completed)} 笔",
        f"- 今日失败/需人工: {len(failed)} 笔",
        f"- 当前进行中: {len(active)} 笔",
        f"- 今日已结算收益: CNY {realized_profit:.2f}",
        f"- 进行中预计收益: CNY {expected_profit:.2f}",
    ]
    if failed:
        lines.append("")
        lines.append("### 需要处理")
        for trade in failed[:10]:
            lines.append(
                f"- {trade['tradeNo']} | {trade['marketHashName']} | "
                f"{trade['status']} | {trade.get('error') or '-'}"
            )
    return "\n".join(lines)


def send_profit_trade_daily_report(settings: Settings) -> bool:
    if not settings.serverchan_sendkey:
        raise RuntimeError("缺少 SERVERCHAN_SENDKEY / SCTKEY")
    client = ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url)
    body = render_profit_trade_daily_report(settings)
    client.send("搬砖做T日报", body)
    return True

































