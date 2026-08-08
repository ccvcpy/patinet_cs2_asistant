from __future__ import annotations

import inspect
import json
import math
import re
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable

from cs2_assistant.accounts import Account, AccountStore
from cs2_assistant.clients import C5GameClient, SteamMarketClient, SteamMarketError
from cs2_assistant.clients.serverchan import ServerChanClient
from cs2_assistant.config import PROJECT_ROOT, Settings
from cs2_assistant.db import Database
from cs2_assistant.models import MarketState, StrategyConfig
from cs2_assistant.services.market import MarketService
from cs2_assistant.services.c5_ip_circuit import (
    bind_c5_ip_circuit_telemetry,
    build_c5_ip_request_guard,
)
from cs2_assistant.services.pricing import (
    build_orderbook_snapshot,
    choose_orderbook_price,
    summarize_orderbook_prices,
)
from cs2_assistant.services.profit_trade_logging import get_profit_trade_event_logger
from cs2_assistant.services.profit_trade_long_buy import (
    LONG_BUY_LIVE_STATES,
    LONG_BUY_MUTABLE_STATES,
    LONG_BUY_PREVIOUS_PRICE_EXCLUSION_SECONDS,
    build_long_buy_proposal,
    cents_to_price,
    long_buy_order_public,
    normalize_roi_four_decimals,
    price_to_cents,
    remembered_own_price_cents,
)
from cs2_assistant.services.public_payload import sanitize_public_payload
from cs2_assistant.services.steam_balances import (
    load_steam_account_balances,
    update_steam_account_balance_snapshot,
)
from cs2_assistant.services.steam_request_scheduler import (
    SteamRequestGuardRejected,
    SteamRequestTimeout,
)
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
# Project policy: keep three failed createbuyorder attempts unless the user explicitly changes it.
STEAM_BUY_LISTING_RETRY_ATTEMPTS = 3
# Independent pre-purchase protection for Steam's listings route. This does not
# consume or change the three concrete buy attempts above.
STEAM_SEARCH_LISTINGS_429_ATTEMPTS = 1
STEAM_SEARCH_LISTINGS_CIRCUIT_COOLDOWN_SECONDS = 10 * 60
PROFIT_TRADE_LISTINGS_CIRCUIT_KEY = "steam_search_listings"
STEAM_BUY_CANCEL_VERIFY_ATTEMPTS = 3
STEAM_BUY_CANCEL_VERIFY_DELAY_SECONDS = 0.35
STEAM_BUY_FAILED_LISTING_TTL_SECONDS = 300.0
C5_SALE_SYNC_PENDING_MAX_SECONDS = 3 * 60 * 60
C5_SALE_SYNC_INITIAL_GRACE_SECONDS = 30.0
PROFIT_TRADE_CYCLE_INTERVAL_SECONDS = 10 * 60
PROFIT_TRADE_SELECTION_WATCH_INTERVAL_SECONDS = PROFIT_TRADE_CYCLE_INTERVAL_SECONDS
PROFIT_TRADE_SELECTION_WATCH_MAX_ITEMS = 200
PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY = 20
PROFIT_TRADE_MANUAL_EXECUTION_REQUEST_TTL_SECONDS = 5 * 60
PROFIT_TRADE_MANUAL_EXECUTION_MIN_APPROVAL_SECONDS = 15 * 60
PROFIT_TRADE_LONG_BUY_BASE_QUANTITY = 4
PROFIT_TRADE_LONG_BUY_HISTORY_PAGE_SIZE = 500
PROFIT_TRADE_LONG_BUY_HISTORY_MAX_PAGES = 10
PROFIT_TRADE_LONG_BUY_MY_LISTINGS_PAGE_SIZE = 100
PROFIT_TRADE_LONG_BUY_MY_LISTINGS_MAX_PAGES = 100
PROFIT_TRADE_PURCHASE_REQUEST_EVIDENCE_NOTE_KEYS = (
    "steamBuyRequestedAt",
    "steamBuySucceededAt",
    "steamBuyUnverifiedAt",
    "steamBuyOrderId",
    "steamBuyPayload",
    "steamPurchaseReceipt",
    "steamBuyVerifiedBy",
    "newInventoryAssetIds",
)
_STEAM_BUY_FAILED_LISTING_BLACKLIST: dict[tuple[str, str], float] = {}


def _profit_trade_new_action_allowed(
    new_action_guard: Callable[[], bool] | None,
) -> bool:
    """Fail closed when the persistent runtime can no longer admit new actions."""

    if new_action_guard is None:
        return True
    try:
        return bool(new_action_guard())
    except Exception:
        return False


def _profit_trade_long_buy_remote_action_allowed(
    settings: Settings,
    new_action_guard: Callable[[], bool] | None = None,
) -> bool:
    """Re-read every mutable switch immediately before a long-buy write."""

    try:
        latest = load_strategy_config(settings)
    except Exception:
        return False
    return bool(
        latest.profit_trade_enabled
        and latest.profit_trade_allow_real_execution
        and latest.profit_trade_long_buy_enabled
        and latest.profit_trade_long_buy_allow_real_execution
        and _profit_trade_new_action_allowed(new_action_guard)
    )


def _callable_accepts_keyword_argument(
    callback: Callable[..., Any],
    keyword: str,
) -> bool:
    """Inspect adapters before invocation; never retry a write after TypeError."""

    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        # Production SteamMarketClient supports the safety arguments. If an
        # opaque callable does not, its first TypeError is treated as an
        # uncertain single attempt rather than grounds for a second write.
        return True
    parameter = parameters.get(keyword)
    if parameter is not None:
        return parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
    return any(
        value.kind is inspect.Parameter.VAR_KEYWORD
        for value in parameters.values()
    )


def _profit_trade_telemetry_context(
    row: Any | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if row is not None:
        note = _read_note(row["note"])
        context.update(
            {
                "trade_id": int(row["id"]),
                "trade_no": str(row["trade_no"] or "").strip() or None,
                "market_hash_name": str(row["market_hash_name"] or "").strip() or None,
                "asset_id": str(row["a_asset_id"] or note.get("assetId") or "").strip() or None,
                "account_id": str(note.get("steamAccountId") or "").strip() or None,
                "steam_id64": str(row["a_steam_id"] or note.get("steamId") or "").strip() or None,
            }
        )
    context.update(overrides)
    return {key: value for key, value in context.items() if value not in (None, "")}


def _profit_trade_telemetry_callback(**context: Any) -> Any:
    return get_profit_trade_event_logger().bind_telemetry(**context)


def _profit_trade_c5_telemetry_callback(settings: Settings, **context: Any) -> Any:
    return bind_c5_ip_circuit_telemetry(
        settings,
        source="profit_trade",
        downstream=_profit_trade_telemetry_callback(**context),
    )


def _build_profit_trade_c5_client(
    settings: Settings,
    **telemetry_context: Any,
) -> C5GameClient:
    if not settings.c5_api_key:
        raise RuntimeError("missing C5GAME_API_KEY / C5_API_KEY")
    return C5GameClient(
        str(settings.c5_api_key),
        settings.c5_base_url,
        telemetry_callback=_profit_trade_c5_telemetry_callback(settings, **telemetry_context),
        request_guard=build_c5_ip_request_guard(settings),
    )


def _build_profit_trade_market_service(
    settings: Settings,
    *,
    telemetry_context: dict[str, Any] | None = None,
    allow_relogin: bool = True,
) -> MarketService:
    """ProfitTrade execution only trusts Steam orderbook and C5 batch prices."""
    context = dict(telemetry_context or {})
    callback = _profit_trade_telemetry_callback(**context)
    store = AccountStore(PROJECT_ROOT / "config")
    usable_accounts: list[Account] = []
    for account in store.list_accounts():
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
                telemetry_callback=callback,
                request_source="profit_trade",
                **({"allow_account_relogin": False} if not allow_relogin else {}),
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
                telemetry_callback=callback,
                request_source="profit_trade",
                **({"allow_account_relogin": False} if not allow_relogin else {}),
            )
        except Exception:
            pass

    return MarketService(
        steamdt_client=None,
        csqaq_client=None,
        c5_client=_build_profit_trade_c5_client(settings, **context)
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
    reserved_balance: float
    spendable_balance: float | None
    wallet: dict[str, Any]
    wallet_is_live: bool = False


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
    purchase_receipt: dict[str, Any] | None = None


@dataclass(slots=True)
class SteamBuyOrderResolution:
    outcome: str
    verification: SteamBuyVerification
    cancel_payload: dict[str, Any] | None = None
    cancel_error: str | None = None


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
    orderbook_snapshot: dict[str, Any] = field(default_factory=dict)
    c5_pricing: dict[str, Any] = field(default_factory=dict)

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
            "steamOrderbook": self.orderbook_snapshot or None,
            "c5Pricing": self.c5_pricing or None,
        }


@dataclass(slots=True)
class ProfitTradeMarketEvaluation:
    market_hash_name: str
    name: str
    steam_buy_price: float
    steam_price_source: str
    c5_listing_price: float
    c5_price_source: str
    c5_expected_net_price: float
    balance_discount: float
    steam_real_cost: float
    expected_profit: float
    expected_roi: float
    inventory_count: int
    tradable_count: int
    c5_recent_sold_net_price: float | None
    c5_recent_sold_count: int | None
    c5_current_sell_price: float | None
    c5_on_sale_count: int | None
    c5_purchase_max_price: float | None
    c5_purchase_count: int | None
    risk_status: str
    risk_reason: str
    execution_status: str
    execution_reason: str
    audit_status: str = "passed"
    audit_reason: str = "rule_based"
    orderbook_snapshot: dict[str, Any] = field(default_factory=dict)
    crossed_listing_probe: dict[str, Any] = field(default_factory=dict)
    c5_pricing: dict[str, Any] = field(default_factory=dict)

    def to_watch_record(
        self,
        config: StrategyConfig,
        *,
        execution_status: str | None = None,
        execution_reason: str | None = None,
        manual_executable_quantity: int | None = None,
        long_buy_order: Any | None = None,
        long_buy_proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        buy_order_reference = _profit_trade_buy_order_reference_values(
            c5_expected_net_price=self.c5_expected_net_price,
            roi_basis=self.balance_discount,
            orderbook_snapshot=self.orderbook_snapshot,
        )
        public_long_buy_order = long_buy_order_public(long_buy_order)
        proposal = dict(long_buy_proposal or {})
        if proposal:
            proposal_roi = proposal.get("competitorBuyRoi")
            proposal_status = str(
                proposal.get("competitorBuyStatus") or ""
            ).strip()
            if proposal_roi is not None:
                reference_status = (
                    "crossed_possible_stale"
                    if self.orderbook_snapshot.get("crossed") is True
                    else "valid"
                )
            elif proposal_status == "missing_external_level":
                reference_status = "missing_buy_book"
            else:
                reference_status = proposal_status or "missing_buy_book"
            buy_order_reference = {
                "roi": proposal_roi,
                "profit": proposal.get("competitorBuyProfit"),
                "status": reference_status,
            }
        c5_purchase_sell_ratio = (
            self.c5_purchase_max_price / self.c5_current_sell_price
            if self.c5_purchase_max_price is not None
            and self.c5_current_sell_price is not None
            and self.c5_current_sell_price > 0
            else None
        )
        return {
            "market_hash_name": self.market_hash_name,
            "name_cn": self.name,
            "steam_buy_price": self.steam_buy_price,
            "steam_price_source": self.steam_price_source,
            "c5_listing_price": self.c5_listing_price,
            "c5_price_source": self.c5_price_source,
            "c5_expected_net_price": self.c5_expected_net_price,
            "balance_discount": self.balance_discount,
            "expected_profit": self.expected_profit,
            "expected_roi": self.expected_roi,
            "buy_order_reference_roi": buy_order_reference["roi"],
            "buy_order_reference_profit": buy_order_reference["profit"],
            "buy_order_reference_status": buy_order_reference["status"],
            "min_roi": float(config.profit_trade_min_roi),
            "manual_review_roi": float(config.profit_trade_manual_review_roi),
            "inventory_count": self.inventory_count,
            "tradable_count": self.tradable_count,
            "c5_recent_sold_net_price": self.c5_recent_sold_net_price,
            "c5_recent_sold_count": self.c5_recent_sold_count,
            "c5_current_sell_price": self.c5_current_sell_price,
            "c5_on_sale_count": self.c5_on_sale_count,
            "c5_purchase_max_price": self.c5_purchase_max_price,
            "c5_purchase_count": self.c5_purchase_count,
            "risk_status": self.risk_status,
            "risk_reason": self.risk_reason,
            "execution_status": execution_status or self.execution_status,
            "execution_reason": execution_reason or self.execution_reason,
            "keep_active": bool(public_long_buy_order or proposal),
            "raw": {
                "auditStatus": self.audit_status,
                "auditReason": self.audit_reason,
                "steamRealCost": self.steam_real_cost,
                "roiBasis": self.balance_discount,
                "buyOrderReference": buy_order_reference,
                "steamOrderbook": self.orderbook_snapshot,
                "crossedListingProbe": self.crossed_listing_probe or None,
                "c5CurrentSellPrice": self.c5_current_sell_price,
                "c5OnSaleCount": self.c5_on_sale_count,
                "c5PurchaseMaxPrice": self.c5_purchase_max_price,
                "c5PurchaseCount": self.c5_purchase_count,
                "c5PurchaseSellRatio": c5_purchase_sell_ratio,
                "c5Pricing": self.c5_pricing or None,
                "c5MinPurchaseSellRatio": float(
                    config.profit_trade_c5_min_purchase_sell_ratio
                ),
                "manualExecutableQuantity": (
                    max(0, int(manual_executable_quantity))
                    if manual_executable_quantity is not None
                    else None
                ),
                "competitorBuyPrice": proposal.get("competitorBuyPrice"),
                "competitorBuyRoi": proposal.get("competitorBuyRoi"),
                "competitorBuyProfit": proposal.get("competitorBuyProfit"),
                "competitorBuyStatus": proposal.get("competitorBuyStatus"),
                "excludedOwnBuyPrices": list(
                    proposal.get("excludedOwnBuyPrices") or []
                ),
                "longBuyOrder": public_long_buy_order,
                "longBuyProposal": proposal or None,
            },
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
    # Internal cycle inputs. They deliberately stay out of ``to_dict`` so C5
    # sale tokens/style tokens and unbounded scan details never leak through
    # the public run-report API.
    inventory_items: list[dict[str, Any]] = field(default_factory=list, repr=False)
    watch_records: list[dict[str, Any]] = field(default_factory=list, repr=False)

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
    sold_orders_by_asset_id: dict[str, dict[str, Any]]
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
    summary = _summarize_profit_trade_run(report)
    get_profit_trade_event_logger().emit(
        level="ERROR" if report.errors else "INFO",
        provider="local",
        component="profit_trade_runner",
        operation="run_completed",
        message=str(summary.get("summary") or "Profit Trade run completed"),
        run_id=str(report.generated_at),
        safe_context={
            "enabled": report.enabled,
            "allow_real_execution": report.allow_real_execution,
            "bought_trade_ids": list(report.bought_trade_ids),
            "listed_trade_ids": list(report.listed_trade_ids),
            "settled_trade_ids": list(report.settled_trade_ids),
            "skipped_trade_ids": list(report.skipped_trade_ids),
            "errors": list(report.errors),
        },
    )
    return report


def _config_payload(
    config: StrategyConfig,
    *,
    protected_market_hash_name_items: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "enabled": config.profit_trade_enabled,
        "allowRealExecution": config.profit_trade_allow_real_execution,
        "longBuyEnabled": config.profit_trade_long_buy_enabled,
        "longBuyAllowRealExecution": (
            config.profit_trade_long_buy_allow_real_execution
        ),
        "balanceDiscount": config.profit_trade_balance_discount,
        "balanceDiscountPct": round(config.profit_trade_balance_discount * 100, 2),
        "minRoi": config.profit_trade_min_roi,
        "minRoiPct": round(config.profit_trade_min_roi * 100, 2),
        "minItemValue": config.profit_trade_min_item_value,
        "maxBuyPerCycle": config.profit_trade_max_buy_per_cycle,
        "dailySteamBudget": config.profit_trade_daily_steam_budget,
        "accountReservedBalances": dict(
            config.profit_trade_account_reserved_balances or {}
        ),
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
        "initialListingDiscountPct": config.profit_trade_initial_listing_discount_pct,
        "repriceDiscountPct": config.profit_trade_reprice_discount_pct,
        "repriceCooldownHours": config.profit_trade_reprice_cooldown_hours,
        "staleRepriceAfterHours": config.profit_trade_stale_reprice_after_hours,
        "staleManualReviewAfterHours": config.profit_trade_stale_manual_review_after_hours,
        "staleMinRoiFactor": config.profit_trade_stale_min_roi_factor,
        "stickerSlabStatus": config.profit_trade_sticker_slab_status,
        "stickerStatus": config.profit_trade_sticker_status,
        "protectedAssetIds": list(config.profit_trade_protected_asset_ids or []),
        "protectedMarketHashNames": list(config.profit_trade_protected_market_hash_names or []),
        "protectedMarketHashNameItems": list(protected_market_hash_name_items or []),
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


def _parse_utc_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _profit_trade_listings_circuit_projection(
    state: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw = dict(state or {})
    raw_status = str(raw.get("status") or "closed").strip().lower()
    cooldown_until = _parse_utc_timestamp(raw.get("cooldownUntil"))
    status = "open" if raw_status == "open" else "closed"
    if status == "open" and (cooldown_until is None or current >= cooldown_until):
        status = "closed"
    remaining_seconds = 0
    if status == "open" and cooldown_until is not None:
        remaining_seconds = max(0, int(math.ceil((cooldown_until - current).total_seconds())))
    first_429_at = _parse_utc_timestamp(raw.get("first429At"))
    open_seconds = (
        max(0, int((current - first_429_at).total_seconds()))
        if first_429_at is not None and status != "closed"
        else 0
    )
    return {
        **raw,
        "stateKey": PROFIT_TRADE_LISTINGS_CIRCUIT_KEY,
        "status": status,
        "rawStatus": raw_status,
        "cooldownUntil": raw.get("cooldownUntil") if status == "open" else None,
        "isBlocking": status == "open",
        "cooldownActive": status == "open",
        "probeAllowed": False,
        "nextProbeAt": None,
        "remainingSeconds": remaining_seconds,
        "openSeconds": open_seconds,
        "shortRetryAttempts": int(STEAM_SEARCH_LISTINGS_429_ATTEMPTS),
        "shortRetryDelaysSeconds": [],
        "cooldownSeconds": int(STEAM_SEARCH_LISTINGS_CIRCUIT_COOLDOWN_SECONDS),
        "normalCooldownSeconds": int(STEAM_SEARCH_LISTINGS_CIRCUIT_COOLDOWN_SECONDS),
        "scope": "profit_trade_global",
        "operation": "search_listings",
        "reason": (
            raw.get("reason") or "Steam listings 返回 HTTP 429，指定卖单查询正在冷却"
        ) if status == "open" else None,
    }


def _protected_market_hash_name_items(
    db: Database,
    config: StrategyConfig,
) -> list[dict[str, str]]:
    """Return protected kinds with a localised display name for the public UI."""
    items: list[dict[str, str]] = []
    for market_hash_name in config.profit_trade_protected_market_hash_names or []:
        normalized = str(market_hash_name or "").strip()
        if not normalized:
            continue
        row = db.get_item(normalized)
        name_cn = str(row["name_cn"] or "").strip() if row is not None else ""
        if not name_cn and normalized.startswith("StatTrak™ "):
            base_name = normalized.removeprefix("StatTrak™ ")
            base_row = db.get_item(base_name)
            if base_row is not None and str(base_row["name_cn"] or "").strip():
                name_cn = f"StatTrak™ {str(base_row['name_cn']).strip()}"
        items.append(
            {
                "marketHashName": normalized,
                "name": name_cn or normalized,
            }
        )
    return items


def _get_profit_trade_listings_circuit(db: Database) -> dict[str, Any]:
    return _profit_trade_listings_circuit_projection(
        db.get_profit_trade_runtime_state(PROFIT_TRADE_LISTINGS_CIRCUIT_KEY)
    )


def _send_profit_trade_listings_circuit_alert(
    settings: Settings,
    *,
    title: str,
    state: dict[str, Any],
) -> bool:
    if not settings.serverchan_sendkey:
        return False
    lines = [
        "## Steam listings 限流状态",
        "",
        "- 状态: 冷却中",
        f"- 触发账号: {state.get('triggerAccountName') or state.get('triggerAccountId') or '-'}",
        f"- SteamID: {state.get('triggerSteamId') or '-'}",
        f"- 饰品: {state.get('triggerMarketHashName') or '-'}",
        f"- 首次429: {state.get('first429At') or '-'}",
        f"- 最近429: {state.get('last429At') or '-'}",
        f"- 冷却结束: {state.get('cooldownUntil') or '-'}",
        f"- 连续429: {state.get('consecutive429Count') or 0}",
        "",
        "冷却期间继续更新 ROI、C5 同步和结算；符合条件的机会重新校验后改走安全求购。冷却到期自动恢复正常 listings 查询。",
    ]
    ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url).send(
        title,
        "\n".join(lines),
    )
    return True


def _open_profit_trade_listings_circuit(
    db: Database,
    settings: Settings,
    *,
    row: Any,
    client: Any,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    previous = db.get_profit_trade_runtime_state(PROFIT_TRADE_LISTINGS_CIRCUIT_KEY) or {}
    previous_projection = _profit_trade_listings_circuit_projection(previous, now=now)
    was_closed = previous_projection["status"] == "closed"
    first_429_at = _parse_utc_timestamp(previous.get("first429At")) if not was_closed else None
    if first_429_at is None:
        first_429_at = now
    interval_seconds = STEAM_SEARCH_LISTINGS_CIRCUIT_COOLDOWN_SECONDS
    cooldown_until = now + timedelta(seconds=interval_seconds)
    note = _read_note(row["note"])
    account_id = str(getattr(client, "account_id", "") or note.get("steamAccountId") or "").strip()
    steam_id64 = str(getattr(client, "steam_id64", "") or row["a_steam_id"] or "").strip()
    account_name = None
    if account_id:
        try:
            account = AccountStore(PROJECT_ROOT / "config").get_account(account_id)
            account_name = account.name if account is not None else None
        except Exception:
            account_name = None
    payload = {
        **previous,
        "status": "open",
        "reason": "Steam listings 返回 HTTP 429，指定卖单查询正在冷却",
        "openedAt": previous.get("openedAt") if not was_closed else now.isoformat(),
        "first429At": first_429_at.isoformat(),
        "last429At": now.isoformat(),
        "cooldownUntil": cooldown_until.isoformat(),
        "nextProbeAt": None,
        "probeStartedAt": None,
        "cooldownSeconds": int(interval_seconds),
        "consecutive429Count": (0 if was_closed else int(previous.get("consecutive429Count") or 0))
        + len(events),
        "triggerAccountId": account_id or None,
        "triggerAccountName": account_name,
        "triggerSteamId": steam_id64 or None,
        "triggerMarketHashName": str(row["market_hash_name"] or "").strip() or None,
        "triggerTradeId": int(row["id"]),
        "triggerTradeNo": str(row["trade_no"] or "").strip() or None,
        "lastRetryAfter": events[-1].get("retryAfter") if events else None,
    }
    should_send_open = was_closed
    if should_send_open:
        payload["openedAlertSent"] = bool(settings.serverchan_sendkey)
        payload["openedAlertAt"] = now.isoformat() if settings.serverchan_sendkey else None
    saved = db.set_profit_trade_runtime_state(PROFIT_TRADE_LISTINGS_CIRCUIT_KEY, payload)
    projected = _profit_trade_listings_circuit_projection(saved, now=now)
    get_profit_trade_event_logger().emit(
        level="ERROR" if was_closed else "WARN",
        provider="local",
        component="profit_trade_listings_circuit",
        operation="listings_circuit_opened" if was_closed else "listings_circuit_extended",
        message=(
            "Steam listings circuit opened after repeated HTTP 429"
            if was_closed
            else "Steam listings circuit cooldown was extended after another HTTP 429"
        ),
        **_profit_trade_telemetry_context(row),
        safe_context=projected,
    )
    try:
        if should_send_open:
            _send_profit_trade_listings_circuit_alert(
                settings,
                title="搬砖做T Steam listings进入冷却",
                state=projected,
            )
    except Exception as exc:
        get_profit_trade_event_logger().emit(
            level="WARN",
            provider="local",
            component="profit_trade_listings_circuit",
            operation="listings_circuit_alert_failed",
            message="Steam listings circuit ServerChan notification failed",
            safe_context={"error": str(exc)},
        )
    return projected


def _open_profit_trade_listings_circuit_for_observation(
    db: Database,
    settings: Settings,
    *,
    market_hash_name: str,
    client: Any,
    retry_after: str | None = None,
) -> dict[str, Any]:
    """Open the shared listings circuit after a diagnostic observation gets 429.

    A crossed-orderbook probe is not attached to a real trade row, so it cannot
    use ``_open_profit_trade_listings_circuit``.  It still touched the same
    Steam route, though, and therefore must protect every Profit Trade caller
    with the same ten-minute circuit instead of retrying under another name.
    """

    now = datetime.now(timezone.utc).replace(microsecond=0)
    previous = db.get_profit_trade_runtime_state(PROFIT_TRADE_LISTINGS_CIRCUIT_KEY) or {}
    previous_projection = _profit_trade_listings_circuit_projection(previous, now=now)
    was_closed = previous_projection["status"] == "closed"
    first_429_at = _parse_utc_timestamp(previous.get("first429At")) if not was_closed else None
    if first_429_at is None:
        first_429_at = now
    cooldown_until = now + timedelta(seconds=STEAM_SEARCH_LISTINGS_CIRCUIT_COOLDOWN_SECONDS)
    account_id = str(getattr(client, "account_id", "") or "").strip()
    steam_id64 = str(getattr(client, "steam_id64", "") or "").strip()
    account_name = None
    if account_id:
        try:
            account = AccountStore(PROJECT_ROOT / "config").get_account(account_id)
            account_name = account.name if account is not None else None
        except Exception:
            account_name = None
    payload = {
        **previous,
        "status": "open",
        "reason": "Steam listings 返回 HTTP 429，具体卖单查询正在冷却",
        "openedAt": previous.get("openedAt") if not was_closed else now.isoformat(),
        "first429At": first_429_at.isoformat(),
        "last429At": now.isoformat(),
        "cooldownUntil": cooldown_until.isoformat(),
        "nextProbeAt": None,
        "probeStartedAt": None,
        "cooldownSeconds": int(STEAM_SEARCH_LISTINGS_CIRCUIT_COOLDOWN_SECONDS),
        "consecutive429Count": (
            0 if was_closed else int(previous.get("consecutive429Count") or 0)
        ) + 1,
        "triggerAccountId": account_id or None,
        "triggerAccountName": account_name,
        "triggerSteamId": steam_id64 or None,
        "triggerMarketHashName": str(market_hash_name or "").strip() or None,
        "triggerTradeId": None,
        "triggerTradeNo": None,
        "lastRetryAfter": retry_after,
        "triggerSource": "crossed_orderbook_listing_probe",
    }
    if was_closed:
        payload["openedAlertSent"] = bool(settings.serverchan_sendkey)
        payload["openedAlertAt"] = now.isoformat() if settings.serverchan_sendkey else None
    saved = db.set_profit_trade_runtime_state(PROFIT_TRADE_LISTINGS_CIRCUIT_KEY, payload)
    projected = _profit_trade_listings_circuit_projection(saved, now=now)
    get_profit_trade_event_logger().emit(
        level="ERROR" if was_closed else "WARN",
        provider="local",
        component="profit_trade_listings_circuit",
        operation="listings_circuit_opened" if was_closed else "listings_circuit_extended",
        message="Steam listings circuit opened by crossed-orderbook evidence probe",
        market_hash_name=str(market_hash_name or "").strip() or None,
        account_id=account_id or None,
        steam_id64=steam_id64 or None,
        safe_context=projected,
    )
    try:
        if was_closed:
            _send_profit_trade_listings_circuit_alert(
                settings,
                title="Profit Trade Steam listings 进入冷却",
                state=projected,
            )
    except Exception as exc:
        get_profit_trade_event_logger().emit(
            level="WARN",
            provider="local",
            component="profit_trade_listings_circuit",
            operation="listings_circuit_alert_failed",
            message="Steam listings circuit ServerChan notification failed",
            safe_context={"error": str(exc)[:500]},
        )
    return projected


def _build_steam_client(
    settings: Settings,
    *,
    telemetry_context: dict[str, Any] | None = None,
) -> SteamMarketClient:
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
        telemetry_callback=_profit_trade_telemetry_callback(**dict(telemetry_context or {})),
        request_source="profit_trade",
    )


def _build_steam_client_for_account(
    settings: Settings,
    account: Account,
    *,
    telemetry_context: dict[str, Any] | None = None,
) -> SteamMarketClient:
    return SteamMarketClient(
        cookies=account.cookies,
        steam_id64=account.steam_id64,
        identity_secret=account.identity_secret,
        device_id=account.device_id,
        account_id=account.id,
        base_url=settings.steam_market_base_url,
        telemetry_callback=_profit_trade_telemetry_callback(**dict(telemetry_context or {})),
        request_source="profit_trade",
    )


def _build_steam_client_for_profit_trade(
    settings: Settings,
    row: Any,
    *,
    steam_client: Any | None = None,
) -> Any:
    note = _read_note(row["note"])
    telemetry_context = _profit_trade_telemetry_context(row)
    target_account_id = str(note.get("steamAccountId") or "").strip()
    target_steam_id = str(note.get("steamId") or row["a_steam_id"] or "").strip()
    if steam_client is not None:
        client_account_id = str(getattr(steam_client, "account_id", "") or "").strip()
        client_steam_id = str(getattr(steam_client, "steam_id64", "") or "").strip()
        account_matches = not target_account_id or not client_account_id or client_account_id == target_account_id
        steam_matches = not target_steam_id or not client_steam_id or client_steam_id == target_steam_id
        if account_matches and steam_matches:
            return steam_client

    store = AccountStore(PROJECT_ROOT / "config")
    accounts = store.list_accounts()
    selected: Account | None = None
    if target_account_id:
        selected = next((account for account in accounts if str(account.id) == target_account_id), None)
    if selected is None and target_steam_id:
        selected = next(
            (
                account
                for account in accounts
                if str(account.steam_id64 or "").strip() == target_steam_id
            ),
            None,
        )
    if selected is None or not selected.cookies:
        raise RuntimeError(
            f"cannot build Steam client for tracked buy order: "
            f"accountId={target_account_id or '-'}, steamId={target_steam_id or '-'}"
        )
    return _build_steam_client_for_account(
        settings,
        selected,
        telemetry_context=telemetry_context,
    )


def _account_reserved_balance(
    account: Account | None,
    reserved_balances: dict[str, float] | None,
    *,
    account_id: str | None = None,
    steam_id64: str | None = None,
) -> float:
    configured = reserved_balances or {}
    lookup_keys = (
        str(account.id if account is not None else account_id or "").strip(),
        str(account.name if account is not None else "").strip(),
        str(account.steam_id64 if account is not None else steam_id64 or "").strip(),
    )
    for key in lookup_keys:
        if key and key in configured:
            amount = safe_float(configured.get(key))
            return max(0.0, float(amount or 0.0))
    return 0.0


def _execution_wallet_balance(client: Any) -> dict[str, Any]:
    """Read the selected account wallet through the P1 execution lane.

    Third-party/test clients that predate the optional keyword keep working;
    the production Steam client receives the priority hint.
    """

    try:
        wallet = client.wallet_balance(execution_priority=True)
    except TypeError as exc:
        if "execution_priority" not in str(exc):
            raise
        wallet = client.wallet_balance()
    if not isinstance(wallet, dict):
        raise RuntimeError("Steam wallet response is not an object")
    return wallet


def _persist_shared_steam_wallet(
    settings: Settings,
    client: Any,
    wallet: dict[str, Any] | None,
    *,
    account: Account | None = None,
) -> None:
    """Best-effort merge into the balance cache shared with guadao."""

    if not isinstance(wallet, dict):
        return
    try:
        update_steam_account_balance_snapshot(
            settings,
            wallet=wallet,
            account=account,
            account_id=str(getattr(client, "account_id", "") or "") or None,
            account_name=(str(account.name or "").strip() if account is not None else None),
            steam_id64=str(getattr(client, "steam_id64", "") or "") or None,
        )
    except Exception:
        # A local dashboard-cache write must never change a real trade result.
        return


def _select_steam_buy_account(
    settings: Settings,
    *,
    required_balance: float,
    preferred_steam_id: str | None = None,
    account_store: AccountStore | None = None,
    account_reserved_balances: dict[str, float] | None = None,
    telemetry_context: dict[str, Any] | None = None,
) -> SteamBuyAccountSelection:
    """Choose the account/client from the shared guadao balance snapshot only."""

    store = account_store or AccountStore(PROJECT_ROOT / "config")
    preferred = str(preferred_steam_id or "").strip()
    accounts = [
        account
        for account in store.list_accounts()
        if account.cookies and account.steam_id64
    ]
    try:
        shared_snapshot = load_steam_account_balances(settings, account_store=store)
    except Exception:
        shared_snapshot = {"accounts": []}
    cached_rows: list[dict[str, Any]] = [
        row
        for row in shared_snapshot.get("accounts") or []
        if isinstance(row, dict)
    ]

    def cached_wallet_for(account: Account) -> dict[str, Any]:
        account_id = str(account.id or "").strip()
        steam_id = str(account.steam_id64 or "").strip()
        for row in cached_rows:
            if (
                account_id
                and str(row.get("id") or "").strip() == account_id
            ) or (
                steam_id
                and str(row.get("steamId") or "").strip() == steam_id
            ):
                return {
                    "balance": row.get("realBalance"),
                    "delayed_balance": row.get("pendingBalance"),
                    "currency": row.get("currency"),
                    "currency_id": row.get("currencyId"),
                }
        return {}

    preferred_account = next(
        (
            account
            for account in accounts
            if preferred and str(account.steam_id64 or "").strip() == preferred
        ),
        None,
    )
    chosen_account: Account | None = None
    if accounts:
        def cached_spendable(account: Account) -> tuple[float | None, float | None]:
            wallet = cached_wallet_for(account)
            currency_id = safe_int(wallet.get("currency_id"))
            currency_code = str(wallet.get("currency") or "").strip().upper()
            if (currency_id is not None and currency_id != 23) or (
                currency_id is None and currency_code and currency_code != "CNY"
            ):
                return None, None
            balance = safe_float(wallet.get("balance"))
            if balance is None:
                return None, None
            reserved = _account_reserved_balance(account, account_reserved_balances)
            return balance, max(0.0, float(balance) - reserved)

        if preferred_account is not None:
            _, preferred_spendable = cached_spendable(preferred_account)
            if (
                preferred_spendable is not None
                and preferred_spendable + 1e-9 >= required_balance
            ):
                chosen_account = preferred_account

        if chosen_account is None:
            cached_fallbacks: list[tuple[float, str, Account]] = []
            for account in accounts:
                if account is preferred_account:
                    continue
                balance, spendable = cached_spendable(account)
                if (
                    balance is not None
                    and spendable is not None
                    and spendable + 1e-9 >= required_balance
                ):
                    cached_fallbacks.append((float(balance), account.name, account))
            if cached_fallbacks:
                chosen_account = sorted(cached_fallbacks, key=lambda row: (row[0], row[1]))[0][2]

        # If the shared snapshot is missing or all cached balances are too low,
        # keep A as the preparation account. The last-moment live selector still
        # gets a chance to find real spendable balance before any buy request.
        if chosen_account is None:
            chosen_account = preferred_account or accounts[0]

    if chosen_account is not None:
        client = (
            _build_steam_client_for_account(
                settings,
                chosen_account,
                telemetry_context=telemetry_context,
            )
            if telemetry_context
            else _build_steam_client_for_account(settings, chosen_account)
        )
        wallet = cached_wallet_for(chosen_account)
        balance = safe_float(wallet.get("balance"))
        reserved = _account_reserved_balance(chosen_account, account_reserved_balances)
        spendable = max(0.0, float(balance) - reserved) if balance is not None else None
        return SteamBuyAccountSelection(
            account=chosen_account,
            client=client,
            wallet_balance=balance,
            reserved_balance=reserved,
            spendable_balance=spendable,
            wallet=wallet,
            wallet_is_live=False,
        )

    if settings.steam_cookies:
        client = (
            _build_steam_client(settings, telemetry_context=telemetry_context)
            if telemetry_context
            else _build_steam_client(settings)
        )
        reserved = _account_reserved_balance(
            None,
            account_reserved_balances,
            account_id=str(getattr(client, "account_id", "") or ""),
            steam_id64=str(getattr(client, "steam_id64", "") or ""),
        )
        return SteamBuyAccountSelection(
            account=None,
            client=client,
            wallet_balance=None,
            reserved_balance=reserved,
            spendable_balance=None,
            wallet={},
            wallet_is_live=False,
        )

    raise RuntimeError("no Steam account is available for Profit Trade purchase")


def _select_live_steam_buy_account(
    settings: Settings,
    *,
    required_balance: float,
    preferred_steam_id: str | None = None,
    account_store: AccountStore | None = None,
    account_reserved_balances: dict[str, float] | None = None,
    telemetry_context: dict[str, Any] | None = None,
) -> SteamBuyAccountSelection:
    """Verify the cache-selected account first, then check other accounts."""

    store = account_store or AccountStore(PROJECT_ROOT / "config")
    accounts = [
        account
        for account in store.list_accounts()
        if account.cookies and account.steam_id64
    ]
    preferred = str(preferred_steam_id or "").strip()
    preferred_account = next(
        (
            account
            for account in accounts
            if preferred and str(account.steam_id64 or "").strip() == preferred
        ),
        None,
    )
    fallback_accounts = [account for account in accounts if account is not preferred_account]
    errors: list[str] = []
    details: list[str] = []

    try:
        shared_snapshot = load_steam_account_balances(settings, account_store=store)
    except Exception:
        shared_snapshot = {"accounts": []}
    cached_rows = [
        row for row in shared_snapshot.get("accounts") or [] if isinstance(row, dict)
    ]

    def cached_balance(account: Account) -> float | None:
        for row in cached_rows:
            if str(row.get("id") or "").strip() == str(account.id or "").strip() or (
                str(account.steam_id64 or "").strip()
                and str(row.get("steamId") or "").strip()
                == str(account.steam_id64 or "").strip()
            ):
                return safe_float(row.get("realBalance"))
        return None

    def fallback_sort_key(account: Account) -> tuple[int, float, str]:
        balance = cached_balance(account)
        if balance is None:
            return (2, math.inf, account.name)
        reserved = _account_reserved_balance(account, account_reserved_balances)
        spendable = max(0.0, float(balance) - reserved)
        return (
            0 if spendable + 1e-9 >= required_balance else 1,
            float(balance),
            account.name,
        )

    ordered_accounts = (
        ([preferred_account] if preferred_account is not None else [])
        + sorted(fallback_accounts, key=fallback_sort_key)
    )
    checked_ids: set[str] = set()
    for account in ordered_accounts:
        checked_ids.add(str(account.id or "").strip())
        try:
            client = (
                _build_steam_client_for_account(
                    settings,
                    account,
                    telemetry_context=telemetry_context,
                )
                if telemetry_context
                else _build_steam_client_for_account(settings, account)
            )
            wallet = _execution_wallet_balance(client)
            _persist_shared_steam_wallet(settings, client, wallet, account=account)
            currency_id = safe_int(
                wallet.get("currency_id")
                if wallet.get("currency_id") is not None
                else wallet.get("currencyId")
            )
            currency_code = str(wallet.get("currency") or "").strip().upper()
            if (currency_id is not None and currency_id != 23) or (
                currency_id is None and currency_code != "CNY"
            ):
                raise RuntimeError("Steam wallet currency is not CNY")
            balance = safe_float(wallet.get("balance"))
            if balance is None:
                raise RuntimeError("Steam wallet response is missing balance")
            reserved = _account_reserved_balance(account, account_reserved_balances)
            spendable = max(0.0, float(balance) - reserved)
            details.append(
                f"{account.name}: balance CNY {balance:.2f}, "
                f"reserved CNY {reserved:.2f}, spendable CNY {spendable:.2f}"
            )
            if spendable + 1e-9 >= required_balance:
                return SteamBuyAccountSelection(
                    account=account,
                    client=client,
                    wallet_balance=balance,
                    reserved_balance=reserved,
                    spendable_balance=spendable,
                    wallet=wallet,
                    wallet_is_live=True,
                )
        except Exception as exc:
            errors.append(f"{account.name}: {exc}")

    fallback_account = store.get_current() if settings.steam_cookies else None
    if (
        settings.steam_cookies
        and (
            fallback_account is None
            or str(fallback_account.id or "").strip() not in checked_ids
        )
    ):
        client = (
            _build_steam_client(settings, telemetry_context=telemetry_context)
            if telemetry_context
            else _build_steam_client(settings)
        )
        wallet = _execution_wallet_balance(client)
        _persist_shared_steam_wallet(settings, client, wallet, account=fallback_account)
        currency_id = safe_int(
            wallet.get("currency_id")
            if wallet.get("currency_id") is not None
            else wallet.get("currencyId")
        )
        currency_code = str(wallet.get("currency") or "").strip().upper()
        if (currency_id is not None and currency_id != 23) or (
            currency_id is None and currency_code != "CNY"
        ):
            raise RuntimeError("Steam wallet currency is not CNY")
        balance = safe_float(wallet.get("balance"))
        if balance is not None:
            reserved = _account_reserved_balance(
                fallback_account,
                account_reserved_balances,
                account_id=str(getattr(client, "account_id", "") or ""),
                steam_id64=str(getattr(client, "steam_id64", "") or ""),
            )
            spendable = max(0.0, float(balance) - reserved)
            if spendable + 1e-9 >= required_balance:
                return SteamBuyAccountSelection(
                    account=fallback_account,
                    client=client,
                    wallet_balance=balance,
                    reserved_balance=reserved,
                    spendable_balance=spendable,
                    wallet=wallet,
                    wallet_is_live=True,
                )

    detail = "; ".join((details + errors)[:5])
    suffix = f" ({detail})" if detail else ""
    raise RuntimeError(
        f"no Steam account has enough live wallet balance for CNY {required_balance:.2f}{suffix}"
    )


def _require_profit_trade_real_execution(config: StrategyConfig) -> None:
    if not config.profit_trade_enabled:
        raise RuntimeError("profitTrade.enabled is false")
    if not config.profit_trade_allow_real_execution:
        raise RuntimeError("profitTrade.allowRealExecution is false")


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


def _profit_trade_buy_order_reference_values(
    *,
    c5_expected_net_price: Any,
    roi_basis: Any,
    orderbook_snapshot: dict[str, Any] | None,
) -> dict[str, float | str | None]:
    """Build diagnostic buy-order ROI values from an already fetched orderbook.

    This is deliberately an observation-only calculation.  It must never be
    used for real purchase pricing: a public highest buy order can be stale and
    a newly created buy order has no time priority over older matching orders.
    """

    snapshot = dict(orderbook_snapshot or {})
    buyer_max_price = safe_float(snapshot.get("buyerMaxPrice"))
    if (
        buyer_max_price is None
        or not math.isfinite(buyer_max_price)
        or buyer_max_price <= 0
    ):
        return {"roi": None, "profit": None, "status": "missing_buy_book"}

    currency_id = safe_int(snapshot.get("currencyId"))
    if snapshot.get("currencyValid") is False or currency_id != 23:
        return {"roi": None, "profit": None, "status": "currency_invalid"}

    c5_expected_net = safe_float(c5_expected_net_price)
    if (
        c5_expected_net is None
        or not math.isfinite(c5_expected_net)
        or c5_expected_net <= 0
    ):
        return {"roi": None, "profit": None, "status": "c5_price_unavailable"}

    basis = safe_float(roi_basis)
    if basis is None or not math.isfinite(basis) or basis <= 0:
        return {"roi": None, "profit": None, "status": "invalid_roi_basis"}

    reference_roi = _profit_trade_transfer_roi(
        c5_expected_net=float(c5_expected_net),
        steam_buy_price=float(buyer_max_price),
        steam_cost_ratio=float(basis),
    )
    if reference_roi is None:
        return {"roi": None, "profit": None, "status": "missing_buy_book"}
    reference_profit = float(c5_expected_net) - float(buyer_max_price) * float(basis)
    status = "crossed_possible_stale" if snapshot.get("crossed") is True else "valid"
    return {
        "roi": float(reference_roi),
        "profit": float(reference_profit),
        "status": status,
    }


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

    return C5RecentSaleRisk(
        recent_sold_net_price=recent_net,
        recent_sold_count=count,
        status="passed",
        reason="C5 recent-sale liquidity risk passed; history does not cap the live listing reference",
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
    listing_depth = risk.raw.get("listingDepth") if isinstance(risk.raw, dict) else None
    low_confidence_reference = (
        isinstance(listing_depth, dict)
        and str(listing_depth.get("referenceConfidence") or "").lower() == "low"
    )
    # The agreed no-wall fallback is still a usable live competitor reference.
    # It may consist of a single external listing, so lack of a stable wall must
    # not be converted back into an automatic seller-depth rejection.  Purchase
    # side and all other risk gates remain unchanged.
    required_on_sale_count = 1 if low_confidence_reference else min_count
    if on_sale_count < required_on_sale_count:
        return C5RecentSaleRisk(
            recent_sold_net_price=risk.recent_sold_net_price,
            recent_sold_count=risk.recent_sold_count,
            status="blocked_low_c5_listing_depth",
            reason=f"C5 on-sale count {on_sale_count} < {required_on_sale_count}",
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
        or "可能已被移除" in text
        or "请刷新页面并重试" in text
        or "already purchased" in text
        or "someone else has already purchased" in text
        or "because another user has purchased" in text
        or "item may have been removed" in text
        or "please refresh the page and try again" in text
        or "problem purchasing your item" in text
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


class _SearchListingsFallbackRequired(RuntimeError):
    def __init__(
        self,
        *,
        last_error: SteamMarketError,
        attempts: int,
        events: list[dict[str, Any]],
        status_code: int,
        reason: str,
    ) -> None:
        super().__init__(reason)
        self.last_error = last_error
        self.attempts = attempts
        self.events = list(events)
        self.status_code = int(status_code)


class _SearchListings429Exhausted(_SearchListingsFallbackRequired):
    def __init__(
        self,
        *,
        last_error: SteamMarketError,
        attempts: int,
        events: list[dict[str, Any]],
    ) -> None:
        super().__init__(
            last_error=last_error,
            attempts=attempts,
            events=events,
            status_code=429,
            reason=(
                f"Steam listings search remained rate limited after {attempts} "
                "attempts (HTTP 429)"
            ),
        )


def _search_profit_trade_listings_once(
    *,
    settings: Settings,
    config: StrategyConfig,
    client: Any,
    market_hash_name: str,
    orderbook_payload: dict[str, Any],
    orderbook_buy_target: SteamBuyTarget,
    telemetry_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], SteamBuyTarget, list[dict[str, Any]]]:
    try:
        listings_payload = client.search_listings(
            app_id=settings.app_id,
            market_hash_name=market_hash_name,
            start=0,
            count=10,
            currency=config.steam_currency,
            country=config.steam_country,
            language=config.steam_language,
            bounded_retry=False,
            auth_retry=False,
        )
    except SteamMarketError as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code not in {400, 429}:
            raise
        event = {
            "attempt": 1,
            "maxAttempts": 1,
            "failedAt": utc_now_iso(),
            "statusCode": int(status_code),
            "retryAfter": getattr(exc, "retry_after", None),
            "waitSeconds": 0.0,
            "delaySource": (
                "no_local_retry"
                if status_code == 429
                else "direct_safe_buy_order_fallback"
            ),
        }
        get_profit_trade_event_logger().emit(
            level="WARN",
            provider="local",
            component="profit_trade_buy",
            operation=f"search_listings_{status_code}_buy_order_fallback_required",
            message=(
                "Steam listings search was rate limited; no same-account short retry will be made"
                if status_code == 429
                else (
                    "Steam listings returned HTTP 400; no proactive Cookie validation or "
                    "relogin will be started, and the safe buy-order path is required"
                )
            ),
            **telemetry_context,
            safe_context=event,
        )
        if status_code == 429:
            raise _SearchListings429Exhausted(
                last_error=exc,
                attempts=1,
                events=[event],
            ) from exc
        raise _SearchListingsFallbackRequired(
            last_error=exc,
            attempts=1,
            events=[event],
            status_code=400,
            reason=(
                "Steam listings search returned HTTP 400; switching directly to the safe "
                "buy-order fallback without proactive Cookie validation"
            ),
        ) from exc
    return (
        listings_payload,
        orderbook_payload,
        orderbook_buy_target,
        [],
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


def _probe_crossed_orderbook_listing(
    *,
    settings: Settings,
    config: StrategyConfig,
    db: Database,
    client: Any,
    market_hash_name: str,
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    """Capture one bounded listing witness for a crossed public orderbook.

    The witness is diagnostic only.  It never buys, reserves, retries or changes
    ROI/execution decisions.  The buyer-visible total is compared in integer CNY
    cents because Steam's public sell floor is the amount the buyer pays.
    """

    if snapshot.get("crossed") is not True:
        return None

    checked_at = utc_now_iso()
    seller_floor = safe_float(snapshot.get("sellerFloorPrice"))
    currency_id = safe_int(snapshot.get("currencyId"))
    base: dict[str, Any] = {
        "checkedAt": checked_at,
        "attemptedAt": None,
        "marketHashName": str(market_hash_name or "").strip(),
        "expectedSellerFloorPrice": seller_floor,
        "currencyId": currency_id,
        "listingId": None,
        "listingSubtotal": None,
        "listingFee": None,
        "listingTotal": None,
        "listingCurrencyId": None,
        "priceMatchesFloor": None,
        "candidateCount": 0,
        "purchaseAttempted": False,
    }
    if snapshot.get("currencyValid") is False or currency_id != 23:
        return {
            **base,
            "status": "currency_invalid",
            "message": "盘口不是人民币 CNY（currencyId=23），未查询具体 listing",
        }
    if seller_floor is None or seller_floor <= 0:
        return {
            **base,
            "status": "floor_unavailable",
            "message": "交叉盘口缺少有效卖一，未查询具体 listing",
        }
    circuit = _get_profit_trade_listings_circuit(db)
    if circuit["status"] == "open":
        return {
            **base,
            "status": "circuit_open",
            "message": "Steam listings 冷却中，本次未抓取具体 listing",
            "circuitCooldownUntil": circuit.get("cooldownUntil"),
        }
    base["attemptedAt"] = checked_at
    try:
        payload = client.search_listings(
            app_id=settings.app_id,
            market_hash_name=market_hash_name,
            start=0,
            count=10,
            currency=23,
            country=config.steam_country,
            language=config.steam_language,
            bounded_retry=False,
        )
    except SteamMarketError as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            circuit = _open_profit_trade_listings_circuit_for_observation(
                db,
                settings,
                market_hash_name=market_hash_name,
                client=client,
                retry_after=getattr(exc, "retry_after", None),
            )
            return {
                **base,
                "status": "rate_limited",
                "httpStatus": 429,
                "message": "具体 listing 查询返回 429；未重试，已进入 listings 冷却",
                "circuitCooldownUntil": circuit.get("cooldownUntil"),
            }
        return {
            **base,
            "status": "unavailable",
            "httpStatus": status_code,
            "message": (
                f"具体 listing 查询失败（HTTP {status_code}）"
                if status_code is not None
                else "具体 listing 查询失败"
            ),
        }
    except Exception:
        return {
            **base,
            "status": "error",
            "message": "具体 listing 查询发生异常，未影响观察或交易状态",
        }

    if not isinstance(payload, dict):
        return {
            **base,
            "status": "unavailable",
            "message": "具体 listing 查询返回了不可识别的数据",
        }
    raw_listinginfo = payload.get("listinginfo") or payload.get("listings") or {}
    rows: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw_listinginfo, dict):
        rows = [
            (str(raw_id), row)
            for raw_id, row in raw_listinginfo.items()
            if isinstance(row, dict)
        ]
    elif isinstance(raw_listinginfo, list):
        rows = [
            (str(row.get("listingid") or ""), row)
            for row in raw_listinginfo
            if isinstance(row, dict)
        ]
    base["candidateCount"] = len(rows)
    target = _pick_lowest_steam_listing_buy_target(
        payload,
        market_hash_name=market_hash_name,
        currency=23,
    )
    if target is None:
        return {
            **base,
            "status": "empty" if not rows else "no_usable_cny_listing",
            "message": (
                "没有找到任何公开 listing；该卖一更像幽灵卖盘或尚未传播到 listings"
                if not rows
                else "返回了 listing，但没有可验证的人民币同物品卖单"
            ),
        }

    target_row: dict[str, Any] = {}
    for raw_id, row in rows:
        if str(row.get("listingid") or raw_id or "").strip() == target.listing_id:
            target_row = row
            break
    listing_currency_id = safe_int(
        target_row.get("converted_currencyid")
        or target_row.get("currencyid")
        or target_row.get("eCurrency")
        or target_row.get("currency")
    )
    expected_cents = int(round(float(seller_floor) * 100.0))
    price_matches = int(target.total) == expected_cents
    result = {
        **base,
        "status": "matched" if price_matches else "floor_mismatch",
        "listingId": target.listing_id,
        "listingSubtotal": target.subtotal / 100.0,
        "listingFee": target.fee / 100.0,
        "listingTotal": target.total / 100.0,
        "listingCurrencyId": listing_currency_id,
        "priceMatchesFloor": price_matches,
        "message": (
            "已找到与交叉卖一一致的具体 listing"
            if price_matches
            else "已找到具体 listing，但实际买家支付价与交叉卖一不一致"
        ),
    }
    get_profit_trade_event_logger().emit(
        provider="steam",
        component="profit_trade_orderbook",
        operation="crossed_listing_probe",
        message=str(result["message"]),
        market_hash_name=str(market_hash_name or "").strip() or None,
        account_id=str(getattr(client, "account_id", "") or "").strip() or None,
        steam_id64=str(getattr(client, "steam_id64", "") or "").strip() or None,
        safe_context=result,
    )
    return result


def _attach_crossed_listing_probe(
    *,
    settings: Settings,
    config: StrategyConfig,
    db: Database,
    market_service: MarketService,
    state: MarketState,
) -> dict[str, Any] | None:
    raw = state.raw_json if isinstance(state.raw_json, dict) else {}
    state.raw_json = raw
    if "crossed_listing_probe" in raw:
        saved = raw.get("crossed_listing_probe")
        return dict(saved) if isinstance(saved, dict) else None
    snapshot = raw.get("steam_orderbook_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("crossed") is not True:
        return None
    clients = list(getattr(market_service, "steam_market_clients", []) or [])
    client_index = safe_int(raw.get("steam_orderbook_client_index"))
    client = (
        clients[client_index]
        if client_index is not None and 0 <= client_index < len(clients)
        else (clients[0] if clients else None)
    )
    if client is None:
        probe = {
            "checkedAt": utc_now_iso(),
            "attemptedAt": None,
            "status": "client_unavailable",
            "marketHashName": state.market_hash_name,
            "expectedSellerFloorPrice": safe_float(snapshot.get("sellerFloorPrice")),
            "currencyId": safe_int(snapshot.get("currencyId")),
            "purchaseAttempted": False,
            "message": "Steam listings 客户端不可用，本次未抓取具体 listing",
        }
    else:
        probe = _probe_crossed_orderbook_listing(
            settings=settings,
            config=config,
            db=db,
            client=client,
            market_hash_name=state.market_hash_name,
            snapshot=snapshot,
        )
    if probe is not None:
        raw["crossed_listing_probe"] = probe
    return probe


def _compact_steam_orderbook_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return build_orderbook_snapshot(payload or {}, depth=5, expected_currency=23)


def _orderbook_log_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: snapshot.get(key)
        for key in (
            "observedAt",
            "currencyId",
            "currencyValid",
            "sellerFloorPrice",
            "sellerFloorCount",
            "buyerMaxPrice",
            "buyerMaxCount",
            "spreadAmount",
            "spreadPct",
            "crossed",
            "sellOrderCountTotal",
            "buyOrderCountTotal",
        )
    }


def _orderbook_log_message(snapshot: dict[str, Any]) -> str:
    def price(value: Any) -> str:
        amount = safe_float(value)
        return f"¥{amount:.2f}" if amount is not None else "未记录"

    message = (
        f"Steam 买入 {price(snapshot.get('sellerFloorPrice'))}｜"
        f"Steam 最高求购 {price(snapshot.get('buyerMaxPrice'))}"
    )
    if snapshot.get("crossed") is True:
        message += "｜盘口交叉，可能滞后"
    return message


def _record_profit_trade_orderbook_snapshot(
    payload: dict[str, Any],
    *,
    stage: str,
    expected_currency: int = 23,
    telemetry_context: dict[str, Any] | None = None,
    db: Database | None = None,
    trade_id: int | None = None,
) -> dict[str, Any]:
    """Persist and log one already-fetched Profit Trade orderbook snapshot."""

    snapshot = build_orderbook_snapshot(
        payload or {},
        observed_at=utc_now_iso(),
        depth=5,
        expected_currency=expected_currency,
    )
    if snapshot.get("currencyValid") is False:
        raise SteamMarketError(
            "Steam orderbook currency mismatch: "
            f"expected={expected_currency} actual={snapshot.get('currencyId')}"
        )
    context = dict(telemetry_context or {})
    get_profit_trade_event_logger().emit(
        provider="steam",
        component="profit_trade_orderbook",
        operation="orderbook_snapshot",
        message=_orderbook_log_message(snapshot),
        **context,
        safe_context={
            "stage": str(stage or "unknown"),
            "steam_orderbook": _orderbook_log_summary(snapshot),
        },
    )
    if db is not None and trade_id is not None:
        current = db.get_profit_trade(int(trade_id))
        if current is not None:
            note = _read_note(current["note"])
            snapshots = [
                item
                for item in list(note.get("executionOrderbookSnapshots") or [])
                if isinstance(item, dict)
            ]
            snapshots.append({"stage": str(stage or "unknown"), **snapshot})
            note["executionOrderbookSnapshots"] = snapshots[-30:]
            db.update_profit_trade(int(trade_id), note=_build_note(note))
            db.add_profit_trade_audit_event(
                int(trade_id),
                event_type="orderbook_snapshot",
                reason=(
                    "Steam 盘口交叉，公开卖一可能滞后"
                    if snapshot.get("crossed") is True
                    else "Steam 执行盘口已记录"
                ),
                context={
                    "stage": str(stage or "unknown"),
                    "steamOrderbook": _public_profit_trade_orderbook_snapshot(
                        snapshot,
                        stage=str(stage or "unknown"),
                    ),
                },
            )
    return snapshot


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

    def call_safety_terminal(method: Any, **kwargs: Any) -> Any:
        try:
            return method(**kwargs, safety_terminal=True)
        except TypeError as exc:
            # Test doubles and third-party adapters may not yet expose this
            # optional scheduler hint. Preserve unrelated TypeErrors.
            if "safety_terminal" not in str(exc):
                raise
            return method(**kwargs)

    for attempt in range(max(1, int(attempts))):
        reasons = []
        verified_by = []
        active_buy_orders = []

        try:
            wallet_after = call_safety_terminal(client.wallet_balance)
            after_balance = safe_float(wallet_after.get("balance")) if isinstance(wallet_after, dict) else None
            if wallet_before_balance is not None and after_balance is not None:
                wallet_delta = round(float(wallet_before_balance) - float(after_balance), 2)
                if abs(wallet_delta - expected_delta) <= 0.02:
                    verified_by.append("wallet_balance_delta")
                elif (
                    method == "createbuyorder"
                    and wallet_delta > 0
                    and wallet_delta <= expected_delta + 0.02
                ):
                    # A Steam buy-order price is the maximum authorized spend.
                    # Matching a cheaper seller listing legitimately deducts
                    # less from the wallet and still proves a filled order once
                    # the tracked buy order is no longer active.
                    verified_by.append("wallet_balance_delta_within_buy_order_max")
                else:
                    relation = "exceeds" if wallet_delta > expected_delta else "does not prove"
                    reasons.append(
                        f"wallet delta {wallet_delta:.2f} {relation} authorized total {expected_delta:.2f}"
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

        # An exact wallet delta is strong enough to pair with the tracked
        # order state. A lower delta is valid for createbuyorder, but because
        # other account activity can also change the wallet it remains
        # provisional until a same-item inventory asset or official purchase
        # receipt confirms it.
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
        c5_client = _build_profit_trade_c5_client(
            settings,
            steam_id64=target_steam_id,
        )
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


def _iso_timestamp(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _find_official_steam_purchase_receipt(
    client: Any,
    *,
    market_hash_name: str,
    expected_total: int,
    purchase_requested_at: str | None,
    actual_total_hint: float | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not hasattr(client, "find_purchase_receipt"):
        return None, None
    try:
        kwargs = {
            "market_hash_name": market_hash_name,
            # createbuyorder price is a maximum. Steam charges the matched
            # seller listing price, which may be lower than this value.
            "maximum_total": max(0, int(expected_total)) / 100.0,
            "earliest_time": _iso_timestamp(purchase_requested_at),
            "count": 100,
            "max_pages": 2,
        }
        if actual_total_hint is not None and actual_total_hint > 0:
            kwargs["expected_total"] = float(actual_total_hint)
        try:
            receipt = client.find_purchase_receipt(**kwargs, safety_terminal=True)
        except TypeError as exc:
            if "safety_terminal" not in str(exc):
                raise
            receipt = client.find_purchase_receipt(**kwargs)
    except Exception as exc:
        return None, f"Steam purchase history verification failed: {exc}"
    return (receipt if isinstance(receipt, dict) else None), None


def _verification_from_purchase_receipt(
    latest: SteamBuyVerification,
    receipt: dict[str, Any],
) -> SteamBuyVerification:
    new_asset_id = str(receipt.get("newAssetId") or "").strip()
    verified_by = list(dict.fromkeys([*latest.verified_by, "market_history_event_type_4"]))
    return SteamBuyVerification(
        confirmed=True,
        wallet_after=latest.wallet_after,
        wallet_delta=latest.wallet_delta,
        active_buy_orders=latest.active_buy_orders,
        verified_by=verified_by,
        inventory_after_asset_ids=latest.inventory_after_asset_ids,
        new_inventory_asset_ids=[new_asset_id] if new_asset_id else latest.new_inventory_asset_ids,
        purchase_receipt=receipt,
    )


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
    purchase_requested_at: str | None = None,
    check_purchase_history: bool = True,
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
        _persist_shared_steam_wallet(settings, client, latest.wallet_after)
        if latest.confirmed:
            return latest

        if method == "createbuyorder" and any(
            value in latest.verified_by
            for value in (
                "wallet_balance_delta",
                "wallet_balance_delta_within_buy_order_max",
            )
        ):
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

    if method == "createbuyorder" and check_purchase_history:
        purchase_receipt, purchase_history_error = _find_official_steam_purchase_receipt(
            client,
            market_hash_name=market_hash_name,
            expected_total=expected_total,
            purchase_requested_at=purchase_requested_at,
            actual_total_hint=latest.wallet_delta,
        )
        if purchase_receipt is not None:
            return _verification_from_purchase_receipt(latest, purchase_receipt)
        if purchase_history_error:
            latest.reason = "; ".join(
                reason for reason in (latest.reason, purchase_history_error) if reason
            )

    return latest


def _cancel_and_resolve_steam_buy_order(
    client: Any,
    *,
    market_hash_name: str,
    expected_total: int,
    wallet_before_balance: float | None,
    buy_order_id: str,
    purchase_requested_at: str | None,
    actual_total_hint: float | None = None,
) -> SteamBuyOrderResolution:
    cancel_payload: dict[str, Any] | None = None
    cancel_error: str | None = None
    if not hasattr(client, "cancel_buy_order"):
        cancel_error = "Steam client cannot cancel buy orders"
    else:
        try:
            payload = client.cancel_buy_order(buy_order_id=buy_order_id)
            cancel_payload = payload if isinstance(payload, dict) else None
        except Exception as exc:
            cancel_error = str(exc)

    latest = SteamBuyVerification(
        confirmed=False,
        wallet_after=None,
        wallet_delta=None,
        active_buy_orders=[],
        verified_by=[],
        reason="Steam buy-order terminal state could not be verified",
    )
    attempts = max(1, int(STEAM_BUY_CANCEL_VERIFY_ATTEMPTS))
    for attempt in range(attempts):
        latest = _verify_steam_buy_completed(
            client,
            market_hash_name=market_hash_name,
            method="createbuyorder",
            expected_total=expected_total,
            wallet_before_balance=wallet_before_balance,
            buy_order_id=buy_order_id,
            attempts=1,
            delay_seconds=0.0,
        )
        if latest.confirmed:
            return SteamBuyOrderResolution(
                outcome="purchased",
                verification=latest,
                cancel_payload=cancel_payload,
                cancel_error=cancel_error,
            )

        order_is_absent = "no_active_matching_buy_order" in latest.verified_by
        if order_is_absent or attempt + 1 >= attempts:
            purchase_receipt, purchase_history_error = _find_official_steam_purchase_receipt(
                client,
                market_hash_name=market_hash_name,
                expected_total=expected_total,
                purchase_requested_at=purchase_requested_at,
                actual_total_hint=actual_total_hint or latest.wallet_delta,
            )
            if purchase_receipt is not None:
                return SteamBuyOrderResolution(
                    outcome="purchased",
                    verification=_verification_from_purchase_receipt(latest, purchase_receipt),
                    cancel_payload=cancel_payload,
                    cancel_error=cancel_error,
                )
            if purchase_history_error:
                latest.reason = "; ".join(
                    reason for reason in (latest.reason, purchase_history_error) if reason
                )
            lower_price_fill_evidence = (
                "wallet_balance_delta_within_buy_order_max" in latest.verified_by
            )
            if (
                order_is_absent
                and cancel_error is None
                and purchase_history_error is None
                and not lower_price_fill_evidence
            ):
                return SteamBuyOrderResolution(
                    outcome="cancelled",
                    verification=latest,
                    cancel_payload=cancel_payload,
                )
            if attempt + 1 >= attempts:
                break
        time.sleep(max(0.0, float(STEAM_BUY_CANCEL_VERIFY_DELAY_SECONDS)))

    return SteamBuyOrderResolution(
        outcome="uncertain",
        verification=latest,
        cancel_payload=cancel_payload,
        cancel_error=cancel_error,
    )


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
    requested = sorted({str(value).strip() for value in steam_ids or [] if str(value).strip()})
    if not requested:
        return ActiveC5SaleLookup(active_ids=_load_active_c5_sale_ids_page(c5_client, settings))

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
        raise RuntimeError("; ".join(errors) or "C5 active sale lookup did not cover any Steam account")
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


def _c5_seller_order_asset_ids(row: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("assetId", "asset_id", "originalAssetId", "original_asset_id"):
        value = row.get(key)
        if value not in (None, ""):
            values.add(str(value).strip())
    asset_info = row.get("assetInfo") if isinstance(row.get("assetInfo"), dict) else {}
    for key in ("assetId", "asset_id", "originalAssetId", "original_asset_id"):
        value = asset_info.get(key)
        if value not in (None, ""):
            values.add(str(value).strip())
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    order_asset = detail.get("orderAsset") if isinstance(detail.get("orderAsset"), dict) else {}
    detail_asset_info = order_asset.get("assetInfo") if isinstance(order_asset.get("assetInfo"), dict) else {}
    for key in ("assetId", "asset_id", "originalAssetId", "original_asset_id"):
        value = detail_asset_info.get(key) or order_asset.get(key)
        if value not in (None, ""):
            values.add(str(value).strip())
    return {value for value in values if value}


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
    detail_net = _first_float(detail, ("getMoney", "get_money", "sellerGetMoney"))
    list_price = _first_float(row, ("price", "getMoney", "get_money", "sellerPrice"))
    if detail_net is not None and detail_net > 0:
        if list_price is not None and list_price > 0:
            ratio = detail_net / list_price
            if ratio < 0.5 or ratio > 1.2:
                return list_price, "seller_order_list_price_detail_net_outlier"
        return detail_net, "seller_order_detail_get_money"
    if list_price is not None and list_price > 0:
        return list_price, "seller_order_list_price"
    detail_price = _first_float(detail, ("price",))
    if detail_price is not None and detail_price > 0:
        return detail_price, "seller_order_detail_price"
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
        return C5SellerOrderLookup(sold_orders_by_product_id={}, sold_orders_by_asset_id={}, covered_steam_ids=set(), errors=["C5 seller order API unavailable"])

    product_ids_by_steam: dict[str, set[str]] = {}
    asset_ids_by_steam: dict[str, set[str]] = {}
    for row in rows:
        note = _read_note(row["note"])
        product_id = str(row["c5_product_id"] or note.get("c5ProductId") or "").strip()
        steam_id = str(row["a_steam_id"] or note.get("steamId") or "").strip()
        asset_id = str(row["a_asset_id"] or note.get("assetId") or "").strip()
        if product_id and steam_id:
            product_ids_by_steam.setdefault(steam_id, set()).add(product_id)
        if asset_id and steam_id:
            asset_ids_by_steam.setdefault(steam_id, set()).add(asset_id)

    sold_orders: dict[str, dict[str, Any]] = {}
    sold_orders_by_asset_id: dict[str, dict[str, Any]] = {}
    covered_steam_ids: set[str] = set()
    errors: list[str] = []
    detail_fetcher = getattr(c5_client, "seller_order_detail", None)
    for steam_id in sorted(set(product_ids_by_steam) | set(asset_ids_by_steam)):
        wanted_product_ids = product_ids_by_steam.get(steam_id, set())
        wanted_asset_ids = asset_ids_by_steam.get(steam_id, set())
        found_for_steam: set[str] = set()
        found_assets_for_steam: set[str] = set()
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
                    order_asset_ids = _c5_seller_order_asset_ids(order_row)
                    product_matches = bool(product_id and product_id in wanted_product_ids)
                    asset_matches = order_asset_ids & wanted_asset_ids
                    if not product_matches and not asset_matches:
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
                            order_asset_ids |= _c5_seller_order_asset_ids(merged)
                    if product_matches and product_id and product_id not in sold_orders:
                        sold_orders[product_id] = merged
                        found_for_steam.add(product_id)
                    for asset_id in order_asset_ids & wanted_asset_ids:
                        if asset_id not in sold_orders_by_asset_id:
                            sold_orders_by_asset_id[asset_id] = merged
                        found_assets_for_steam.add(asset_id)

                total = safe_int(payload.get("total")) if isinstance(payload, dict) else None
                if wanted_product_ids.issubset(found_for_steam) and wanted_asset_ids.issubset(found_assets_for_steam):
                    break
                if len(order_rows) < limit:
                    break
                if total is not None and page * limit >= total:
                    break
                page += 1
        if wanted_product_ids.issubset(found_for_steam) and wanted_asset_ids.issubset(found_assets_for_steam):
            continue

    return C5SellerOrderLookup(
        sold_orders_by_product_id=sold_orders,
        sold_orders_by_asset_id=sold_orders_by_asset_id,
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
        if str(note.get("recordOrigin") or "") == "manual_backfill":
            continue
        bought_at_value, _ = _profit_trade_steam_bought_at(note)
        bought_at = _parse_iso(bought_at_value)
        if bought_at is None:
            continue
        if bought_at.tzinfo is None:
            bought_at = bought_at.replace(tzinfo=timezone.utc)
        if start <= bought_at.astimezone(timezone.utc) < end:
            total += safe_float(row["steam_buy_price"]) or safe_float(note.get("steamBuyPrice")) or 0.0
    return total


def _profit_trade_daily_steam_committed_through(
    db: Database,
    *,
    trade_id: int,
    now: datetime | None = None,
) -> float:
    """Count confirmed spend plus deterministic in-flight buy reservations.

    `buying` rows are ordered by trade id.  When concurrent workers compete
    for the remaining daily budget, an earlier trade keeps priority and a
    later trade must include every earlier in-flight amount before its Steam
    HTTP callback may run.
    """

    start, end = _beijing_day_bounds(now)
    total = _profit_trade_daily_steam_spent(db, now=now)
    for row in db.list_profit_trades(status="buying", limit=5000):
        row_id = int(row["id"])
        if row_id > int(trade_id):
            continue
        note = _read_note(row["note"])
        bought_at_value, _ = _profit_trade_steam_bought_at(note)
        if bought_at_value:
            continue
        committed_at = _parse_iso(
            note.get("steamBuyRequestedAt") or row["updated_at"]
        )
        if committed_at is None:
            continue
        if committed_at.tzinfo is None:
            committed_at = committed_at.replace(tzinfo=timezone.utc)
        if start <= committed_at.astimezone(timezone.utc) < end:
            total += (
                safe_float(row["steam_buy_price"])
                or safe_float(note.get("steamBuyPrice"))
                or 0.0
            )
    return total


def _floor_cny_cent(value: float) -> float:
    """Round a positive CNY price down to the C5 cent tick."""

    return float(
        Decimal(str(max(0.01, float(value)))).quantize(
            Decimal("0.01"),
            rounding=ROUND_DOWN,
        )
    )


def _profit_trade_discounted_reference_price(reference_price: float, discount_pct: float) -> float:
    pct = Decimal(str(min(100.0, max(0.0, float(discount_pct)))))
    reference = Decimal(str(max(0.01, float(reference_price))))
    target = reference * (Decimal("1") - pct / Decimal("100"))
    return float(target.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _profit_trade_initial_listing_price(
    config: StrategyConfig,
    *,
    competitor_reference_price: float | None,
    fallback_price: float,
) -> float:
    reference = safe_float(competitor_reference_price)
    if reference is None or reference <= 0:
        reference = float(fallback_price)
    return _profit_trade_discounted_reference_price(
        reference,
        config.profit_trade_initial_listing_discount_pct,
    )


def _profit_trade_competitive_listing_price(
    config: StrategyConfig,
    *,
    current_lowest_price: float | None,
    fallback_price: float,
) -> float:
    lowest = safe_float(current_lowest_price)
    if lowest is None or lowest <= 0:
        return _floor_cny_cent(float(fallback_price))
    target = _profit_trade_discounted_reference_price(
        lowest,
        config.profit_trade_reprice_discount_pct,
    )
    return _floor_cny_cent(min(float(fallback_price), target))


def _profit_trade_stale_listing_age_hours(note: dict[str, Any], *, now: datetime | None = None) -> float | None:
    listed_at = _parse_iso(note.get("c5FirstListedAt") or note.get("c5ListedAt"))
    if listed_at is None:
        return None
    if listed_at.tzinfo is None:
        listed_at = listed_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current.astimezone(timezone.utc) - listed_at.astimezone(timezone.utc)).total_seconds() / 3600.0)


def _profit_trade_min_roi_at_open(config: StrategyConfig, note: dict[str, Any]) -> tuple[float, str]:
    frozen = safe_float(note.get("minRoiAtOpen"))
    if frozen is not None and frozen >= 0:
        return float(frozen), str(note.get("minRoiAtOpenSource") or "trade_note")
    return max(0.0, float(config.profit_trade_min_roi)), "legacy_runtime_fallback"


def _profit_trade_roi_gate_value(value: float) -> float:
    """Normalize ROI comparisons to the user-approved four-decimal policy."""

    return round(float(value), 4)


def _profit_trade_stale_roi_factor_at_open(config: StrategyConfig, note: dict[str, Any]) -> float:
    frozen = safe_float(note.get("staleMinRoiFactorAtOpen"))
    if frozen is not None and frozen >= 0:
        return float(frozen)
    return max(0.0, float(config.profit_trade_stale_min_roi_factor))


def _profit_trade_reprice_cooldown_passed(
    config: StrategyConfig,
    note: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    cooldown_hours = max(0.0, float(config.profit_trade_reprice_cooldown_hours))
    if cooldown_hours <= 0:
        return True, None
    anchor = _parse_iso(
        note.get("lastRepriceAt")
        or note.get("repriceAt")
        or note.get("c5FirstListedAt")
        or note.get("c5ListedAt")
    )
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


def _c5_price_batch_reference(
    *,
    price: float,
    count: int | None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the only C5 lowest-price reference accepted by Profit Trade."""

    normalized_price = safe_float(price)
    if normalized_price is None or normalized_price <= 0:
        raise RuntimeError("C5 price_batch returned no usable lowest sell price")
    normalized_count = max(0, int(count or 0))
    return {
        "lowestPrice": float(normalized_price),
        "rawLowestPrice": float(normalized_price),
        "effectiveReferencePrice": float(normalized_price),
        "referenceSource": "c5_price_batch_lowest",
        "referenceConfidence": "direct",
        "secondLowestPrice": None,
        "onSaleCount": normalized_count,
        "sampleCount": normalized_count,
        # price_batch is aggregated and has no productId.  Own listings are
        # guarded later by matching its minimum against known local listings;
        # claiming that any exact row was removed here would be false.
        "excludedOwnListingCount": None,
        "ownListingExclusionMode": "known_own_price_match_guard",
        "rows": [],
        "source": "c5_price_batch",
        "priceBatch": dict(raw or {}),
    }


def _fetch_c5_price_batch_references(
    c5_client: Any,
    settings: Settings,
    *,
    market_hash_names: list[str],
) -> dict[str, dict[str, Any]]:
    names = sorted({str(name).strip() for name in market_hash_names if str(name).strip()})
    if not names:
        return {}
    price_fetcher = getattr(c5_client, "price_batch", None)
    if not callable(price_fetcher):
        raise RuntimeError("C5 client does not support price_batch")
    payload = price_fetcher(names, app_id=settings.app_id)
    if not isinstance(payload, dict):
        raise RuntimeError("C5 price_batch returned an invalid payload")

    references: dict[str, dict[str, Any]] = {}
    for market_hash_name in names:
        row = payload.get(market_hash_name)
        if not isinstance(row, dict):
            continue
        price = _first_float(
            row,
            ("price", "sellPrice", "salePrice", "lowestSellPrice", "minSellPrice"),
        )
        if price is None or price <= 0:
            continue
        count = _first_int(
            row,
            ("count", "onSaleCount", "sellCount", "listingCount", "saleCount"),
        )
        references[market_hash_name] = _c5_price_batch_reference(
            price=float(price),
            count=count,
            raw=row,
        )
    return references


def _fetch_c5_price_batch_reference(
    c5_client: Any,
    settings: Settings,
    *,
    market_hash_name: str,
) -> dict[str, Any]:
    reference = _fetch_c5_price_batch_references(
        c5_client,
        settings,
        market_hash_names=[market_hash_name],
    ).get(market_hash_name)
    if reference is None:
        raise RuntimeError(f"C5 price_batch returned no matching item: {market_hash_name}")
    return reference


def _merge_c5_listing_depth_and_statistics(
    *,
    depth: dict[str, Any],
    statistics: C5RecentSaleRisk | None,
) -> C5RecentSaleRisk:
    lowest = safe_float(depth.get("effectiveReferencePrice") or depth.get("lowestPrice"))
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
    live_depth = str(depth.get("source") or "") == "c5_price_batch"
    return C5RecentSaleRisk(
        recent_sold_net_price=statistics.recent_sold_net_price,
        recent_sold_count=statistics.recent_sold_count,
        status=statistics.status,
        reason=statistics.reason,
        raw={"statistics": statistics.raw, "listingDepth": depth},
        current_sell_price=(
            lowest
            if live_depth or lowest is not None
            else statistics.current_sell_price
        ),
        on_sale_count=(
            depth_count
            if live_depth or depth_count is not None
            else statistics.on_sale_count
        ),
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
    lowest = safe_float(depth.get("effectiveReferencePrice") or depth.get("lowestPrice"))
    count = safe_int(depth.get("onSaleCount")) or 0
    sample_count = safe_int(depth.get("sampleCount")) or 0
    min_count = max(0, int(config.profit_trade_c5_min_on_sale_count))
    if str(depth.get("referenceConfidence") or "").lower() == "low":
        min_count = 1
    if lowest is None or lowest <= 0:
        return False, "C5 orderbook has no usable lowest sell price"
    if count < min_count or sample_count < min_count:
        return False, f"C5 orderbook depth too low: sale={count}, sample={sample_count}, min={min_count}"
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


def _list_executable_sell_assets(
    db: Database,
    config: StrategyConfig,
    inventory_items: list[dict[str, Any]],
    *,
    market_hash_name: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
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
        result.append(item)
        if limit is not None and len(result) >= max(1, int(limit)):
            break
    return result


def _pick_sell_asset(
    db: Database,
    config: StrategyConfig,
    inventory_items: list[dict[str, Any]],
    *,
    market_hash_name: str,
) -> dict[str, Any] | None:
    items = _list_executable_sell_assets(
        db,
        config,
        inventory_items,
        market_hash_name=market_hash_name,
        limit=1,
    )
    return items[0] if items else None


def _watch_eligible_market_hash_names(
    config: StrategyConfig,
    inventory_items: list[dict[str, Any]],
) -> set[str]:
    """Return types with a real, tradable, unprotected C5 asset.

    Observation does not reserve the asset and does not require sale tokens.
    Those remain execution-only gates in ``_pick_sell_asset``.
    """

    result: set[str] = set()
    for item in inventory_items:
        market_hash_name = str(item.get("marketHashName") or "").strip()
        if not market_hash_name:
            continue
        if not _inventory_item_key(item):
            continue
        if _inventory_item_protection_reason(config, item):
            continue
        if _is_tradable_c5_item(item):
            result.add(market_hash_name)
    return result


def _state_price_is_usable_for_profit_trade(state: MarketState) -> bool:
    return (
        state.steam_sell_price is not None
        and state.steam_price_source == "steam_orderbook"
        and state.c5_sell_price is not None
        and state.c5_price_source == "c5_batch"
    )


def _build_market_evaluation(
    *,
    config: StrategyConfig,
    item_type: dict[str, Any],
    state: MarketState,
    c5_risk: C5RecentSaleRisk | None = None,
    c5_pricing: dict[str, Any] | None = None,
    allow_below_min_item_value_for_existing_long_buy: bool = False,
) -> ProfitTradeMarketEvaluation | None:
    if not _state_price_is_usable_for_profit_trade(state):
        return None
    c5_listing_price = safe_float(state.c5_sell_price)
    steam_buy_price = safe_float(state.steam_sell_price)
    if c5_listing_price is None or steam_buy_price is None:
        return None
    if c5_listing_price <= 0 or steam_buy_price <= 0:
        return None
    # Profit Trade has one C5 price authority: MarketState.c5_sell_price from
    # price_batch.  Filtered listing rows and statistics must never override it.
    price_batch_reference = _c5_price_batch_reference(
        price=float(c5_listing_price),
        count=(
            safe_int(state.c5_sell_count)
            if state.c5_sell_count is not None
            else (safe_int(c5_risk.on_sale_count) if c5_risk is not None else None)
        ),
        raw={"marketHashName": state.market_hash_name, "price": c5_listing_price},
    )
    competitor_reference = float(price_batch_reference["effectiveReferencePrice"])
    combined_c5_risk = _merge_c5_listing_depth_and_statistics(
        depth=price_batch_reference,
        statistics=c5_risk,
    )
    c5_listing_price = _profit_trade_initial_listing_price(
        config,
        competitor_reference_price=competitor_reference,
        fallback_price=float(c5_listing_price),
    )
    below_min_item_value = bool(
        c5_listing_price < float(config.profit_trade_min_item_value)
    )
    if (
        below_min_item_value
        and not allow_below_min_item_value_for_existing_long_buy
    ):
        return None

    listing_net = c5_listing_price * float(config.profit_trade_c5_current_sale_net_factor)
    evaluated_depth = (
        _evaluate_c5_market_depth_risk(
            config,
            c5_listing_price=float(c5_listing_price),
            risk=combined_c5_risk,
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
            risk=combined_c5_risk,
        )
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
    risk_status = (
        evaluated_risk.status
        if config.profit_trade_require_c5_recent_sales
        else evaluated_depth.status
    )
    risk_reason = (
        evaluated_risk.reason
        if config.profit_trade_require_c5_recent_sales
        else evaluated_depth.reason
    )
    execution_status = "executable"
    execution_reason = "all automatic execution gates passed"
    audit_status = "passed"
    audit_reason = "rule_based"
    if below_min_item_value:
        # ``minItemValue`` remains a hard admission gate for ordinary direct
        # purchases and new long-buy orders.  An already-managed long buy is
        # the narrow exception: it still needs the fresh C5/Steam snapshot so
        # a falling C5 price cannot leave an unsafe old bid invisible.
        execution_status = "below_min_item_value"
        execution_reason = (
            f"initial C5 listing price {c5_listing_price:.2f} is below the "
            f"minimum item value {float(config.profit_trade_min_item_value):.2f}; "
            "only existing long-buy maintenance is evaluated"
        )
    elif manual_review_roi > 0 and expected_roi > manual_review_roi:
        audit_status = "manual_required"
        audit_reason = (
            f"ROI {expected_roi * 100:.2f}% > manual review threshold "
            f"{manual_review_roi * 100:.2f}%"
        )
        execution_status = "manual_review"
        execution_reason = audit_reason
    elif config.profit_trade_require_c5_market_depth and evaluated_depth.status != "passed":
        execution_status = "c5_risk_blocked"
        execution_reason = evaluated_depth.reason
        risk_status = evaluated_depth.status
        risk_reason = evaluated_depth.reason
    elif config.profit_trade_require_c5_recent_sales and evaluated_risk.status != "passed":
        execution_status = "c5_risk_blocked"
        execution_reason = evaluated_risk.reason
    elif expected_roi < float(config.profit_trade_min_roi):
        execution_status = "below_min_roi"
        execution_reason = (
            f"ROI {expected_roi * 100:.2f}% < automatic threshold "
            f"{float(config.profit_trade_min_roi) * 100:.2f}%"
        )
    elif config.profit_trade_ai_audit_enabled:
        execution_status = "ai_audit_blocked"
        execution_reason = "AI audit is enabled but no AI auditor is configured"

    return ProfitTradeMarketEvaluation(
        market_hash_name=str(item_type["market_hash_name"]),
        name=str(state.name_cn or item_type.get("name_cn") or item_type["market_hash_name"]),
        steam_buy_price=float(steam_buy_price),
        steam_price_source=state.steam_price_source or "unknown",
        c5_listing_price=float(c5_listing_price),
        c5_price_source=state.c5_price_source or "unknown",
        c5_expected_net_price=float(c5_expected_net),
        balance_discount=float(steam_cost_ratio),
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
        risk_status=risk_status,
        risk_reason=risk_reason,
        execution_status=execution_status,
        execution_reason=execution_reason,
        audit_status=audit_status,
        audit_reason=audit_reason,
        orderbook_snapshot=(
            dict(state.raw_json.get("steam_orderbook_snapshot") or {})
            if isinstance(state.raw_json.get("steam_orderbook_snapshot"), dict)
            else {}
        ),
        crossed_listing_probe=(
            dict(state.raw_json.get("crossed_listing_probe") or {})
            if isinstance(state.raw_json.get("crossed_listing_probe"), dict)
            else {}
        ),
        c5_pricing=price_batch_reference,
    )


def _refresh_profit_trade_c5_evaluation_for_buy_order_fallback(
    *,
    settings: Settings,
    config: StrategyConfig,
    c5_client: Any,
    market_hash_name: str,
    name: str,
    steam_buy_price: float,
) -> ProfitTradeMarketEvaluation:
    try:
        price_reference = _fetch_c5_price_batch_reference(
            c5_client,
            settings,
            market_hash_name=market_hash_name,
        )
    except Exception as exc:
        raise RuntimeError(
            f"C5 batch price refresh failed after Steam listings fallback: {exc}"
        ) from exc
    c5_sell_price = float(price_reference["effectiveReferencePrice"])
    batch_on_sale_count = safe_int(price_reference.get("onSaleCount"))
    statistics_map = _fetch_c5_recent_sale_risks(
        c5_client,
        app_id=settings.app_id,
        market_hash_names=[market_hash_name],
    )
    statistics = statistics_map.get(market_hash_name)
    c5_risk = C5RecentSaleRisk(
        recent_sold_net_price=statistics.recent_sold_net_price if statistics else None,
        recent_sold_count=statistics.recent_sold_count if statistics else None,
        status=statistics.status if statistics else "raw",
        reason=statistics.reason if statistics else "C5 batch price refreshed without statistics",
        raw={
            "priceBatch": price_reference.get("priceBatch") or {},
            "statistics": statistics.raw if statistics else {},
        },
        current_sell_price=float(c5_sell_price),
        on_sale_count=(
            batch_on_sale_count
            if batch_on_sale_count is not None
            else (statistics.on_sale_count if statistics else None)
        ),
        purchase_max_price=statistics.purchase_max_price if statistics else None,
        purchase_count=statistics.purchase_count if statistics else None,
    )
    evaluation = _build_market_evaluation(
        config=config,
        item_type={
            "market_hash_name": market_hash_name,
            "name_cn": name or market_hash_name,
            "inventory_count": 1,
            "tradable_count": 1,
            "reference_price": float(c5_sell_price),
        },
        state=MarketState(
            market_hash_name=market_hash_name,
            name_cn=name or market_hash_name,
            c5_sell_price=float(c5_sell_price),
            c5_sell_count=c5_risk.on_sale_count,
            c5_price_source="c5_batch",
            steam_sell_price=float(steam_buy_price),
            steam_price_source="steam_orderbook",
        ),
        c5_risk=c5_risk,
    )
    if evaluation is None:
        raise RuntimeError("C5/Steam market evaluation is no longer usable after listings HTTP 429")
    return evaluation


def _opportunity_from_market_evaluation(
    evaluation: ProfitTradeMarketEvaluation,
    *,
    sell_item: dict[str, Any],
) -> ProfitTradeOpportunity:
    return ProfitTradeOpportunity(
        market_hash_name=evaluation.market_hash_name,
        name=evaluation.name,
        asset_id=_inventory_item_key(sell_item),
        steam_id=str(sell_item.get("steamId") or "").strip() or None,
        token=str(sell_item.get("token") or "").strip() or None,
        style_token=str(sell_item.get("styleToken") or sell_item.get("style_token") or "").strip() or None,
        steam_buy_price=evaluation.steam_buy_price,
        steam_price_source=evaluation.steam_price_source,
        c5_listing_price=evaluation.c5_listing_price,
        c5_price_source=evaluation.c5_price_source,
        c5_expected_net_price=evaluation.c5_expected_net_price,
        steam_real_cost=evaluation.steam_real_cost,
        expected_profit=evaluation.expected_profit,
        expected_roi=evaluation.expected_roi,
        inventory_count=evaluation.inventory_count,
        tradable_count=evaluation.tradable_count,
        c5_recent_sold_net_price=evaluation.c5_recent_sold_net_price,
        c5_recent_sold_count=evaluation.c5_recent_sold_count,
        c5_current_sell_price=evaluation.c5_current_sell_price,
        c5_on_sale_count=evaluation.c5_on_sale_count,
        c5_purchase_max_price=evaluation.c5_purchase_max_price,
        c5_purchase_count=evaluation.c5_purchase_count,
        liquidity_status=evaluation.risk_status,
        audit_status=evaluation.audit_status,
        audit_reason=evaluation.audit_reason,
        orderbook_snapshot=dict(evaluation.orderbook_snapshot),
        c5_pricing=dict(evaluation.c5_pricing),
    )


def _build_opportunity(
    *,
    config: StrategyConfig,
    item_type: dict[str, Any],
    state: MarketState,
    sell_item: dict[str, Any],
    c5_risk: C5RecentSaleRisk | None = None,
) -> ProfitTradeOpportunity | None:
    evaluation = _build_market_evaluation(
        config=config,
        item_type=item_type,
        state=state,
        c5_risk=c5_risk,
    )
    if evaluation is None:
        return None
    if evaluation.execution_status not in {"executable", "manual_review"}:
        return None
    return _opportunity_from_market_evaluation(evaluation, sell_item=sell_item)


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
            "c5Pricing": opportunity.c5_pricing or None,
            "liquidityStatus": opportunity.liquidity_status,
            "auditStatus": opportunity.audit_status,
            "auditReason": opportunity.audit_reason,
            "scanOrderbookSnapshot": opportunity.orderbook_snapshot or None,
        }
    )


def _create_profit_trade_from_opportunity(
    db: Database,
    config: StrategyConfig,
    opportunity: ProfitTradeOpportunity,
    *,
    lock_asset: bool,
    origin_scan_id: str | None = None,
    origin_observed_at: str | None = None,
    source: str = "profit_trade_scan",
    note_overrides: dict[str, Any] | None = None,
    reserved_until_override: str | None = None,
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
    note = _opportunity_note(opportunity, source=source)
    note = _build_note(
        {
            **_read_note(note),
            "minRoiAtOpen": float(config.profit_trade_min_roi),
            "minRoiAtOpenSource": "trade_create_config",
            "initialListingDiscountPctAtOpen": float(
                config.profit_trade_initial_listing_discount_pct
            ),
            "repriceDiscountPctAtOpen": float(config.profit_trade_reprice_discount_pct),
            "staleMinRoiFactorAtOpen": float(config.profit_trade_stale_min_roi_factor),
            "pricingPolicyVersion": 2,
        }
    )
    if origin_scan_id:
        note = _build_note(
            {
                **_read_note(note),
                "originScanId": str(origin_scan_id),
                "originObservedAt": str(origin_observed_at or "").strip() or None,
            }
        )
    if note_overrides:
        note = _build_note({**_read_note(note), **note_overrides})

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
        reserved_until = reserved_until_override or _reservation_until(config)
        reservation_id = db.reserve_asset(
            asset_id=opportunity.asset_id,
            market_hash_name=opportunity.market_hash_name,
            owner=PROFIT_TRADE_OWNER,
            purpose="sell_existing_a",
            reserved_until=reserved_until,
            note=_build_note({"source": source, "trade": "pending"}),
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

    trade_id: int | None = None
    try:
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
            attached = db.attach_asset_reservation_operation(
                reservation_id=reservation_id,
                operation_id=trade_id,
                note=_build_note(
                    {
                        "source": source,
                        "tradeId": trade_id,
                        "reservedUntil": _read_note(note).get("reservedUntil"),
                    }
                ),
            )
            if not attached:
                raise RuntimeError(
                    f"failed to attach reservation {reservation_id} to Profit Trade {trade_id}"
                )
        if opportunity.orderbook_snapshot:
            scan_snapshot = _public_profit_trade_orderbook_snapshot(
                opportunity.orderbook_snapshot,
                stage="scan",
            )
            if scan_snapshot is not None:
                db.add_profit_trade_audit_event(
                    trade_id,
                    event_type="orderbook_snapshot",
                    reason=(
                        "Steam 盘口交叉，公开卖一可能滞后"
                        if scan_snapshot.get("crossed") is True
                        else "Steam 扫描盘口已记录"
                    ),
                    context={
                        "stage": "scan",
                        "steamOrderbook": scan_snapshot,
                    },
                )
    except Exception:
        if trade_id is not None:
            try:
                db.update_profit_trade(
                    trade_id,
                    status="cancelled",
                    error=None,
                    note=_build_note(
                        {
                            **_read_note(note),
                            "cancelReason": "asset reservation attachment failed",
                            "cancelSource": "profit_trade_reservation_compensation",
                            "cancelledBeforeSteamBuyAt": utc_now_iso(),
                        }
                    ),
                )
            except Exception:
                pass
        if reservation_id is not None:
            try:
                db.release_asset_reservation(
                    asset_id=opportunity.asset_id,
                    owner=PROFIT_TRADE_OWNER,
                    reason=_build_note(
                        {
                            "source": "profit_trade_reservation_compensation",
                            "tradeId": trade_id,
                            "reservationId": reservation_id,
                        }
                    ),
                )
            except Exception:
                pass
        raise
    assert trade_id is not None
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
        _event_reason=reason,
        _event_context={
            "cancelSource": source,
            "purchaseRequestSent": bool(
                (extra_note or {}).get("purchaseRequestSent")
            ),
            "listingIdObtained": bool(
                (extra_note or {}).get("listingIdObtained")
            ),
        },
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


def _preserve_locked_trade_after_pre_buy_queue_timeout(
    settings: Settings,
    config: StrategyConfig,
    trade_id: int,
    *,
    error: SteamRequestTimeout,
) -> bool:
    """Keep a provably pre-buy trade reserved for a safe full revalidation retry."""

    db = Database(settings.db_path)
    try:
        db.initialize()
        row = db.get_profit_trade(trade_id)
        if row is None or str(row["status"] or "") != "locked":
            return False
        note = _read_note(row["note"])
        if not _trade_is_before_steam_buy(row):
            return False
        if _profit_trade_has_purchase_request_evidence(row, note):
            return False
        asset_id = str(row["a_asset_id"] or "").strip()
        if not asset_id:
            return False
        reservation = db.get_active_asset_reservation(asset_id)
        if reservation is None:
            return False
        if str(reservation["status"] or "") != "active":
            return False
        if str(reservation["owner"] or "") != PROFIT_TRADE_OWNER:
            return False
        if safe_int(reservation["operation_id"]) != int(trade_id):
            return False

        timeout_count = max(0, safe_int(note.get("preBuyQueueTimeoutCount")) or 0) + 1
        retry_after_seconds = max(
            int(config.profit_trade_reservation_seconds),
            PROFIT_TRADE_CYCLE_INTERVAL_SECONDS + 120,
        )
        reserved_until = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
        ).replace(microsecond=0).isoformat()
        reservation_note = _read_note(reservation["note"])
        preserved_at = utc_now_iso()
        if not db.update_asset_reservation_deadline(
            asset_id=asset_id,
            owner=PROFIT_TRADE_OWNER,
            operation_id=int(trade_id),
            reserved_until=reserved_until,
            note=_build_note(
                {
                    **reservation_note,
                    "preBuyQueueTimeoutAt": preserved_at,
                    "preBuyQueueTimeoutCount": timeout_count,
                    "reservedUntil": reserved_until,
                }
            ),
        ):
            return False
        db.update_profit_trade(
            int(trade_id),
            note=_build_note(
                {
                    **note,
                    "purchaseRequestSent": False,
                    "preBuyQueueTimeoutAt": preserved_at,
                    "preBuyQueueTimeoutCount": timeout_count,
                    "preBuyQueueTimeoutError": str(error),
                    "preBuyQueueRetryPolicy": "retry_next_profit_trade_cycle",
                    "reservedUntil": reserved_until,
                }
            ),
        )
        db.add_profit_trade_audit_event(
            int(trade_id),
            event_type="pre_buy_queue_timeout_preserved",
            reason=str(error),
            context={
                "timeoutCount": timeout_count,
                "purchaseRequestSent": False,
                "reservedUntil": reserved_until,
                "retryPolicy": "immediate_once_then_next_profit_trade_cycle",
            },
        )
        return True
    finally:
        db.close()


def _execute_profit_trade_buy_with_queue_timeout_retry(
    settings: Settings,
    trade_id: int,
    *,
    config: StrategyConfig,
    steam_client: Any | None,
    c5_client: Any | None,
    new_action_guard: Callable[[], bool] | None,
    refresh_config_before_purchase: bool = False,
) -> dict[str, Any]:
    for attempt_index in range(2):
        try:
            return execute_profit_trade_buy(
                settings,
                trade_id,
                config=config,
                steam_client=steam_client,
                c5_client=c5_client,
                new_action_guard=new_action_guard,
                refresh_config_before_purchase=refresh_config_before_purchase,
            )
        except SteamRequestTimeout as exc:
            if not _preserve_locked_trade_after_pre_buy_queue_timeout(
                settings,
                config,
                trade_id,
                error=exc,
            ):
                raise
            if attempt_index == 0:
                continue
            raise
    raise AssertionError("unreachable pre-buy queue retry state")


def _manual_execution_roi_approval_block_reason(
    note: dict[str, Any],
    *,
    expected_roi: float,
) -> str | None:
    if note.get("manualExecutionApproved") is not True:
        return "this trade has no one-time manual ROI approval"
    approved_floor = safe_float(note.get("manualExecutionRoiFloor"))
    if approved_floor is None or approved_floor <= 0:
        return "the one-time manual ROI approval is missing its approved floor"
    expires_at = _parse_iso(note.get("manualExecutionApprovalExpiresAt"))
    if expires_at is None:
        return "the one-time manual ROI approval is missing its expiry"
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at.astimezone(timezone.utc):
        return "the one-time manual ROI approval expired before the Steam purchase"
    if _profit_trade_roi_gate_value(expected_roi) + 1e-12 < _profit_trade_roi_gate_value(
        approved_floor
    ):
        return (
            "ROI fell below the one-time manually approved floor before Steam buy: "
            f"{expected_roi * 100:.2f}% < {approved_floor * 100:.2f}%"
        )
    return None


def _record_search_listings_failure_before_purchase(
    db: Database,
    trade_id: int,
    *,
    error: Exception,
) -> None:
    """Persist explicit negative evidence before propagating listing-search failure."""

    row = db.get_profit_trade(trade_id)
    if row is None:
        return
    note = _read_note(row["note"])
    db.update_profit_trade(
        trade_id,
        note=_build_note(
            {
                **note,
                "purchaseRequestSent": False,
                "listingIdObtained": False,
                "purchaseRequestEvidence": "search_listings_failed_before_steam_buy",
                "searchListingsFailedAt": utc_now_iso(),
                "searchListingsError": str(error),
            }
        ),
    )


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
    new_action_guard: Callable[[], bool] | None = None,
    on_locked_trade_ready: Callable[[int, ProfitTradeOpportunity], None] | None = None,
    on_market_states_ready: Callable[[dict[str, MarketState], MarketService], None] | None = None,
) -> ProfitTradeScanReport:
    if config is None:
        config = load_strategy_config(settings)
    if not settings.c5_api_key and inventory_payload is None:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    if limit <= 0:
        raise ValueError("--limit 必须大于 0")

    db = Database(settings.db_path)
    db.initialize()
    listings_circuit = _get_profit_trade_listings_circuit(db)
    scan_id = f"PTSCAN-{uuid.uuid4().hex}"
    scan_observed_at = utc_now_iso()
    event_logger = get_profit_trade_event_logger()
    event_logger.emit(
        provider="local",
        component="profit_trade_scan",
        operation="run_started",
        message="Profit Trade scan started",
        run_id=scan_id,
        safe_context={
            "record": bool(record),
            "lock_asset": bool(lock_asset),
            "result_limit": int(limit),
            "evaluation_scope": "all eligible item types",
        },
    )
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
            c5_client = c5_client or _build_profit_trade_c5_client(
                settings,
                run_id=scan_id,
            )
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
        watch_eligible_names = _watch_eligible_market_hash_names(config, inventory_items)
        # A low current C5 price must not hide an existing managed long buy.
        # We need a new authoritative snapshot to detect when its old bid has
        # fallen below the agreed aggressive ROI floor.  This only widens the
        # maintenance scan; ordinary low-value items still cannot become new
        # direct-purchase or new-long-buy candidates.
        live_long_buy_orders_by_market = (
            {
                str(row["market_hash_name"] or "").strip(): row
                for row in db.list_profit_trade_long_buy_orders(
                    states=LONG_BUY_LIVE_STATES,
                    limit=1000,
                )
                if str(row["market_hash_name"] or "").strip()
            }
            if config.profit_trade_long_buy_enabled
            else {}
        )
        live_long_buy_market_hash_names = set(live_long_buy_orders_by_market)
        inventory_types = [
            item_type
            for item_type in all_inventory_types
            if int(item_type.get("tradable_count") or 0) > 0
            and str(item_type.get("market_hash_name") or "") in watch_eligible_names
            and (
                safe_float(item_type.get("reference_price")) is None
                or float(safe_float(item_type.get("reference_price")) or 0) >= float(config.profit_trade_min_item_value)
                or str(item_type.get("market_hash_name") or "")
                in live_long_buy_market_hash_names
            )
        ]
        inventory_types.sort(
            key=lambda item_type: (
                -(safe_float(item_type.get("reference_price")) or 0.0),
                str(item_type.get("market_hash_name") or ""),
            )
        )
        # Evaluate every item type that passed the tradable/value prefilters.
        # Cutting this list by C5 reference price can hide a lower-priced item
        # with a valid ROI before its Steam orderbook is ever checked. Keep the
        # legacy scan_max_items argument/API field compatible, but do not use it
        # as an evaluation cap. ``limit`` still controls how many opportunities
        # are returned/recorded after every eligible type has been evaluated.
        if market_service is None:
            market_service = _build_profit_trade_market_service(
                settings,
                telemetry_context={"run_id": scan_id},
            )
        stream_refresh = getattr(market_service, "refresh_items_stream", None)
        use_streaming_execution = bool(
            inventory_types
            and record
            and lock_asset
            and on_locked_trade_ready is not None
            and callable(stream_refresh)
        )
        if c5_client is None and settings.c5_api_key:
            c5_client = _build_profit_trade_c5_client(
                settings,
                run_id=scan_id,
            )
        c5_risks = (
            _fetch_c5_recent_sale_risks(
                c5_client,
                app_id=settings.app_id,
                market_hash_names=[str(item["market_hash_name"]) for item in inventory_types],
            )
            if c5_client is not None
            else {}
        )
        item_type_map = {
            str(item_type["market_hash_name"]): item_type
            for item_type in inventory_types
        }
        streamed_evaluations: dict[str, ProfitTradeMarketEvaluation] = {}
        streamed_sell_items: dict[str, dict[str, Any] | None] = {}
        streamed_opportunities: list[ProfitTradeOpportunity] = []
        streamed_created_asset_ids: set[str] = set()

        def handle_state_ready(state: MarketState) -> None:
            market_hash_name = str(state.market_hash_name or "")
            item_type = item_type_map.get(market_hash_name)
            if item_type is None or not _state_price_is_usable_for_profit_trade(state):
                return
            live_long_buy_order = live_long_buy_orders_by_market.get(
                market_hash_name
            )
            evaluation = _build_market_evaluation(
                config=config,
                item_type=item_type,
                state=state,
                c5_risk=c5_risks.get(market_hash_name),
                allow_below_min_item_value_for_existing_long_buy=(
                    live_long_buy_order is not None
                ),
            )
            if evaluation is None:
                return
            sell_item = _pick_sell_asset(
                db,
                config,
                inventory_items,
                market_hash_name=market_hash_name,
            )
            streamed_evaluations[market_hash_name] = evaluation
            streamed_sell_items[market_hash_name] = sell_item
            if (
                sell_item is None
                or evaluation.execution_status != "executable"
                or len(streamed_opportunities) >= limit
            ):
                return
            if not _profit_trade_new_action_allowed(new_action_guard):
                return
            opportunity = _opportunity_from_market_evaluation(
                evaluation,
                sell_item=sell_item,
            )
            trade_id = _create_profit_trade_from_opportunity(
                db,
                config,
                opportunity,
                lock_asset=True,
                origin_scan_id=scan_id,
                origin_observed_at=scan_observed_at,
            )
            if trade_id is None:
                return
            created_trade_ids.append(trade_id)
            locked_trade_ids.append(trade_id)
            streamed_opportunities.append(opportunity)
            streamed_created_asset_ids.add(str(opportunity.asset_id or ""))
            event_logger.emit(
                provider="local",
                component="profit_trade_scan",
                operation="opportunity_dispatched_early",
                message="Profit Trade opportunity entered the execution queue before the full scan completed",
                run_id=scan_id,
                trade_id=trade_id,
                market_hash_name=market_hash_name,
                asset_id=str(opportunity.asset_id or "") or None,
                safe_context={
                    "expected_roi": opportunity.expected_roi,
                    "steam_buy_price": opportunity.steam_buy_price,
                    "c5_listing_price": opportunity.c5_listing_price,
                    "full_scan_complete": False,
                    "execution_priority": "high",
                },
            )
            on_locked_trade_ready(trade_id, opportunity)

        if use_streaming_execution:
            states = stream_refresh(
                inventory_types,
                on_state_ready=handle_state_ready,
            )
        else:
            states = market_service.refresh_items(inventory_types) if inventory_types else []
        if inventory_types and not states:
            raise RuntimeError(
                "Profit Trade market refresh returned no states for a non-empty eligible inventory; "
                "ROI watch was left unchanged"
            )
        # Scanning only consumes the already-fetched orderbook.  Concrete
        # listing discovery belongs exclusively to the real purchase path.
        listings_circuit = _get_profit_trade_listings_circuit(db)
        state_map = {state.market_hash_name: state for state in states}
        if on_market_states_ready is not None:
            try:
                on_market_states_ready(state_map, market_service)
            except Exception as exc:
                # Research observation must never block the real scan or its
                # execution queue.  The independent P3 task can retry later.
                notes.append(f"selection watch refresh deferred: {exc}")
                event_logger.emit(
                    level="WARN",
                    provider="local",
                    component="profit_trade_selection_watch",
                    operation="shared_snapshot_refresh_failed",
                    message="Profit Trade selection watch could not reuse this scan snapshot",
                    run_id=scan_id,
                    safe_context={"error": str(exc)[:1000]},
                )
        opportunities: list[ProfitTradeOpportunity] = []
        watch_observations: list[dict[str, Any]] = []
        watch_exit_reasons: dict[str, str] = {}
        watch_exit_observations: dict[str, dict[str, Any]] = {}
        missing_price_count = 0
        skipped_count = 0
        for item_type in inventory_types:
            market_hash_name = str(item_type["market_hash_name"])
            state = state_map.get(market_hash_name)
            if state is None or not _state_price_is_usable_for_profit_trade(state):
                missing_price_count += 1
                watch_exit_reasons[market_hash_name] = "Steam orderbook or C5 batch price is unavailable"
                unavailable_snapshot = (
                    state.raw_json.get("steam_orderbook_snapshot")
                    if state is not None
                    and isinstance(state.raw_json.get("steam_orderbook_snapshot"), dict)
                    else {}
                )
                event_logger.emit(
                    level="WARN",
                    provider="local",
                    component="profit_trade_scan",
                    operation="item_price_unavailable",
                    message=(
                        _orderbook_log_message(unavailable_snapshot)
                        if unavailable_snapshot
                        else "Profit Trade item evaluation has no usable Steam/C5 price pair"
                    ),
                    run_id=scan_id,
                    market_hash_name=market_hash_name,
                    safe_context={
                        "reason": watch_exit_reasons[market_hash_name],
                        "stage": "scan",
                        "steam_orderbook": _orderbook_log_summary(unavailable_snapshot),
                    },
                )
                continue
            live_long_buy_order = live_long_buy_orders_by_market.get(
                market_hash_name
            )
            evaluation = streamed_evaluations.get(market_hash_name) or _build_market_evaluation(
                config=config,
                item_type=item_type,
                state=state,
                c5_risk=c5_risks.get(market_hash_name),
                allow_below_min_item_value_for_existing_long_buy=(
                    live_long_buy_order is not None
                ),
            )
            if evaluation is None:
                skipped_count += 1
                watch_exit_reasons[market_hash_name] = "price or minimum-item-value evaluation is not usable"
                skipped_snapshot = (
                    state.raw_json.get("steam_orderbook_snapshot")
                    if isinstance(state.raw_json.get("steam_orderbook_snapshot"), dict)
                    else {}
                )
                event_logger.emit(
                    level="DEBUG",
                    provider="local",
                    component="profit_trade_scan",
                    operation="item_evaluation_skipped",
                    message=(
                        _orderbook_log_message(skipped_snapshot)
                        if skipped_snapshot
                        else "Profit Trade item evaluation was skipped"
                    ),
                    run_id=scan_id,
                    market_hash_name=market_hash_name,
                    safe_context={
                        "reason": watch_exit_reasons[market_hash_name],
                        "stage": "scan",
                        "steam_orderbook": _orderbook_log_summary(skipped_snapshot),
                    },
                )
                continue
            sell_item = (
                streamed_sell_items.get(market_hash_name)
                if market_hash_name in streamed_sell_items
                else _pick_sell_asset(
                    db,
                    config,
                    inventory_items,
                    market_hash_name=market_hash_name,
                )
            )
            manual_executable_quantity = len(
                _list_executable_sell_assets(
                    db,
                    config,
                    inventory_items,
                    market_hash_name=market_hash_name,
                )
            )
            proposal_quantity = (
                max(
                    1,
                    safe_int(live_long_buy_order["remaining_quantity"]) or 1,
                )
                if live_long_buy_order is not None
                else min(
                    PROFIT_TRADE_LONG_BUY_BASE_QUANTITY,
                    manual_executable_quantity,
                )
            )
            c5_price_batch = (
                safe_float(
                    (evaluation.c5_pricing or {}).get(
                        "effectiveReferencePrice"
                    )
                )
                or safe_float(state.c5_sell_price)
            )
            long_buy_proposal = (
                build_long_buy_proposal(
                    config,
                    c5_price_batch=c5_price_batch,
                    orderbook_snapshot=evaluation.orderbook_snapshot,
                    quantity=proposal_quantity,
                    own_price_cents=remembered_own_price_cents(
                        live_long_buy_order
                    ),
                )
                if config.profit_trade_long_buy_enabled
                and c5_price_batch is not None
                and proposal_quantity > 0
                else None
            )
            if long_buy_proposal is not None:
                direct_gate_passed = bool(
                    sell_item is not None
                    and evaluation.execution_status == "executable"
                )
                orderbook_crossed = bool(
                    isinstance(evaluation.orderbook_snapshot, dict)
                    and evaluation.orderbook_snapshot.get("crossed") is True
                )
                new_order_eligible = bool(
                    live_long_buy_order is None
                    and sell_item is not None
                    and evaluation.execution_status == "below_min_roi"
                    and evaluation.execution_status != "below_min_item_value"
                    # A crossed public Steam book is not reliable evidence that
                    # the apparent seller path is actionable.  User-confirmed
                    # safety policy: when that seller path is below the normal
                    # ROI threshold, do not create a new long-term buy order.
                    and not orderbook_crossed
                )
                crossed_existing_long_buy = bool(
                    orderbook_crossed and live_long_buy_order is not None
                )
                blocked_reason: str | None = None
                if crossed_existing_long_buy:
                    blocked_reason = (
                        "Steam 盘口交叉且已有未成交长期求购；"
                        "安全策略保留旧求购，不撤单、不改价、不走直购"
                    )
                elif orderbook_crossed and not direct_gate_passed:
                    blocked_reason = (
                        "Steam 盘口交叉且当前卖盘未达到正常 ROI；"
                        "安全策略不创建新的长期求购"
                    )
                elif live_long_buy_order is not None:
                    blocked_reason = "managed long-term buy order already exists"
                elif sell_item is None:
                    blocked_reason = "no executable old A asset is currently available"
                elif evaluation.execution_status != "below_min_roi":
                    blocked_reason = (
                        "current seller path is executable"
                        if direct_gate_passed
                        else evaluation.execution_reason
                    )
                long_buy_proposal.update(
                    {
                        "eligible": new_order_eligible,
                        "executionAllowed": bool(
                            new_order_eligible
                            and config.profit_trade_allow_real_execution
                            and config.profit_trade_long_buy_allow_real_execution
                        ),
                        "blockedReason": blocked_reason,
                        "sourceScanId": scan_id,
                        "sellerExecutionStatus": evaluation.execution_status,
                        "recommendedAction": (
                            "hold"
                            if crossed_existing_long_buy
                            else "cancel_for_direct_purchase"
                            if live_long_buy_order is not None and direct_gate_passed
                            else "hold"
                            if live_long_buy_order is not None
                            else "create"
                            if new_order_eligible
                            else "none"
                        ),
                    }
                )
                if new_order_eligible:
                    event_logger.emit(
                        level="INFO",
                        provider="local",
                        component="profit_trade_long_buy",
                        operation="proposal_ready",
                        message="Profit Trade long-term buy proposal is ready",
                        run_id=scan_id,
                        market_hash_name=market_hash_name,
                        safe_context={
                            "target_price": long_buy_proposal.get("targetPrice"),
                            "quantity": long_buy_proposal.get("quantity"),
                            "decision": long_buy_proposal.get("decision"),
                            "competitor_buy_price": long_buy_proposal.get(
                                "competitorBuyPrice"
                            ),
                            "worst_case_roi": long_buy_proposal.get(
                                "worstCaseRoi"
                            ),
                            "execution_allowed": long_buy_proposal.get(
                                "executionAllowed"
                            ),
                        },
                    )
            should_watch = bool(
                evaluation.expected_roi > 0
                or live_long_buy_order is not None
                or (
                    long_buy_proposal is not None
                    and bool(long_buy_proposal.get("eligible"))
                )
            )
            if should_watch:
                watch_execution_status = evaluation.execution_status
                watch_execution_reason = evaluation.execution_reason
                if sell_item is None and watch_execution_status == "executable":
                    watch_execution_status = "asset_unavailable"
                    watch_execution_reason = (
                        "no unreserved executable asset with the required C5 sale tokens is currently available"
                    )
                elif watch_execution_status == "executable" and listings_circuit["status"] == "open":
                    watch_execution_status = "listings_cooldown"
                    watch_execution_reason = (
                        "Steam listings 冷却中；执行前将重新校验行情并改走安全求购，"
                        f"冷却结束 {listings_circuit.get('cooldownUntil') or '-'}；到期后自动恢复正常查询"
                    )
                watch_observations.append(
                    evaluation.to_watch_record(
                        config,
                        execution_status=watch_execution_status,
                        execution_reason=watch_execution_reason,
                        manual_executable_quantity=manual_executable_quantity,
                        long_buy_order=live_long_buy_order,
                        long_buy_proposal=long_buy_proposal,
                    )
                )
            else:
                watch_exit_reasons[market_hash_name] = (
                    f"ROI is not positive: {evaluation.expected_roi * 100:.2f}%"
                )
                watch_exit_observations[market_hash_name] = evaluation.to_watch_record(config)
            event_logger.emit(
                level="INFO" if evaluation.expected_roi > 0 else "DEBUG",
                provider="local",
                component="profit_trade_scan",
                operation="item_evaluated",
                message=_orderbook_log_message(evaluation.orderbook_snapshot),
                run_id=scan_id,
                market_hash_name=market_hash_name,
                asset_id=_inventory_item_key(sell_item) if sell_item is not None else None,
                safe_context={
                    "steam_buy_price": evaluation.steam_buy_price,
                    "steam_price_source": evaluation.steam_price_source,
                    "c5_listing_price": evaluation.c5_listing_price,
                    "c5_price_source": evaluation.c5_price_source,
                    "c5_expected_net_price": evaluation.c5_expected_net_price,
                    "balance_discount": evaluation.balance_discount,
                    "expected_profit": evaluation.expected_profit,
                    "expected_roi": evaluation.expected_roi,
                    "min_roi": float(config.profit_trade_min_roi),
                    "manual_review_roi": float(config.profit_trade_manual_review_roi),
                    "risk_status": evaluation.risk_status,
                    "risk_reason": evaluation.risk_reason,
                    "execution_status": evaluation.execution_status,
                    "execution_reason": evaluation.execution_reason,
                    "has_executable_asset": sell_item is not None,
                    "stage": "scan",
                    "steam_orderbook": _orderbook_log_summary(evaluation.orderbook_snapshot),
                },
            )
            if sell_item is None:
                skipped_count += 1
                continue
            if evaluation.execution_status not in {"executable", "manual_review"}:
                skipped_count += 1
                continue
            opportunity = _opportunity_from_market_evaluation(
                evaluation,
                sell_item=sell_item,
            )
            opportunities.append(opportunity)

        opportunities.sort(key=lambda item: (-item.expected_roi, -item.c5_listing_price, item.market_hash_name))
        if streamed_opportunities:
            streamed_assets = {str(item.asset_id or "") for item in streamed_opportunities}
            opportunities = [
                *streamed_opportunities,
                *(item for item in opportunities if str(item.asset_id or "") not in streamed_assets),
            ]
        opportunities = opportunities[:limit]
        watch_result = db.record_profit_trade_roi_scan(
            watch_observations,
            scan_id=scan_id,
            observed_at=scan_observed_at,
            exit_reasons=watch_exit_reasons,
            exit_observations=watch_exit_observations,
        )
        notes.append(
            "ROI watch updated: "
            f"active={len(watch_observations)}, entered={watch_result['inserted']}, "
            f"refreshed={watch_result['updated']}, exited={watch_result['exited']}."
        )
        event_logger.emit(
            provider="local",
            component="profit_trade_scan",
            operation="run_completed",
            message="Profit Trade scan completed",
            run_id=scan_id,
            safe_context={
                "inventory_count": len(inventory_items),
                "evaluated_count": len(inventory_types),
                "opportunity_count": len(opportunities),
                "missing_price_count": missing_price_count,
                "skipped_count": skipped_count,
                "roi_watch": watch_result,
            },
        )
        if config.profit_trade_ai_audit_enabled:
            notes.append("AI audit enabled but no AI auditor is configured; opportunities are blocked.")
        if config.profit_trade_require_c5_market_depth:
            notes.append("C5 current market depth risk is required; opportunities without usable C5 sell price/count statistics are blocked.")
        if config.profit_trade_require_c5_recent_sales:
            notes.append("C5 recent sale risk is required; opportunities without sufficient C5 recent sale statistics are blocked.")

        listings_circuit = _get_profit_trade_listings_circuit(db)
        record_opportunities = list(opportunities)
        if record and listings_circuit["status"] == "open":
            notes.append(
                "Steam listings circuit is open; eligible trades may still execute through the safe buy-order fallback after fresh market checks."
            )
            event_logger.emit(
                level="WARN",
                provider="local",
                component="profit_trade_listings_circuit",
                operation="listings_circuit_buy_order_fallback_enabled",
                message="Steam listings is cooling down; eligible Profit Trade execution may use the safe buy-order fallback",
                run_id=scan_id,
                safe_context=listings_circuit,
            )
        if record:
            for opportunity in record_opportunities:
                if str(opportunity.asset_id or "") in streamed_created_asset_ids:
                    continue
                if lock_asset and not _profit_trade_new_action_allowed(new_action_guard):
                    notes.append(
                        "Profit Trade runtime was disabled during the scan; "
                        "no additional execution trade was created or locked."
                    )
                    break
                trade_id = _create_profit_trade_from_opportunity(
                    db,
                    config,
                    opportunity,
                    lock_asset=lock_asset,
                    origin_scan_id=scan_id,
                    origin_observed_at=scan_observed_at,
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
        inventory_items=[
            dict(item)
            for item in list((inventory_payload or {}).get("list") or [])
            if isinstance(item, dict)
        ],
        watch_records=[
            dict(item)
            for item in (watch_observations if "watch_observations" in locals() else [])
            if isinstance(item, dict)
        ],
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
    new_action_guard: Callable[[], bool] | None = None,
    refresh_config_before_purchase: bool = False,
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

        def stop_if_runtime_disabled(stage: str) -> dict[str, Any] | None:
            if _profit_trade_new_action_allowed(new_action_guard):
                return None
            reason = (
                "Profit Trade runtime was disabled before a new Steam purchase action; "
                f"stopped safely at {stage}"
            )
            _cancel_pre_steam_buy_trade(
                db,
                row,
                reason=reason,
                source="profit_trade_runtime_disabled",
                extra_note={
                    "runtimeDisabledAt": utc_now_iso(),
                    "runtimeDisabledStage": stage,
                    "purchaseRequestSent": False,
                    "listingIdObtained": False,
                    "purchaseRequestEvidence": "runtime_disabled_before_steam_buy",
                },
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

        stopped = stop_if_runtime_disabled("before_market_lookup")
        if stopped is not None:
            return stopped

        asset_id = str(row["a_asset_id"] or "").strip()
        market_hash_name = str(row["market_hash_name"] or "").strip()
        if not asset_id or not market_hash_name:
            raise RuntimeError("trade missing A asset or market_hash_name")
        market_prefers_buy_order = _steam_market_should_use_buy_order(market_hash_name)

        listings_circuit = _get_profit_trade_listings_circuit(db)
        listings_cooldown_mode = (
            listings_circuit["status"] == "open" and not market_prefers_buy_order
        )
        if listings_circuit["status"] == "open" and not market_prefers_buy_order:
            get_profit_trade_event_logger().emit(
                level="WARN",
                provider="local",
                component="profit_trade_listings_circuit",
                operation="listings_circuit_buy_order_fallback",
                message="Steam listings is cooling down; this trade will revalidate markets and use the safe buy-order fallback",
                **_profit_trade_telemetry_context(row),
                safe_context=listings_circuit,
            )

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
        telemetry_context = _profit_trade_telemetry_context(row)
        a_steam_id = str(row["a_steam_id"] or note.get("steamId") or "").strip()
        selected_account: Account | None = None
        selected_wallet: dict[str, Any] = {}
        selected_wallet_balance: float | None = None
        selected_reserved_balance = 0.0
        selected_spendable_balance: float | None = None
        selected_wallet_is_live = False
        client = steam_client
        if steam_client is not None:
            selected_reserved_balance = _account_reserved_balance(
                None,
                config.profit_trade_account_reserved_balances,
                account_id=str(getattr(client, "account_id", "") or ""),
                steam_id64=str(getattr(client, "steam_id64", "") or ""),
            )
        if c5_client is None and settings.c5_api_key:
            c5_client = _build_profit_trade_c5_client(
                settings,
                **telemetry_context,
            )
        orderbook_payload: dict[str, Any] = {}
        orderbook_buy_target: SteamBuyTarget | None = None
        if steam_client is None:
            # The scan has already supplied the CNY orderbook floor that created
            # this trade. It is enough to choose a likely buyer account. Avoid a
            # duplicate default-account orderbook request; the selected account
            # still performs one P1 final market check immediately afterward.
            scanned_buy_price = safe_float(row["steam_buy_price"]) or safe_float(
                note.get("steamBuyPrice")
            )
            if scanned_buy_price is None or scanned_buy_price <= 0:
                bootstrap_client = _build_steam_client(
                    settings,
                    telemetry_context=telemetry_context,
                )
                try:
                    orderbook_payload = bootstrap_client.order_book(
                        app_id=settings.app_id,
                        market_hash_name=market_hash_name,
                        execution_priority=True,
                    )
                    _record_profit_trade_orderbook_snapshot(
                        orderbook_payload,
                        stage="account_selection_bootstrap",
                        expected_currency=config.steam_currency,
                        telemetry_context=telemetry_context,
                        db=db,
                        trade_id=trade_id,
                    )
                except SteamMarketError as exc:
                    raise RuntimeError(f"Steam orderbook failed: {exc}") from exc
                orderbook_buy_target = _pick_lowest_steam_orderbook_buy_target(orderbook_payload)
                if orderbook_buy_target is None:
                    raise RuntimeError("Steam orderbook returned no buyable sell order")
                scanned_buy_price = orderbook_buy_target.total_price
            selection = _select_steam_buy_account(
                settings,
                required_balance=float(scanned_buy_price),
                preferred_steam_id=a_steam_id,
                account_reserved_balances=config.profit_trade_account_reserved_balances,
                telemetry_context=telemetry_context,
            )
            selected_account = selection.account
            selected_wallet = selection.wallet
            selected_wallet_balance = selection.wallet_balance
            selected_reserved_balance = selection.reserved_balance
            selected_spendable_balance = selection.spendable_balance
            selected_wallet_is_live = selection.wallet_is_live
            client = selection.client

        if client is None:
            raise RuntimeError("missing Steam client before final buy market check")
        try:
            orderbook_payload = client.order_book(
                app_id=settings.app_id,
                market_hash_name=market_hash_name,
                execution_priority=True,
            )
            _record_profit_trade_orderbook_snapshot(
                orderbook_payload,
                stage="pre_buy",
                expected_currency=config.steam_currency,
                telemetry_context=telemetry_context,
                db=db,
                trade_id=trade_id,
            )
        except SteamMarketError as exc:
            raise RuntimeError("Steam orderbook failed for selected account: " f"{exc}") from exc
        orderbook_buy_target = _pick_lowest_steam_orderbook_buy_target(orderbook_payload)
        if orderbook_buy_target is None:
            raise RuntimeError("Steam orderbook returned no buyable sell order for selected account")
        if (
            selected_spendable_balance is not None
            and selected_spendable_balance + 1e-9 < orderbook_buy_target.total_price
            and steam_client is None
        ):
            selection = _select_steam_buy_account(
                settings,
                required_balance=orderbook_buy_target.total_price,
                preferred_steam_id=a_steam_id,
                account_reserved_balances=config.profit_trade_account_reserved_balances,
                telemetry_context=telemetry_context,
            )
            selected_account = selection.account
            selected_wallet = selection.wallet
            selected_wallet_balance = selection.wallet_balance
            selected_reserved_balance = selection.reserved_balance
            selected_spendable_balance = selection.spendable_balance
            selected_wallet_is_live = selection.wallet_is_live
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
        search_listings_429_events: list[dict[str, Any]] = []
        search_listings_400_events: list[dict[str, Any]] = []
        search_listings_fallback_events: list[dict[str, Any]] = []
        c5_evaluation_after_429: ProfitTradeMarketEvaluation | None = None
        c5_evaluated_fallback_count = 0
        prefer_buy_order = market_prefers_buy_order
        force_buy_order_fallback = listings_cooldown_mode
        c5_refresh_required = listings_cooldown_mode
        used_search_listings_429_buy_order_fallback = False
        used_search_listings_400_buy_order_fallback = False
        last_search_listings_fallback_status_code: int | None = None
        listings_payload: dict[str, Any] = {}
        refresh_market_snapshot = False

        def activate_safe_buy_order_fallback(
            exc: _SearchListingsFallbackRequired,
            *,
            after_account_selection: bool,
        ) -> tuple[dict[str, Any], SteamBuyTarget]:
            nonlocal listings_circuit
            nonlocal force_buy_order_fallback
            nonlocal c5_refresh_required
            nonlocal used_search_listings_429_buy_order_fallback
            nonlocal used_search_listings_400_buy_order_fallback
            nonlocal last_search_listings_fallback_status_code

            status_code = int(exc.status_code)
            last_search_listings_fallback_status_code = status_code
            search_listings_fallback_events.extend(exc.events)
            if status_code == 429:
                search_listings_429_events.extend(exc.events)
                listings_circuit = _open_profit_trade_listings_circuit(
                    db,
                    settings,
                    row=row,
                    client=client,
                    events=exc.events,
                )
                used_search_listings_429_buy_order_fallback = True
            elif status_code == 400:
                search_listings_400_events.extend(exc.events)
                used_search_listings_400_buy_order_fallback = True
            else:  # pragma: no cover - the producer only allows audited codes.
                raise RuntimeError(
                    f"unsupported Steam listings fallback status: {status_code}"
                )

            force_buy_order_fallback = True
            c5_refresh_required = True
            try:
                refreshed_orderbook = client.order_book(
                    app_id=settings.app_id,
                    market_hash_name=market_hash_name,
                    execution_priority=True,
                )
                _record_profit_trade_orderbook_snapshot(
                    refreshed_orderbook,
                    stage=f"after_listings_{status_code}",
                    expected_currency=config.steam_currency,
                    telemetry_context=telemetry_context,
                    db=db,
                    trade_id=trade_id,
                )
            except SteamMarketError as refresh_exc:
                raise RuntimeError(
                    "Steam orderbook refresh failed after listings "
                    f"HTTP {status_code}: {refresh_exc}"
                ) from refresh_exc
            refreshed_target = _pick_lowest_steam_orderbook_buy_target(
                refreshed_orderbook
            )
            if refreshed_target is None:
                raise RuntimeError(
                    "Steam orderbook returned no buyable sell order after listings "
                    f"HTTP {status_code}"
                )
            get_profit_trade_event_logger().emit(
                level="WARN",
                provider="local",
                component="profit_trade_buy",
                operation=f"search_listings_{status_code}_buy_order_fallback",
                message=(
                    f"Steam listings returned HTTP {status_code}"
                    + (" after account selection" if after_account_selection else "")
                    + "; refreshed orderbook and switched to the safe buy-order path"
                ),
                **telemetry_context,
                exception_type=type(exc.last_error).__name__,
                safe_context={
                    "listing_id_obtained": False,
                    "purchase_request_sent": False,
                    "search_listings_status_code": status_code,
                    "search_listings_fallback_count": len(
                        search_listings_fallback_events
                    ),
                    "current_orderbook_price": refreshed_target.total_price,
                    "listings_circuit": listings_circuit,
                },
            )
            return refreshed_orderbook, refreshed_target

        def search_listings_fallback_audit() -> dict[str, Any]:
            return {
                "searchListingsFallbackStatusCode": (
                    last_search_listings_fallback_status_code
                ),
                "searchListingsFallbackCount": len(
                    search_listings_fallback_events
                ),
                "searchListingsFallbackEvents": search_listings_fallback_events,
                "searchListings429Count": len(search_listings_429_events),
                "searchListings429Events": search_listings_429_events,
                "searchListings400Count": len(search_listings_400_events),
                "searchListings400Events": search_listings_400_events,
                "searchListings429FallbackToBuyOrder": (
                    used_search_listings_429_buy_order_fallback
                ),
                "searchListings400FallbackToBuyOrder": (
                    used_search_listings_400_buy_order_fallback
                ),
            }

        while True:
            stopped = stop_if_runtime_disabled("before_steam_buy_market_refresh")
            if stopped is not None:
                return stopped
            if refresh_market_snapshot:
                try:
                    orderbook_payload = client.order_book(
                        app_id=settings.app_id,
                        market_hash_name=market_hash_name,
                        execution_priority=True,
                    )
                    _record_profit_trade_orderbook_snapshot(
                        orderbook_payload,
                        stage="buy_retry",
                        expected_currency=config.steam_currency,
                        telemetry_context=telemetry_context,
                        db=db,
                        trade_id=trade_id,
                    )
                except SteamMarketError as exc:
                    raise RuntimeError(f"Steam orderbook refresh failed before buy: {exc}") from exc
                orderbook_buy_target = _pick_lowest_steam_orderbook_buy_target(orderbook_payload)
                if orderbook_buy_target is None:
                    raise RuntimeError("Steam orderbook returned no buyable sell order before buy")
            refresh_market_snapshot = True

            use_buy_order = prefer_buy_order or force_buy_order_fallback
            if not use_buy_order:
                try:
                    (
                        listings_payload,
                        orderbook_payload,
                        orderbook_buy_target,
                        rate_limit_events,
                    ) = _search_profit_trade_listings_once(
                        settings=settings,
                        config=config,
                        client=client,
                        market_hash_name=market_hash_name,
                        orderbook_payload=orderbook_payload,
                        orderbook_buy_target=orderbook_buy_target,
                        telemetry_context=telemetry_context,
                    )
                    search_listings_429_events.extend(rate_limit_events)
                except _SearchListingsFallbackRequired as exc:
                    listings_payload = {}
                    orderbook_payload, orderbook_buy_target = (
                        activate_safe_buy_order_fallback(
                            exc,
                            after_account_selection=False,
                        )
                    )
                    use_buy_order = True
                except SteamMarketError as exc:
                    _record_search_listings_failure_before_purchase(
                        db,
                        trade_id,
                        error=exc,
                    )
                    get_profit_trade_event_logger().emit(
                        level="WARN",
                        provider="local",
                        component="profit_trade_buy",
                        operation="purchase_request_not_sent",
                        message="Steam purchase request was not sent because listing search failed",
                        **telemetry_context,
                        exception_type=type(exc).__name__,
                        safe_context={
                            "failed_operation": "search_listings",
                            "listing_id_obtained": False,
                            "purchase_request_sent": False,
                            "error": str(exc),
                        },
                    )
                    raise RuntimeError(f"Steam listings search failed: {exc}") from exc
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
                selected_spendable_balance is not None
                and selected_spendable_balance + 1e-9 < buy_target.total_price
                and steam_client is None
            ):
                selection = _select_steam_buy_account(
                    settings,
                    required_balance=buy_target.total_price,
                    preferred_steam_id=a_steam_id,
                    account_reserved_balances=config.profit_trade_account_reserved_balances,
                    telemetry_context=telemetry_context,
                )
                selected_account = selection.account
                selected_wallet = selection.wallet
                selected_wallet_balance = selection.wallet_balance
                selected_reserved_balance = selection.reserved_balance
                selected_spendable_balance = selection.spendable_balance
                selected_wallet_is_live = selection.wallet_is_live
                client = selection.client
                try:
                    orderbook_payload = client.order_book(
                        app_id=settings.app_id,
                        market_hash_name=market_hash_name,
                        execution_priority=True,
                    )
                    _record_profit_trade_orderbook_snapshot(
                        orderbook_payload,
                        stage="account_change",
                        expected_currency=config.steam_currency,
                        telemetry_context=telemetry_context,
                        db=db,
                        trade_id=trade_id,
                    )
                    orderbook_buy_target = _pick_lowest_steam_orderbook_buy_target(orderbook_payload)
                    if orderbook_buy_target is None:
                        raise RuntimeError("Steam orderbook returned no buyable sell order for selected account")
                except SteamMarketError as exc:
                    raise RuntimeError(f"Steam buy market refresh failed for selected account: {exc}") from exc

                use_buy_order = prefer_buy_order or force_buy_order_fallback
                listings_payload = {}
                if not use_buy_order:
                    stopped = stop_if_runtime_disabled(
                        "before_search_listings_after_account_change"
                    )
                    if stopped is not None:
                        return stopped
                    try:
                        (
                            listings_payload,
                            orderbook_payload,
                            orderbook_buy_target,
                            rate_limit_events,
                        ) = _search_profit_trade_listings_once(
                            settings=settings,
                            config=config,
                            client=client,
                            market_hash_name=market_hash_name,
                            orderbook_payload=orderbook_payload,
                            orderbook_buy_target=orderbook_buy_target,
                            telemetry_context=telemetry_context,
                        )
                        search_listings_429_events.extend(rate_limit_events)
                    except _SearchListingsFallbackRequired as exc:
                        listings_payload = {}
                        orderbook_payload, orderbook_buy_target = (
                            activate_safe_buy_order_fallback(
                                exc,
                                after_account_selection=True,
                            )
                        )
                        use_buy_order = True
                    except SteamMarketError as exc:
                        _record_search_listings_failure_before_purchase(
                            db,
                            trade_id,
                            error=exc,
                        )
                        raise RuntimeError(
                            f"Steam buy market refresh failed for selected account: {exc}"
                        ) from exc
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

            steam_buy_price = buy_target.total_price
            if (
                c5_refresh_required
                or len(search_listings_fallback_events)
                > c5_evaluated_fallback_count
            ):
                if c5_client is None:
                    reason = (
                        "C5 market refresh is unavailable before the Steam buy-order fallback; "
                        "purchase was stopped safely"
                    )
                    _cancel_pre_steam_buy_trade(
                        db,
                        row,
                        reason=reason,
                        source="profit_trade_buy_fallback_c5_refresh_guard",
                        extra_note={
                            "purchaseRequestSent": False,
                            "listingIdObtained": bool(getattr(buy_target, "listing_id", "")),
                            "purchaseRequestEvidence": "c5_refresh_unavailable_after_search_listings_fallback",
                            **search_listings_fallback_audit(),
                        },
                    )
                    updated = db.get_profit_trade(trade_id)
                    return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
                try:
                    c5_evaluation_after_429 = (
                        _refresh_profit_trade_c5_evaluation_for_buy_order_fallback(
                            settings=settings,
                            config=config,
                            c5_client=c5_client,
                            market_hash_name=market_hash_name,
                            name=str(note.get("name") or market_hash_name),
                            steam_buy_price=steam_buy_price,
                        )
                    )
                except RuntimeError as exc:
                    reason = f"C5 market refresh failed before the Steam buy-order fallback: {exc}"
                    _cancel_pre_steam_buy_trade(
                        db,
                        row,
                        reason=reason,
                        source="profit_trade_buy_fallback_c5_refresh_guard",
                        extra_note={
                            "purchaseRequestSent": False,
                            "listingIdObtained": bool(getattr(buy_target, "listing_id", "")),
                            "purchaseRequestEvidence": "c5_refresh_failed_after_search_listings_fallback",
                            **search_listings_fallback_audit(),
                        },
                    )
                    updated = db.get_profit_trade(trade_id)
                    return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
                c5_evaluated_fallback_count = len(
                    search_listings_fallback_events
                )
                c5_refresh_required = False

            current_c5_risk_reason = _trade_c5_risk_block_reason(config, row)
            if (
                c5_evaluation_after_429 is not None
                and c5_evaluation_after_429.execution_status == "c5_risk_blocked"
            ):
                current_c5_risk_reason = (
                    "C5 risk no longer passes after Steam listings fallback"
                    + (
                        f" (HTTP {last_search_listings_fallback_status_code})"
                        if last_search_listings_fallback_status_code is not None
                        else ""
                    )
                    + ": "
                    f"{c5_evaluation_after_429.execution_reason}"
                )
            elif c5_evaluation_after_429 is not None:
                current_c5_risk_reason = None
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
                        **search_listings_fallback_audit(),
                        "c5RiskAfterSearchListings429": (
                            c5_evaluation_after_429.risk_reason
                            if c5_evaluation_after_429 is not None
                            else None
                        ),
                        "c5RiskAfterSearchListingsFallback": (
                            c5_evaluation_after_429.risk_reason
                            if c5_evaluation_after_429 is not None
                            else None
                        ),
                    },
                    update_fields=(
                        {
                            "c5_listing_price": c5_evaluation_after_429.c5_listing_price,
                            "c5_expected_net_price": c5_evaluation_after_429.c5_expected_net_price,
                        }
                        if c5_evaluation_after_429 is not None
                        else None
                    ),
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
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
                        **search_listings_fallback_audit(),
                    },
                    update_fields={
                        "steam_listing_id": buy_target.listing_id,
                        "steam_buy_price": steam_buy_price,
                        **(
                            {
                                "c5_listing_price": c5_evaluation_after_429.c5_listing_price,
                                "c5_expected_net_price": c5_evaluation_after_429.c5_expected_net_price,
                            }
                            if c5_evaluation_after_429 is not None
                            else {}
                        ),
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
                        **search_listings_fallback_audit(),
                    },
                    update_fields={
                        "steam_listing_id": buy_target.listing_id,
                        "steam_buy_price": steam_buy_price,
                        **(
                            {
                                "c5_listing_price": c5_evaluation_after_429.c5_listing_price,
                                "c5_expected_net_price": c5_evaluation_after_429.c5_expected_net_price,
                            }
                            if c5_evaluation_after_429 is not None
                            else {}
                        ),
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

            steam_cost_ratio = _profit_trade_steam_cost_ratio(config)
            steam_real_cost = steam_buy_price * steam_cost_ratio
            c5_expected_net = (
                c5_evaluation_after_429.c5_expected_net_price
                if c5_evaluation_after_429 is not None
                else safe_float(row["c5_expected_net_price"])
            )
            c5_listing_price = (
                c5_evaluation_after_429.c5_listing_price
                if c5_evaluation_after_429 is not None
                else safe_float(row["c5_listing_price"])
            )
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
                    c5_listing_price=c5_listing_price,
                    c5_expected_net_price=c5_expected_net,
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
                            **search_listings_fallback_audit(),
                            "searchListings429Recovered": bool(search_listings_429_events),
                            "searchListings400Recovered": bool(search_listings_400_events),
                            "searchListingsFallbackRecovered": bool(search_listings_fallback_events),
                        }
                    ),
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            manual_execution_approved = note.get("manualExecutionApproved") is True
            if (
                expected_roi < float(config.profit_trade_min_roi)
                or manual_execution_approved
            ):
                manual_block_reason = _manual_execution_roi_approval_block_reason(
                    note,
                    expected_roi=expected_roi,
                )
                if manual_block_reason is not None:
                    if expected_roi < float(config.profit_trade_min_roi):
                        reason = (
                            f"ROI no longer meets threshold before Steam buy: "
                            f"{expected_roi * 100:.2f}% < {config.profit_trade_min_roi * 100:.2f}%; "
                            f"{manual_block_reason}"
                        )
                    else:
                        reason = manual_block_reason
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
                            **search_listings_fallback_audit(),
                            "searchListings429Recovered": bool(search_listings_429_events),
                            "searchListings400Recovered": bool(search_listings_400_events),
                            "searchListingsFallbackRecovered": bool(search_listings_fallback_events),
                        },
                        update_fields={
                            "steam_listing_id": None,
                            "steam_buy_price": steam_buy_price,
                            "steam_balance_discount": float(steam_cost_ratio),
                            "steam_real_cost": steam_real_cost,
                            "c5_listing_price": c5_listing_price,
                            "c5_expected_net_price": c5_expected_net,
                            "expected_profit": expected_profit,
                            "expected_roi": expected_roi,
                        },
                    )
                    updated = db.get_profit_trade(trade_id)
                    return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
                if manual_execution_approved:
                    note = {
                        **note,
                        "manualExecutionRoiGuardPassedAt": utc_now_iso(),
                        "manualExecutionRoiAtBuy": round(expected_roi, 6),
                        "manualExecutionAutomaticMinRoi": float(config.profit_trade_min_roi),
                    }

            wallet_before_for_buy = selected_wallet_balance
            wallet_before_payload = selected_wallet or None
            wallet_spendable_before = (
                max(0.0, float(wallet_before_for_buy) - selected_reserved_balance)
                if wallet_before_for_buy is not None
                else None
            )
            # Cached preparation chooses a likely client without network I/O.
            # At the real buy boundary that chosen account is checked first;
            # only an insufficient live balance causes another account check.
            if not selected_wallet_is_live:
                try:
                    if steam_client is None:
                        selected_steam_id = str(
                            (
                                selected_account.steam_id64
                                if selected_account is not None
                                else getattr(client, "steam_id64", "")
                            )
                            or ""
                        ).strip()
                        live_selection = _select_live_steam_buy_account(
                            settings,
                            required_balance=buy_target.total_price,
                            preferred_steam_id=selected_steam_id,
                            account_reserved_balances=config.profit_trade_account_reserved_balances,
                            telemetry_context=telemetry_context,
                        )
                        selected_account = live_selection.account
                        client = live_selection.client
                        selected_wallet = live_selection.wallet
                        selected_wallet_balance = live_selection.wallet_balance
                        selected_reserved_balance = live_selection.reserved_balance
                        selected_spendable_balance = live_selection.spendable_balance
                        selected_wallet_is_live = True
                        refreshed_wallet_before = live_selection.wallet
                    else:
                        refreshed_wallet_before = _execution_wallet_balance(client)
                        _persist_shared_steam_wallet(
                            settings,
                            client,
                            refreshed_wallet_before,
                            account=selected_account,
                        )
                    refreshed_balance = safe_float(refreshed_wallet_before.get("balance"))
                    if refreshed_balance is None:
                        raise RuntimeError("Steam wallet response is missing balance")
                    selected_wallet_balance = refreshed_balance
                    selected_wallet = refreshed_wallet_before
                    selected_spendable_balance = max(
                        0.0,
                        float(refreshed_balance) - selected_reserved_balance,
                    )
                    selected_wallet_is_live = True
                    wallet_before_for_buy = refreshed_balance
                    wallet_before_payload = refreshed_wallet_before
                    wallet_spendable_before = selected_spendable_balance
                except Exception as exc:
                    raise RuntimeError(
                        f"Steam wallet final verification failed before purchase: {exc}"
                    ) from exc

            if (
                wallet_spendable_before is not None
                and wallet_spendable_before + 1e-9 < buy_target.total_price
            ):
                account_label = (
                    selected_account.name
                    if selected_account is not None
                    else str(getattr(client, "account_id", "") or "current Steam account")
                )
                raise RuntimeError(
                    f"Profit Trade wallet reserve blocked purchase for {account_label}: "
                    f"balance CNY {float(wallet_before_for_buy or 0.0):.2f}, "
                    f"reserved CNY {selected_reserved_balance:.2f}, "
                    f"spendable CNY {wallet_spendable_before:.2f}, "
                    f"required CNY {buy_target.total_price:.2f}"
                )

            # The live selector can switch from the cached preparation account
            # to A or another fallback. Inventory evidence must follow the
            # account that will actually send the purchase request.
            before_asset_ids: list[str] = []
            steam_id64 = str(getattr(client, "steam_id64", "") or "").strip()
            if steam_id64:
                before_asset_ids = db.list_asset_ids(market_hash_name, steam_id=steam_id64)

            purchase_guard_failure: dict[str, str] = {}

            def purchase_guard_reason(
                *,
                include_current_claim: bool,
                include_runtime: bool = False,
            ) -> str | None:
                if include_runtime and not _profit_trade_new_action_allowed(new_action_guard):
                    return "Profit Trade runtime, task lease or real-execution switch is no longer active"
                latest_config = (
                    load_strategy_config(settings)
                    if refresh_config_before_purchase
                    else config
                )
                if (
                    not latest_config.profit_trade_enabled
                    or not latest_config.profit_trade_allow_real_execution
                ):
                    return "Profit Trade real execution was disabled before the Steam purchase HTTP"
                latest_protection = _profit_trade_protection_reason(
                    latest_config,
                    asset_id=asset_id or None,
                    market_hash_name=market_hash_name,
                    steam_id=str(row["a_steam_id"] or "") or None,
                )
                if latest_protection is None:
                    latest_protection = _profit_trade_type_block_reason(
                        latest_config,
                        market_hash_name,
                    )
                if latest_protection is not None:
                    return f"latest Profit Trade protection blocked the purchase: {latest_protection}"

                latest_roi = _profit_trade_transfer_roi(
                    c5_expected_net=c5_expected_net,
                    steam_buy_price=steam_buy_price,
                    steam_cost_ratio=float(latest_config.profit_trade_balance_discount),
                )
                if latest_roi is None:
                    return "latest ROI could not be calculated before the Steam purchase"
                if (
                    float(latest_config.profit_trade_manual_review_roi) > 0
                    and latest_roi
                    > float(latest_config.profit_trade_manual_review_roi)
                ):
                    return (
                        "latest ROI exceeds the manual-review ceiling before Steam buy: "
                        f"{latest_roi * 100:.2f}%"
                    )
                if note.get("manualExecutionApproved") is True:
                    approval_reason = _manual_execution_roi_approval_block_reason(
                        note,
                        expected_roi=latest_roi,
                    )
                    if approval_reason is not None:
                        return approval_reason
                elif latest_roi < float(latest_config.profit_trade_min_roi):
                    return (
                        "latest ROI no longer meets the automatic threshold before Steam buy: "
                        f"{latest_roi * 100:.2f}% < "
                        f"{latest_config.profit_trade_min_roi * 100:.2f}%"
                    )

                guard_db = Database(settings.db_path)
                try:
                    guard_db.initialize()
                    account_id = str(
                        selected_account.id
                        if selected_account is not None
                        else getattr(client, "account_id", "")
                        or ""
                    ).strip()
                    if account_id:
                        health = guard_db.get_steam_cookie_health(account_id)
                        if health is not None and str(health["status"] or "") != "valid":
                            return (
                                f"Steam Cookie is not valid for purchase account {account_id}"
                            )
                    daily_budget = max(
                        0.0,
                        float(latest_config.profit_trade_daily_steam_budget),
                    )
                    if daily_budget > 0:
                        if include_current_claim:
                            committed = _profit_trade_daily_steam_committed_through(
                                guard_db,
                                trade_id=trade_id,
                            )
                        else:
                            committed = (
                                _profit_trade_daily_steam_spent(guard_db)
                                + float(buy_target.total_price)
                            )
                        if committed > daily_budget + 1e-9:
                            return (
                                "daily Steam budget would be exceeded at the final purchase price: "
                                f"CNY {committed:.2f} > CNY {daily_budget:.2f}"
                            )
                finally:
                    guard_db.close()
                return None

            def final_purchase_guard() -> bool:
                reason = purchase_guard_reason(
                    include_current_claim=True,
                    include_runtime=True,
                )
                if reason is None:
                    purchase_guard_failure.pop("reason", None)
                    return True
                purchase_guard_failure["reason"] = reason
                return False

            initial_purchase_guard_reason = purchase_guard_reason(
                include_current_claim=False
            )
            if initial_purchase_guard_reason is not None:
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason=initial_purchase_guard_reason,
                    source="profit_trade_final_purchase_guard",
                    update_fields={
                        "steam_listing_id": None,
                        "steam_buy_price": steam_buy_price,
                        "steam_balance_discount": float(steam_cost_ratio),
                        "steam_real_cost": steam_real_cost,
                        "c5_listing_price": c5_listing_price,
                        "c5_expected_net_price": c5_expected_net,
                        "expected_profit": expected_profit,
                        "expected_roi": expected_roi,
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

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

            stopped = stop_if_runtime_disabled("before_purchase_request")
            if stopped is not None:
                return stopped
            steam_buy_requested_at = utc_now_iso()
            purchase_hold_note = _build_note(
                {
                    "source": "profit_trade_purchase_request_hold",
                    "tradeId": trade_id,
                    "heldAt": steam_buy_requested_at,
                    "reason": "A must remain locked from before Steam request until terminal reconciliation",
                }
            )
            if not db.update_asset_reservation_deadline(
                asset_id=asset_id,
                owner=PROFIT_TRADE_OWNER,
                operation_id=trade_id,
                reserved_until=None,
                note=purchase_hold_note,
            ):
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason="A asset reservation could not be extended before Steam buy request",
                    source="profit_trade_purchase_request_hold_failed",
                    update_fields={
                        "step_key": "asset_locked",
                        "step_index": 2,
                        "steam_listing_id": None,
                    },
                    extra_note={
                        "purchaseRequestSent": False,
                        "purchaseHoldFailedAt": steam_buy_requested_at,
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            db.update_profit_trade(
                trade_id,
                status="buying",
                step_key="steam_bought",
                step_index=3,
                steam_buy_price=steam_buy_price,
                _event_reason="Steam 购买请求即将发送",
                _event_context={
                    "steamBuyMethod": buy_method,
                    "steamBuyPrice": round(steam_buy_price, 2),
                    "listingIdObtained": bool(
                        buy_method == "buylisting" and buy_target.listing_id
                    ),
                    "purchaseRequestSent": False,
                },
                note=_build_note(
                    {
                        **note,
                        "steamBuyRequestedAt": steam_buy_requested_at,
                        "steamBuyPrice": round(steam_buy_price, 2),
                        "purchaseRequestSent": False,
                    }
                ),
            )
            claimed_purchase_guard_reason = purchase_guard_reason(
                include_current_claim=True
            )
            if claimed_purchase_guard_reason is not None:
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason=claimed_purchase_guard_reason,
                    source="profit_trade_final_purchase_guard",
                    update_fields={
                        "steam_listing_id": None,
                        "steam_buy_price": steam_buy_price,
                        "steam_balance_discount": float(steam_cost_ratio),
                        "steam_real_cost": steam_real_cost,
                        "c5_listing_price": c5_listing_price,
                        "c5_expected_net_price": c5_expected_net,
                        "expected_profit": expected_profit,
                        "expected_roi": expected_roi,
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            latest_row = db.get_profit_trade(trade_id) or row
            note = _read_note(latest_row["note"])
            long_buy_direct_gate = (
                _prepare_profit_trade_long_buy_for_direct_purchase(
                    db,
                    settings,
                    config,
                    market_hash_name=market_hash_name,
                    steam_client=client,
                    new_action_guard=new_action_guard,
                    orderbook_crossed=(
                        _compact_steam_orderbook_snapshot(orderbook_payload).get(
                            "crossed"
                        )
                        is True
                    ),
                )
            )
            if not bool(long_buy_direct_gate.get("ok")):
                reason = str(
                    long_buy_direct_gate.get("reason")
                    or "managed long-term buy order blocked the direct purchase"
                )
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason=reason,
                    source="profit_trade_long_buy_direct_purchase_guard",
                    update_fields={
                        "steam_listing_id": None,
                        "steam_buy_price": steam_buy_price,
                        "steam_balance_discount": float(steam_cost_ratio),
                        "steam_real_cost": steam_real_cost,
                        "c5_listing_price": c5_listing_price,
                        "c5_expected_net_price": c5_expected_net,
                        "expected_profit": expected_profit,
                        "expected_roi": expected_roi,
                    },
                    extra_note={
                        **{
                            key: value
                            for key, value in note.items()
                            if key
                            not in PROFIT_TRADE_PURCHASE_REQUEST_EVIDENCE_NOTE_KEYS
                        },
                        "purchaseRequestSent": False,
                        "listingIdObtained": bool(
                            getattr(buy_target, "listing_id", "")
                        ),
                        "managedLongBuyDirectGate": sanitize_public_payload(
                            long_buy_direct_gate
                        ),
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {
                    "ok": False,
                    "changed": True,
                    "trade": _trade_row_to_dict(updated),
                    "longBuyFillIds": list(
                        long_buy_direct_gate.get("fillIds") or []
                    ),
                    "longBuyDirectGate": sanitize_public_payload(
                        long_buy_direct_gate
                    ),
                }
            if long_buy_direct_gate.get("outcome") == "cancelled":
                note.update(
                    {
                        "managedLongBuyCancelledBeforeDirectPurchase": True,
                        "managedLongBuyCancelledAt": utc_now_iso(),
                        "managedLongBuyOrderId": long_buy_direct_gate.get(
                            "orderId"
                        ),
                        "managedLongBuySteamOrderId": long_buy_direct_gate.get(
                            "buyOrderId"
                        ),
                    }
                )
                db.update_profit_trade(
                    trade_id,
                    note=_build_note(note),
                )
                latest_row = db.get_profit_trade(trade_id) or row
                note = _read_note(latest_row["note"])
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
                        execution_guard=final_purchase_guard,
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
                        execution_guard=final_purchase_guard,
                    )
            except SteamRequestGuardRejected as exc:
                reason = purchase_guard_failure.get("reason") or (
                    "Profit Trade runtime was disabled while the Steam request was queued; "
                    "the purchase callback was rejected before HTTP send"
                )
                _cancel_pre_steam_buy_trade(
                    db,
                    row,
                    reason=reason,
                    source="profit_trade_runtime_disabled",
                    update_fields={
                        "step_key": "asset_locked",
                        "step_index": 2,
                        "steam_listing_id": None,
                    },
                    extra_note={
                        **{
                            key: value
                            for key, value in note.items()
                            if key
                            not in PROFIT_TRADE_PURCHASE_REQUEST_EVIDENCE_NOTE_KEYS
                        },
                        "runtimeDisabledAt": utc_now_iso(),
                        "runtimeDisabledStage": "scheduler_before_purchase_http",
                        "purchaseRequestSent": False,
                        "listingIdObtained": bool(
                            buy_method == "buylisting" and buy_target.listing_id
                        ),
                        "purchaseRequestEvidence": "scheduler_guard_rejected_before_http",
                        "schedulerGuardError": str(exc),
                    },
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
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
                            "steamBuyRequestedAt": steam_buy_requested_at,
                            "steamBuyMethod": buy_method,
                            "purchaseRequestSent": True,
                            "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                            "steamBuyPrice": round(steam_buy_price, 2),
                            "steamAccountId": selected_account.id if selected_account else getattr(client, "account_id", None),
                            "steamAccountName": selected_account.name if selected_account else None,
                            "walletBalanceBefore": wallet_before_for_buy,
                            "walletReservedBalance": selected_reserved_balance,
                            "walletSpendableBefore": wallet_spendable_before,
                            "activeBuyOrdersBefore": active_buy_orders_before,
                            "failedSteamListingIds": sorted(failed_listing_ids),
                            "staleSteamListingAttempts": stale_listing_attempts,
                            **search_listings_fallback_audit(),
                            "searchListings429Recovered": bool(search_listings_429_events),
                            "searchListings400Recovered": bool(search_listings_400_events),
                            "searchListingsFallbackRecovered": bool(search_listings_fallback_events),
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
            latest_row = db.get_profit_trade(trade_id) or row
            latest_note = _read_note(latest_row["note"])
            request_returned_at = utc_now_iso()
            db.update_profit_trade(
                trade_id,
                steam_listing_id=steam_buy_reference_id,
                note=_build_note(
                    {
                        **latest_note,
                        "steamBuyRequestedAt": steam_buy_requested_at,
                        "steamBuyRequestReturnedAt": request_returned_at,
                        "steamBuyMethod": buy_method,
                        "purchaseRequestSent": True,
                        "listingIdObtained": bool(
                            buy_method == "buylisting" and buy_target.listing_id
                        ),
                        "steamListingId": (
                            buy_target.listing_id
                            if buy_method == "buylisting"
                            else None
                        ),
                        "steamBuyOrderId": buy_order_id or None,
                        "steamBuyPrice": round(steam_buy_price, 2),
                    }
                ),
            )
            db.add_profit_trade_audit_event(
                trade_id,
                event_type="steam_purchase_request_returned",
                reason=(
                    "Steam 求购创建请求已返回"
                    if buy_method == "createbuyorder"
                    else "Steam listing 购买请求已返回"
                ),
                context={
                    "steamBuyMethod": buy_method,
                    "steamBuyPrice": round(steam_buy_price, 2),
                    "steamListingId": (
                        buy_target.listing_id if buy_method == "buylisting" else None
                    ),
                    "steamBuyOrderId": buy_order_id or None,
                    "purchaseRequestSent": True,
                    "requestReturnedAt": request_returned_at,
                },
            )

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
                purchase_requested_at=steam_buy_requested_at,
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
                    resolution = _cancel_and_resolve_steam_buy_order(
                        client,
                        market_hash_name=market_hash_name,
                        expected_total=buy_target.total,
                        wallet_before_balance=wallet_before_for_buy,
                        buy_order_id=buy_order_id,
                        purchase_requested_at=steam_buy_requested_at,
                        actual_total_hint=verification.wallet_delta,
                    )
                    verification = resolution.verification
                    buy_order_cancel_payload = resolution.cancel_payload
                    buy_order_cancel_error = resolution.cancel_error
                    reason = (
                        "Steam buy request succeeded but purchase completion is not verified"
                        if not verification.reason
                        else f"Steam buy request succeeded but purchase completion is not verified: {verification.reason}"
                    )
                    unverified_buy_order_attempts.append(
                        {
                            "steamBuyOrderId": buy_order_id,
                            "steamBuyPrice": round(steam_buy_price, 2),
                            "unverifiedAt": utc_now_iso(),
                            "reason": verification.reason,
                            "resolution": resolution.outcome,
                            "cancelled": resolution.outcome == "cancelled",
                            "cancelError": buy_order_cancel_error,
                            "cancelPayload": buy_order_cancel_payload,
                            "activeBuyOrdersAfterCancel": verification.active_buy_orders,
                            "steamBuyVerifiedBy": verification.verified_by,
                            "steamPurchaseReceipt": verification.purchase_receipt,
                        }
                    )
                    if resolution.outcome == "purchased":
                        break
                    cancellation_confirmed_at = (
                        utc_now_iso()
                        if resolution.outcome == "cancelled"
                        else None
                    )
                    if cancellation_confirmed_at is not None:
                        db.add_profit_trade_audit_event(
                            trade_id,
                            event_type="steam_buy_order_cancelled",
                            reason="Steam 未成交求购已撤销并确认终态",
                            context={
                                "steamBuyMethod": buy_method,
                                "steamBuyOrderId": buy_order_id,
                                "steamBuyPrice": round(steam_buy_price, 2),
                                "cancellationConfirmedAt": cancellation_confirmed_at,
                                "retryRemaining": max(
                                    0,
                                    buy_order_retry_budget - 1,
                                ),
                            },
                        )
                    if resolution.outcome == "cancelled" and buy_order_retry_budget > 1:
                        buy_order_retry_budget -= 1
                        refresh_market_snapshot = True
                        continue
                    if resolution.outcome == "cancelled":
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
                            _event_reason=reason,
                            _event_context={
                                "cancelSource": "profit_trade_buy_order_unverified_cancel",
                                "steamBuyMethod": buy_method,
                                "steamBuyOrderId": buy_order_id,
                                "purchaseRequestSent": True,
                                "cancellationConfirmedAt": cancellation_confirmed_at,
                            },
                            steam_listing_id=steam_buy_reference_id,
                            steam_buy_price=steam_buy_price,
                            note=_build_note(
                                {
                                    **latest_note,
                                    "steamBuyUnverifiedAt": utc_now_iso(),
                                    "steamBuyRequestedAt": steam_buy_requested_at,
                                    "steamBuyMethod": buy_method,
                                    "purchaseRequestSent": True,
                                    "steamListingId": None,
                                    "steamBuyOrderId": buy_order_id,
                                    "steamBuyPrice": round(steam_buy_price, 2),
                                    "walletBalanceBefore": wallet_before_for_buy,
                                    "walletBefore": wallet_before_payload,
                                    "walletAfter": verification.wallet_after,
                                    "walletDelta": verification.wallet_delta,
                                    "activeBuyOrdersBefore": active_buy_orders_before,
                                    "activeBuyOrdersAfter": verification.active_buy_orders,
                                    "activeBuyOrdersAfterCancel": verification.active_buy_orders,
                                    "steamBuyVerifiedBy": verification.verified_by,
                                    "beforeAssetIds": before_asset_ids,
                                    "inventoryAfterAssetIds": verification.inventory_after_asset_ids,
                                    "newInventoryAssetIds": verification.new_inventory_asset_ids,
                                    "steamBuyPayload": payload if isinstance(payload, dict) else None,
                                    "steamBuyOrderCancelledAt": cancellation_confirmed_at,
                                    "steamBuyOrderCancellationConfirmedAt": cancellation_confirmed_at,
                                    "steamBuyOrderCancelPayload": buy_order_cancel_payload,
                                    "unverifiedBuyOrderAttempts": unverified_buy_order_attempts,
                                    "failedSteamListingIds": sorted(failed_listing_ids),
                                    "staleSteamListingAttempts": stale_listing_attempts,
                                    **search_listings_fallback_audit(),
                                    "searchListings429Recovered": bool(search_listings_429_events),
                                    "searchListings400Recovered": bool(search_listings_400_events),
                                    "searchListingsFallbackRecovered": bool(search_listings_fallback_events),
                                    "cancelReason": reason,
                                    "cancelSource": "profit_trade_buy_order_unverified_cancel",
                                    "steamBuyListingSnapshot": _compact_steam_listing_snapshot(listings_payload, buy_target),
                                    "steamBuyOrderbookSnapshot": _compact_steam_orderbook_snapshot(orderbook_payload),
                                }
                            ),
                        )
                        updated = db.get_profit_trade(trade_id)
                        return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
                # The purchase request reached Steam, but its terminal state
                # is uncertain. Never let the short scan reservation expire:
                # the same A must not create another trade until recovery or
                # a confirmed cancellation resolves this request.
                if asset_id:
                    db.update_asset_reservation_deadline(
                        asset_id=asset_id,
                        owner=PROFIT_TRADE_OWNER,
                        operation_id=trade_id,
                        reserved_until=None,
                        note=_build_note(
                            {
                                "source": "profit_trade_unverified_buy_hold",
                                "tradeId": trade_id,
                                "steamBuyOrderId": buy_order_id or None,
                                "heldAt": utc_now_iso(),
                                "reason": reason,
                            }
                        ),
                    )
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
                            "steamBuyRequestedAt": steam_buy_requested_at,
                            "steamBuyMethod": buy_method,
                            "purchaseRequestSent": True,
                            "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                            "steamBuyOrderId": buy_order_id or None,
                            "steamBuyPrice": round(steam_buy_price, 2),
                            "walletBalanceBefore": wallet_before_for_buy,
                            "walletBefore": wallet_before_payload,
                            "walletAfter": verification.wallet_after,
                            "walletDelta": verification.wallet_delta,
                            "activeBuyOrdersBefore": active_buy_orders_before,
                            "activeBuyOrdersAfter": verification.active_buy_orders,
                            "activeBuyOrdersAfterCancel": verification.active_buy_orders,
                            "steamBuyVerifiedBy": verification.verified_by,
                            "beforeAssetIds": before_asset_ids,
                            "inventoryAfterAssetIds": verification.inventory_after_asset_ids,
                            "newInventoryAssetIds": verification.new_inventory_asset_ids,
                            "steamBuyPayload": payload if isinstance(payload, dict) else None,
                            "steamBuyOrderCancelError": buy_order_cancel_error,
                            "steamBuyOrderCancelPayload": buy_order_cancel_payload,
                            "steamPurchaseReceipt": verification.purchase_receipt,
                            "unverifiedBuyOrderAttempts": unverified_buy_order_attempts,
                            "failedSteamListingIds": sorted(failed_listing_ids),
                            "staleSteamListingAttempts": stale_listing_attempts,
                            **search_listings_fallback_audit(),
                            "searchListings429Recovered": bool(search_listings_429_events),
                            "searchListings400Recovered": bool(search_listings_400_events),
                            "searchListingsFallbackRecovered": bool(search_listings_fallback_events),
                            "steamBuyListingSnapshot": _compact_steam_listing_snapshot(listings_payload, buy_target),
                            "steamBuyOrderbookSnapshot": _compact_steam_orderbook_snapshot(orderbook_payload),
                        }
                    ),
                )
                updated = db.get_profit_trade(trade_id)
                return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}
            break

        steam_buy_maximum_price = float(steam_buy_price)
        receipt_paid_total = safe_float((verification.purchase_receipt or {}).get("paidTotal"))
        wallet_paid_total = safe_float(verification.wallet_delta)
        actual_paid_total = receipt_paid_total
        if (
            actual_paid_total is None
            and buy_method == "createbuyorder"
            and wallet_paid_total is not None
            and wallet_paid_total > 0
            and wallet_paid_total <= steam_buy_maximum_price + 0.02
        ):
            actual_paid_total = wallet_paid_total
        if actual_paid_total is not None and actual_paid_total > 0:
            steam_buy_price = round(float(actual_paid_total), 2)
            steam_real_cost = float(steam_buy_price) * float(steam_cost_ratio)
            expected_profit = float(c5_expected_net) - steam_real_cost
            expected_roi = _profit_trade_transfer_roi(
                c5_expected_net=float(c5_expected_net),
                steam_buy_price=float(steam_buy_price),
                steam_cost_ratio=float(steam_cost_ratio),
            )

        verified_b_asset_ids = [
            str(value).strip()
            for value in (verification.new_inventory_asset_ids or [])
            if str(value or "").strip()
        ]
        verified_b_asset_id = verified_b_asset_ids[0] if len(set(verified_b_asset_ids)) == 1 else None
        hold_note = _build_note(
            {
                "source": "profit_trade_buy",
                "tradeId": trade_id,
                "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                "steamBuyOrderId": buy_order_id or None,
                "steamBuyMethod": buy_method,
                "steamBuyRequestedAt": steam_buy_requested_at,
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
                        "steamBuyRequestedAt": steam_buy_requested_at,
                        "purchaseRequestSent": True,
                        "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                        "steamBuyOrderId": buy_order_id or None,
                        "steamPurchaseReceipt": verification.purchase_receipt,
                        "walletInfo": payload.get("wallet_info") if isinstance(payload, dict) else None,
                        **search_listings_fallback_audit(),
                        "searchListings429Recovered": bool(search_listings_429_events),
                        "searchListings400Recovered": bool(search_listings_400_events),
                        "searchListingsFallbackRecovered": bool(search_listings_fallback_events),
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
            _event_reason="Steam 购买完成已取得终态证据",
            _event_context={
                "steamBuyMethod": buy_method,
                "steamBuyOrderId": buy_order_id or None,
                "steamListingId": (
                    buy_target.listing_id if buy_method == "buylisting" else None
                ),
                "purchaseRequestSent": True,
                "steamBuyVerifiedBy": verification.verified_by,
                "bAssetId": verified_b_asset_id,
            },
            b_asset_id=verified_b_asset_id,
            steam_listing_id=steam_buy_reference_id,
            steam_buy_price=steam_buy_price,
            steam_balance_discount=float(steam_cost_ratio),
            steam_real_cost=steam_real_cost,
            c5_listing_price=c5_listing_price,
            c5_expected_net_price=c5_expected_net,
            expected_profit=expected_profit,
            expected_roi=expected_roi,
            error=None,
            note=_build_note(
                {
                    **note,
                    "steamBuySucceededAt": utc_now_iso(),
                    "steamBuyRequestedAt": steam_buy_requested_at,
                    "steamBuyMethod": buy_method,
                    "purchaseRequestSent": True,
                    "steamListingId": buy_target.listing_id if buy_method == "buylisting" else None,
                    "steamBuyOrderId": buy_order_id or None,
                    "steamBuyPrice": round(float(steam_buy_price), 2),
                    "steamBuyMaximumPrice": round(steam_buy_maximum_price, 2),
                    "steamBuyActualPrice": round(float(steam_buy_price), 2),
                    "subtotal": buy_target.subtotal,
                    "fee": buy_target.fee,
                    "total": buy_target.total,
                    "steamCostRatio": round(steam_cost_ratio, 4),
                    "steamId": steam_id64 or None,
                    "steamAccountId": selected_account.id if selected_account else getattr(client, "account_id", None),
                    "steamAccountName": selected_account.name if selected_account else None,
                    "walletBalanceBefore": wallet_before_for_buy,
                    "walletReservedBalance": selected_reserved_balance,
                    "walletSpendableBefore": wallet_spendable_before,
                    "walletBefore": wallet_before_payload,
                    "walletAfter": verification.wallet_after,
                    "walletDelta": verification.wallet_delta,
                    "activeBuyOrdersBefore": active_buy_orders_before,
                    "activeBuyOrdersAfter": verification.active_buy_orders,
                    "steamBuyVerifiedBy": verification.verified_by,
                    "beforeAssetIds": before_asset_ids,
                    "inventoryAfterAssetIds": verification.inventory_after_asset_ids,
                    "newInventoryAssetIds": verification.new_inventory_asset_ids,
                    "steamPurchaseReceipt": verification.purchase_receipt,
                    "walletInfo": payload.get("wallet_info") if isinstance(payload, dict) else None,
                    "failedSteamListingIds": sorted(failed_listing_ids),
                    "staleSteamListingAttempts": stale_listing_attempts,
                    "unverifiedBuyOrderAttempts": unverified_buy_order_attempts,
                    **search_listings_fallback_audit(),
                    "searchListings429Recovered": bool(search_listings_429_events),
                    "searchListings400Recovered": bool(search_listings_400_events),
                    "searchListingsFallbackRecovered": bool(search_listings_fallback_events),
                    "listingsCircuitFallbackToBuyOrder": listings_cooldown_mode,
                    "listingsCircuit": listings_circuit,
                    "c5ListingPrice": round(c5_listing_price, 2),
                    "c5ExpectedNetPrice": round(c5_expected_net, 2),
                    "c5RiskAfterSearchListings429": (
                        c5_evaluation_after_429.risk_reason
                        if c5_evaluation_after_429 is not None
                        else None
                    ),
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
            c5_client = _build_profit_trade_c5_client(
                settings,
                **_profit_trade_telemetry_context(row),
            )

        try:
            statistics = _fetch_c5_recent_sale_risks(
                c5_client,
                app_id=settings.app_id,
                market_hash_names=[market_hash_name],
            ).get(market_hash_name)
            depth = _fetch_c5_price_batch_reference(
                c5_client,
                settings,
                market_hash_name=market_hash_name,
            )
            depth_ok, depth_reason = _evaluate_c5_orderbook_depth_risk(config, depth=depth)
            if not depth_ok:
                raise RuntimeError(depth_reason)
            competitor_reference = safe_float(depth.get("effectiveReferencePrice"))
            if competitor_reference is None or competitor_reference <= 0:
                raise RuntimeError("C5 competitor reference is unavailable before listing")
            sale_price = _profit_trade_initial_listing_price(
                config,
                competitor_reference_price=competitor_reference,
                fallback_price=float(sale_price),
            )
            market_stats = _merge_c5_listing_depth_and_statistics(
                depth=depth,
                statistics=statistics,
            )
            evaluated_market_stats = market_stats
            if config.profit_trade_require_c5_market_depth:
                evaluated_market_stats = _evaluate_c5_market_depth_risk(
                    config,
                    c5_listing_price=sale_price,
                    risk=market_stats,
                )
                if evaluated_market_stats.status != "passed":
                    raise RuntimeError(evaluated_market_stats.reason)
            if config.profit_trade_require_c5_recent_sales:
                evaluated_recent = _evaluate_c5_recent_sale_risk(
                    config,
                    c5_listing_price=sale_price,
                    risk=market_stats,
                )
                if evaluated_recent.status != "passed":
                    raise RuntimeError(evaluated_recent.reason)
            steam_buy_price = safe_float(row["steam_buy_price"])
            steam_cost_ratio = safe_float(row["steam_balance_discount"]) or _profit_trade_steam_cost_ratio(config)
            if steam_buy_price is None or steam_buy_price <= 0:
                raise RuntimeError("trade missing usable Steam buy price before C5 listing")
            expected_net = sale_price * float(config.profit_trade_c5_current_sale_net_factor)
            expected_profit, expected_roi = _realized_values(
                sold_net_price=expected_net,
                steam_buy_price=steam_buy_price,
                steam_cost_ratio=float(steam_cost_ratio),
            )
            # The irreversible Steam purchase has already happened.  The open
            # ROI floor and high-ROI review gate protect whether to buy B; they
            # must not strand an already-bought trade before C5 listing.  At
            # this boundary only a negative ROI may stop the listing, using
            # the same four-decimal normalization as manual approval checks.
            pre_list_roi_gate = _profit_trade_roi_gate_value(expected_roi)
            pre_list_roi_floor = 0.0
            note = {
                **note,
                "preListExpectedRoiRaw": float(expected_roi),
                "preListExpectedRoiGate": pre_list_roi_gate,
                "preListRoiFloor": pre_list_roi_floor,
                "preListRoiFloorSource": "post_steam_buy_non_negative",
            }
            if pre_list_roi_gate < pre_list_roi_floor:
                raise RuntimeError(
                    f"pre-list ROI {expected_roi * 100:.4f}% "
                    f"(four-decimal gate {pre_list_roi_gate * 100:.4f}%) < "
                    "post-Steam-buy minimum 0.0000%"
                )
            note = {
                **note,
                "c5Pricing": depth,
                "preListPricingAt": utc_now_iso(),
                "preListCompetitorReference": competitor_reference,
                "preListC5ListingPrice": sale_price,
                "preListExpectedNetPrice": round(expected_net, 2),
                "preListExpectedRoi": round(expected_roi, 4),
                "preListMarketStats": _c5_risk_note(evaluated_market_stats),
            }
        except Exception as exc:
            reason = f"C5 pre-list pricing failed or became unsafe: {exc}"
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                error=reason,
                note=_build_note(
                    {
                        **note,
                        "c5PreListPricingFailedAt": utc_now_iso(),
                        "c5PreListPricingFailure": str(exc),
                    }
                ),
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

        listing_requested_at = utc_now_iso()
        note = {
            **note,
            "c5ListingRequestedAt": listing_requested_at,
            "c5ListingRequestedPrice": round(float(sale_price), 2),
            "c5ListingPrice": round(float(sale_price), 2),
            "c5ExpectedNetPrice": round(expected_net, 2),
            "expectedProfit": round(expected_profit, 2),
            "expectedRoi": round(expected_roi, 4),
        }
        db.update_profit_trade(
            trade_id,
            status="listing_c5",
            step_key="c5_listed",
            step_index=4,
            c5_listing_price=sale_price,
            c5_expected_net_price=expected_net,
            expected_profit=expected_profit,
            expected_roi=expected_roi,
            note=_build_note(note),
        )
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
        except Exception as exc:
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
        listing_failed_reason: str | None = None
        if isinstance(payload, dict):
            failed_list = payload.get("failedList")
            success_list = payload.get("successList")
            if failed_list:
                listing_failed_reason = f"C5 listing failed: {failed_list}"
            elif payload.get("succeed") == 0:
                listing_failed_reason = "C5 listing failed: C5 returned succeed=0"
            elif isinstance(success_list, list) and not success_list:
                listing_failed_reason = "C5 listing failed: empty successList"
        if not product_id and listing_failed_reason is None:
            listing_failed_reason = "C5 listing returned no product id; outcome is uncertain"
        if listing_failed_reason is not None:
            latest = db.get_profit_trade(trade_id) or row
            latest_note = _read_note(latest["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                step_key="c5_listed",
                error=listing_failed_reason,
                note=_build_note(
                    {
                        **latest_note,
                        "c5ListingFailedAt": utc_now_iso(),
                        "c5ListingFailure": listing_failed_reason,
                        "c5Raw": payload,
                    }
                ),
            )
            updated = db.get_profit_trade(trade_id)
            return {"ok": False, "changed": True, "trade": _trade_row_to_dict(updated)}

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
        listed_at = str(note.get("c5FirstListedAt") or note.get("c5ListedAt") or utc_now_iso())
        merged_note = {
            **note,
            "c5FirstListedAt": listed_at,
            "c5ListedAt": listed_at,
            "initialC5ListingPrice": safe_float(note.get("initialC5ListingPrice")) or round(float(sale_price), 2),
            "initialC5Reference": {
                "price": safe_float(note.get("preListCompetitorReference")),
                "type": (note.get("c5Pricing") or {}).get("referenceSource") if isinstance(note.get("c5Pricing"), dict) else None,
                "confidence": (note.get("c5Pricing") or {}).get("referenceConfidence") if isinstance(note.get("c5Pricing"), dict) else None,
                "observedAt": note.get("preListPricingAt"),
            },
            "c5ProductId": product_id,
            "c5SalePrice": round(float(sale_price), 2),
            "c5ListingPrice": round(float(sale_price), 2),
            "c5ExpectedNetPrice": round(expected_net, 2),
            "expectedProfit": round(expected_profit, 2),
            "expectedRoi": round(expected_roi, 4),
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
            c5_listing_price=sale_price,
            c5_expected_net_price=expected_net,
            expected_profit=expected_profit,
            expected_roi=expected_roi,
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
        manual_rows = [
            row
            for row in db.list_profit_trades(status="manual_required", limit=500)
            if str(row["step_key"] or "") == "c5_listed"
            and str(row["c5_product_id"] or _read_note(row["note"]).get("c5ProductId") or "").strip()
        ]
        if manual_rows:
            seen_ids = {int(row["id"]) for row in rows}
            rows.extend(row for row in manual_rows if int(row["id"]) not in seen_ids)
    except Exception as exc:
        event_logger.emit(
            level="ERROR",
            provider="local",
            component="profit_trade_scan",
            operation="run_failed",
            message="Profit Trade scan failed",
            run_id=scan_id,
            exception_type=type(exc).__name__,
            stack_trace=traceback.format_exc(),
            safe_context={"error": str(exc)},
        )
        raise
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
        c5_client = _build_profit_trade_c5_client(
            settings,
            run_id=f"PTSALE-{uuid.uuid4().hex}",
        )

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
            asset_id = str(row["a_asset_id"] or note.get("assetId") or "").strip()
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
            if sold_order is None and asset_id:
                sold_order = seller_order_lookup.sold_orders_by_asset_id.get(asset_id)
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
                wait_seconds = max(
                    0.0,
                    float(config.profit_trade_sale_sync_initial_grace_seconds),
                )
                if (now - listed_at.astimezone(timezone.utc)).total_seconds() < wait_seconds:
                    skipped_ids.append(trade_id)
                    continue

            if seller_order_lookup.covers(steam_id) and active_lookup.covers(steam_id):
                first_missing_value = (
                    note.get("c5SaleSyncPendingFirstAt")
                    or note.get("activeSaleMissingFirstAt")
                    or note.get("settlementBlockedAt")
                )
                first_missing_at = _parse_iso(str(first_missing_value or ""))
                if first_missing_at is not None and first_missing_at.tzinfo is None:
                    first_missing_at = first_missing_at.replace(tzinfo=timezone.utc)
                if first_missing_at is None:
                    first_missing_at = now
                pending_age_seconds = (now - first_missing_at.astimezone(timezone.utc)).total_seconds()
                pending_count = safe_int(note.get("c5SaleSyncPendingProbeCount")) or 0
                if pending_age_seconds < C5_SALE_SYNC_PENDING_MAX_SECONDS:
                    reason = (
                        "C5售出同步等待中：在售列表已无此商品，但卖家订单暂未返回匹配的 productId/assetId，程序会继续自动复查。"
                    )
                    db.update_profit_trade(
                        trade_id,
                        status="c5_listed",
                        step_key="c5_listed",
                        step_index=4,
                        error=reason,
                        completed_at=None,
                        c5_sold_net_price=None,
                        realized_profit=None,
                        realized_roi=None,
                        note=_build_note(
                            {
                                **note,
                                "c5SaleSyncPendingFirstAt": first_missing_at.astimezone(timezone.utc).isoformat(),
                                "c5SaleSyncPendingLastAt": utc_now_iso(),
                                "c5SaleSyncPendingProbeCount": pending_count + 1,
                                "c5SaleSyncPendingMaxSeconds": C5_SALE_SYNC_PENDING_MAX_SECONDS,
                                "c5SaleSyncPendingReason": reason,
                                "missingSellerOrderProductId": product_id,
                                "activeSaleMissingProductId": product_id,
                                "activeSaleMissingAssetId": asset_id or None,
                            }
                        ),
                    )
                    skipped_ids.append(trade_id)
                    continue
                reason = "C5 listed product has been missing for more than 3 hours, but no matching seller sold order was found"
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error=reason,
                    completed_at=None,
                    c5_sold_net_price=None,
                    realized_profit=None,
                    realized_roi=None,
                    note=_build_note(
                        {
                            **note,
                            "settlementBlockedAt": utc_now_iso(),
                            "settlementBlockedReason": reason,
                            "c5SaleSyncPendingFirstAt": first_missing_at.astimezone(timezone.utc).isoformat(),
                            "c5SaleSyncPendingLastAt": utc_now_iso(),
                            "c5SaleSyncPendingProbeCount": pending_count + 1,
                            "c5SaleSyncPendingMaxSeconds": C5_SALE_SYNC_PENDING_MAX_SECONDS,
                            "missingSellerOrderProductId": product_id,
                            "activeSaleMissingProductId": product_id,
                            "activeSaleMissingAssetId": asset_id or None,
                        }
                    ),
                )
                errors.append(f"trade {trade_id}: {reason}")
                continue
            if seller_order_lookup.covers(steam_id) and not active_lookup.covers(steam_id):
                skipped_ids.append(trade_id)
                errors.append(f"trade {trade_id}: active C5 sale lookup did not cover steamId {steam_id}")
                continue
            reason = (
                "C5 seller sold-order lookup did not cover the Steam account; "
                "settlement evidence unavailable"
            )
            db.update_profit_trade(
                trade_id,
                status="c5_listed",
                step_key="c5_listed",
                step_index=4,
                error=reason,
                completed_at=None,
                c5_sold_net_price=None,
                realized_profit=None,
                realized_roi=None,
                note=_build_note(
                    {
                        **note,
                        "c5SaleSyncPendingLastAt": utc_now_iso(),
                        "settlementBlockedAt": utc_now_iso(),
                        "settlementBlockedReason": reason,
                        "activeSaleMissingProductId": product_id,
                        "activeSaleMissingAssetId": asset_id or None,
                    }
                ),
            )
            skipped_ids.append(trade_id)
            errors.append(f"trade {trade_id}: {reason}")
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
    has_wallet_buy_evidence = any(
        value in verified_by
        for value in (
            "wallet_balance_delta",
            "wallet_balance_delta_within_buy_order_max",
        )
    )
    if not has_wallet_buy_evidence:
        if (
            wallet_delta is None
            or steam_buy_price is None
            or wallet_delta <= 0
            or wallet_delta > steam_buy_price + 0.02
        ):
            return None
    return "wallet decrease within the buy-order maximum plus local inventory proves Steam buy completed"


def _restore_profit_trade_after_verified_steam_purchase(
    db: Database,
    row: Any,
    config: StrategyConfig,
    *,
    source: str,
    reason: str,
    b_asset_id: str | None,
    purchase_receipt: dict[str, Any] | None = None,
    inventory_after_asset_ids: list[str] | None = None,
) -> str | None:
    trade_id = int(row["id"])
    asset_id = str(row["a_asset_id"] or "").strip()
    market_hash_name = str(row["market_hash_name"] or "").strip()
    note = _read_note(row["note"])
    steam_id = str(note.get("steamId") or row["a_steam_id"] or "").strip()
    if not asset_id or not market_hash_name or not steam_id:
        return "missing A asset, market_hash_name, or Steam id"
    protected_reason = _profit_trade_protection_reason(
        config,
        asset_id=asset_id,
        market_hash_name=market_hash_name,
        steam_id=steam_id,
    )
    if protected_reason is None:
        protected_reason = _profit_trade_type_block_reason(config, market_hash_name)
    if protected_reason is not None:
        return f"protected asset: {protected_reason}"

    active_reservation = db.get_active_asset_reservation(asset_id)
    if active_reservation is not None:
        reservation_status = str(active_reservation["status"] or "").strip()
        reservation_owner = str(active_reservation["owner"] or "").strip()
        reservation_operation = active_reservation["operation_id"]
        if reservation_status != "active" or reservation_owner != PROFIT_TRADE_OWNER:
            return "A asset has incompatible reservation"
        if reservation_operation is not None and int(reservation_operation) != trade_id:
            return "A asset is reserved by another profit trade"
    else:
        reservation_id = db.reserve_asset(
            asset_id=asset_id,
            market_hash_name=market_hash_name,
            owner=PROFIT_TRADE_OWNER,
            purpose="sell_existing_a",
            reserved_until=None,
            operation_id=trade_id,
            note=_build_note(
                {
                    "source": "profit_trade_recover_verified_buy",
                    "tradeId": trade_id,
                    "reason": reason,
                    "recoveredAt": utc_now_iso(),
                }
            ),
        )
        if reservation_id is None:
            return "failed to reserve A asset"

    paid_total = safe_float((purchase_receipt or {}).get("paidTotal"))
    steam_buy_price = paid_total or safe_float(row["steam_buy_price"])
    steam_cost_ratio = safe_float(row["steam_balance_discount"])
    if steam_cost_ratio is None:
        steam_cost_ratio = _profit_trade_steam_cost_ratio(config)
    steam_real_cost = (
        float(steam_buy_price) * float(steam_cost_ratio)
        if steam_buy_price is not None and steam_buy_price > 0
        else safe_float(row["steam_real_cost"])
    )
    c5_expected_net = safe_float(row["c5_expected_net_price"])
    expected_profit = (
        c5_expected_net - steam_real_cost
        if c5_expected_net is not None and steam_real_cost is not None
        else safe_float(row["expected_profit"])
    )
    expected_roi = (
        _profit_trade_transfer_roi(
            c5_expected_net=c5_expected_net,
            steam_buy_price=float(steam_buy_price),
            steam_cost_ratio=float(steam_cost_ratio),
        )
        if c5_expected_net is not None and steam_buy_price is not None and steam_buy_price > 0
        else safe_float(row["expected_roi"])
    )
    receipt_time = safe_int((purchase_receipt or {}).get("timePurchased"))
    succeeded_at = (
        datetime.fromtimestamp(receipt_time, tz=timezone.utc).isoformat()
        if receipt_time is not None
        else utc_now_iso()
    )
    resolved_b_asset_id = str(
        b_asset_id
        or (purchase_receipt or {}).get("newAssetId")
        or ""
    ).strip() or None
    verified_by = [str(value) for value in (note.get("steamBuyVerifiedBy") or [])]
    if source not in verified_by:
        verified_by.append(source)
    inventory_ids = [
        str(value)
        for value in (inventory_after_asset_ids or note.get("inventoryAfterAssetIds") or [])
        if str(value or "").strip()
    ]
    db.update_profit_trade(
        trade_id,
        status="steam_bought",
        step_key="steam_bought",
        step_index=3,
        b_asset_id=resolved_b_asset_id,
        steam_buy_price=steam_buy_price,
        steam_balance_discount=float(steam_cost_ratio),
        steam_real_cost=steam_real_cost,
        expected_profit=expected_profit,
        expected_roi=expected_roi,
        error=None,
        note=_build_note(
            {
                **note,
                "steamBuySucceededAt": succeeded_at,
                "steamBuyRecoveredAt": utc_now_iso(),
                "steamBuyRecoveredBy": source,
                "steamBuyRecoverReason": reason,
                "steamPurchaseReceipt": purchase_receipt,
                "inventoryAfterAssetIds": inventory_ids,
                "newInventoryAssetIds": [resolved_b_asset_id] if resolved_b_asset_id else [],
                "steamBuyVerifiedBy": verified_by,
                "orphanBuyRecoveredAt": utc_now_iso() if str(row["status"] or "") == "cancelled" else None,
            }
        ),
    )
    db.conn.execute("UPDATE profit_trades SET completed_at = NULL WHERE id = ?", (trade_id,))
    db.conn.commit()
    return None


def _close_confirmed_duplicate_purchase_incident(
    db: Database,
    row: Any,
    config: StrategyConfig,
    *,
    reason: str,
    restore_error: str,
    purchase_receipt: dict[str, Any],
) -> dict[str, Any] | None:
    """Close a proven Steam purchase that can no longer reuse its A asset.

    This is deliberately narrower than a normal dismissal: an official Steam
    purchase receipt is required, and the A reservation must already belong to
    another trade.  The purchase remains recorded as real spend, while the
    interrupted row is never resumed into the C5 listing path.
    """

    normalized_restore_error = str(restore_error or "").strip().lower()
    if (
        "reservation" not in normalized_restore_error
        and normalized_restore_error != "failed to reserve a asset"
    ):
        return None

    trade_id = int(row["id"])
    asset_id = str(row["a_asset_id"] or "").strip()
    reservation = db.get_active_asset_reservation(asset_id) if asset_id else None
    if reservation is None:
        return None
    reservation_operation_id = reservation["operation_id"]
    if reservation_operation_id is None or int(reservation_operation_id) == trade_id:
        return None

    note = _read_note(row["note"])
    paid_total = safe_float(purchase_receipt.get("paidTotal"))
    if paid_total is None or paid_total <= 0:
        return None
    maximum_price = (
        safe_float(note.get("steamBuyMaximumPrice"))
        or safe_float(row["steam_buy_price"])
        or paid_total
    )
    steam_cost_ratio = safe_float(row["steam_balance_discount"])
    if steam_cost_ratio is None:
        steam_cost_ratio = _profit_trade_steam_cost_ratio(config)
    steam_real_cost = float(paid_total) * float(steam_cost_ratio)
    receipt_time = safe_int(purchase_receipt.get("timePurchased"))
    succeeded_at = (
        datetime.fromtimestamp(receipt_time, tz=timezone.utc).isoformat()
        if receipt_time is not None
        else utc_now_iso()
    )
    b_asset_id = str(purchase_receipt.get("newAssetId") or "").strip() or None
    closed_at = utc_now_iso()
    incident_reason = (
        "Steam purchase completed, but A is already reserved or consumed by "
        f"another Profit Trade ({reservation_operation_id}); closed as a duplicate purchase incident"
    )
    incident_context = {
        "purchaseActuallyCompleted": True,
        "duplicatePurchaseIncident": True,
        "duplicatePurchaseReason": incident_reason,
        "conflictingReservationId": int(reservation["id"]),
        "conflictingTradeId": int(reservation_operation_id),
        "restoreError": restore_error,
        "steamBuyMaximumPrice": float(maximum_price),
        "steamBuyActualPrice": float(paid_total),
        "steamPurchaseReceipt": purchase_receipt,
    }
    db.update_profit_trade(
        trade_id,
        status="cancelled",
        step_key="steam_bought",
        step_index=3,
        b_asset_id=b_asset_id,
        steam_buy_price=float(paid_total),
        steam_balance_discount=float(steam_cost_ratio),
        steam_real_cost=steam_real_cost,
        realized_profit=None,
        realized_roi=None,
        error=incident_reason,
        note=_build_note(
            {
                **note,
                **incident_context,
                "steamBuySucceededAt": succeeded_at,
                "steamBuyVerifiedBy": list(
                    dict.fromkeys(
                        [
                            *[str(value) for value in (note.get("steamBuyVerifiedBy") or [])],
                            "market_history_event_type_4",
                        ]
                    )
                ),
                "dismissedAt": closed_at,
                "dismissedReason": reason,
                "cancelSource": "profit_trade_duplicate_purchase_incident_acknowledged",
                "dismissBuyOrderResolution": "purchased",
                "steamBuyOrderTerminalState": "purchased",
            }
        ),
        _event_reason=incident_reason,
        _event_context=incident_context,
    )
    db.set_profit_trade_acknowledgement(
        trade_id,
        acknowledged=True,
        reason=incident_reason,
    )
    return _trade_row_to_dict(db.get_profit_trade(trade_id))


def recover_unverified_profit_trade_steam_buys(
    settings: Settings,
    *,
    config: StrategyConfig | None = None,
    limit: int = 1000,
    steam_client: Any | None = None,
    remote_audit: bool = False,
) -> dict[str, Any]:
    config = config or load_strategy_config(settings)
    recovered_ids: list[int] = []
    skipped_ids: list[int] = []
    errors: list[str] = []
    db = Database(settings.db_path)
    try:
        db.initialize()
        rows = list(db.list_profit_trades(status="manual_required", limit=limit))
        if remote_audit:
            rows.extend(db.list_profit_trades(status="cancelled", limit=limit))
        seen_ids: set[int] = set()
        for row in rows:
            trade_id = int(row["id"])
            if trade_id in seen_ids:
                continue
            seen_ids.add(trade_id)
            note = _read_note(row["note"])
            status = str(row["status"] or "").strip()
            tracked_buy_order_id = str(note.get("steamBuyOrderId") or "").strip()
            tracked_create_buy_order = (
                str(note.get("steamBuyMethod") or "").strip() == "createbuyorder"
                and bool(tracked_buy_order_id)
                and not str(row["b_asset_id"] or "").strip()
                and not str(row["c5_product_id"] or "").strip()
                and (
                    status == "manual_required"
                    or not (
                        note.get("steamBuyOrderCancellationConfirmedAt")
                        or note.get("orphanBuyOrderCancellationConfirmedAt")
                    )
                )
            )
            reason = _manual_trade_recoverable_steam_buy_reason(row)
            local_error: str | None = None
            if reason is not None:
                asset_id = str(row["a_asset_id"] or "").strip()
                market_hash_name = str(row["market_hash_name"] or "").strip()
                steam_id = str(note.get("steamId") or row["a_steam_id"] or "").strip()
                inventory_asset_rows = db.list_assets(
                    market_hash_name=market_hash_name,
                    steam_id=steam_id,
                )
                inventory_after_asset_ids = [str(asset["asset_id"]) for asset in inventory_asset_rows]
                before_asset_ids = [
                    str(value)
                    for value in (note.get("beforeAssetIds") or [])
                    if str(value or "").strip()
                ]
                if before_asset_ids:
                    before_set = set(before_asset_ids)
                    candidates = [value for value in inventory_after_asset_ids if value not in before_set]
                else:
                    candidates = [value for value in inventory_after_asset_ids if value != asset_id]
                candidates = sorted(set(candidates))
                if len(candidates) == 1:
                    local_error = _restore_profit_trade_after_verified_steam_purchase(
                        db,
                        row,
                        config,
                        source="local_inventory_reconciliation",
                        reason=reason,
                        b_asset_id=candidates[0],
                        inventory_after_asset_ids=inventory_after_asset_ids,
                    )
                    if local_error is None:
                        recovered_ids.append(trade_id)
                        continue
                else:
                    local_error = f"expected exactly one B candidate, got {len(candidates)}"

            if not remote_audit or not tracked_create_buy_order:
                if local_error:
                    skipped_ids.append(trade_id)
                    errors.append(f"recover-buy {trade_id}: {local_error}")
                continue

            market_hash_name = str(row["market_hash_name"] or "").strip()
            steam_buy_price = safe_float(row["steam_buy_price"])
            if not market_hash_name or steam_buy_price is None or steam_buy_price <= 0:
                skipped_ids.append(trade_id)
                errors.append(f"recover-buy {trade_id}: missing market hash name or Steam buy price")
                continue
            try:
                client = _build_steam_client_for_profit_trade(
                    settings,
                    row,
                    steam_client=steam_client,
                )
                purchase_requested_at = str(
                    note.get("steamBuyRequestedAt")
                    or note.get("steamBuyUnverifiedAt")
                    or row["created_at"]
                    or ""
                ).strip() or None
                purchase_receipt, purchase_history_error = _find_official_steam_purchase_receipt(
                    client,
                    market_hash_name=market_hash_name,
                    expected_total=int(round(steam_buy_price * 100.0)),
                    purchase_requested_at=purchase_requested_at,
                )
                if purchase_receipt is not None:
                    restore_error = _restore_profit_trade_after_verified_steam_purchase(
                        db,
                        row,
                        config,
                        source="market_history_event_type_4",
                        reason="official Steam purchase history proves the tracked buy order filled",
                        b_asset_id=str(purchase_receipt.get("newAssetId") or "").strip() or None,
                        purchase_receipt=purchase_receipt,
                    )
                    if restore_error is not None:
                        raise RuntimeError(restore_error)
                    recovered_ids.append(trade_id)
                    continue
                if purchase_history_error:
                    raise RuntimeError(purchase_history_error)

                listings_payload = call_safety_terminal(
                    client.my_listings,
                    start=0,
                    count=100,
                )
                active_orders = _matching_active_steam_buy_orders(
                    listings_payload if isinstance(listings_payload, dict) else {},
                    market_hash_name=market_hash_name,
                    buy_order_id=tracked_buy_order_id,
                )
                if status == "cancelled" and active_orders:
                    resolution = _cancel_and_resolve_steam_buy_order(
                        client,
                        market_hash_name=market_hash_name,
                        expected_total=int(round(steam_buy_price * 100.0)),
                        wallet_before_balance=safe_float(note.get("walletBalanceBefore")),
                        buy_order_id=tracked_buy_order_id,
                        purchase_requested_at=purchase_requested_at,
                        actual_total_hint=safe_float(note.get("walletDelta")),
                    )
                    if resolution.outcome == "purchased":
                        receipt = resolution.verification.purchase_receipt
                        restore_error = _restore_profit_trade_after_verified_steam_purchase(
                            db,
                            row,
                            config,
                            source=(
                                "market_history_event_type_4"
                                if receipt is not None
                                else "wallet_and_order_state_reconciliation"
                            ),
                            reason="legacy hidden buy order filled during reconciliation",
                            b_asset_id=(resolution.verification.new_inventory_asset_ids or [None])[0],
                            purchase_receipt=receipt,
                        )
                        if restore_error is not None:
                            raise RuntimeError(restore_error)
                        recovered_ids.append(trade_id)
                        continue
                    if resolution.outcome == "cancelled":
                        db.update_profit_trade(
                            trade_id,
                            note=_build_note(
                                {
                                    **note,
                                    "orphanBuyAuditAt": utc_now_iso(),
                                    "orphanBuyOrderCancellationConfirmedAt": utc_now_iso(),
                                    "orphanBuyOrderCancelPayload": resolution.cancel_payload,
                                }
                            ),
                        )
                        continue
                    asset_id = str(row["a_asset_id"] or "").strip()
                    if asset_id and db.get_active_asset_reservation(asset_id) is None:
                        db.reserve_asset(
                            asset_id=asset_id,
                            market_hash_name=market_hash_name,
                            owner=PROFIT_TRADE_OWNER,
                            purpose="sell_existing_a",
                            reserved_until=None,
                            operation_id=trade_id,
                            note=_build_note({"source": "orphan_buy_order_tracking", "tradeId": trade_id}),
                        )
                    db.update_profit_trade(
                        trade_id,
                        status="manual_required",
                        error="Tracked Steam buy order is still active or its terminal state is uncertain; cannot safely hide",
                        note=_build_note(
                            {
                                **note,
                                "orphanBuyAuditAt": utc_now_iso(),
                                "activeBuyOrdersAfterCancel": resolution.verification.active_buy_orders,
                                "steamBuyOrderCancelError": resolution.cancel_error,
                            }
                        ),
                    )
                    skipped_ids.append(trade_id)
                    errors.append(f"recover-buy {trade_id}: tracked buy order terminal state is uncertain")
                    continue
                if status == "cancelled":
                    db.update_profit_trade(
                        trade_id,
                        note=_build_note(
                            {
                                **note,
                                "orphanBuyAuditAt": utc_now_iso(),
                                "orphanBuyOrderActive": False,
                                "orphanBuyOrderCancellationConfirmedAt": utc_now_iso(),
                            }
                        ),
                    )
            except Exception as exc:
                skipped_ids.append(trade_id)
                errors.append(f"recover-buy {trade_id}: {exc}")
                continue

            if local_error:
                skipped_ids.append(trade_id)
                errors.append(f"recover-buy {trade_id}: {local_error}")
    finally:
        db.close()
    return {"ok": True, "recoveredTradeIds": recovered_ids, "skippedTradeIds": skipped_ids, "errors": errors}
def dismiss_profit_trade(
    settings: Settings,
    trade_id: int,
    *,
    reason: str = "user dismissed manual review trade",
    steam_client: Any | None = None,
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
        note = _read_note(row["note"])
        tracked_buy_order_id = str(note.get("steamBuyOrderId") or "").strip()
        if note.get("steamBuyUnverifiedAt") and not tracked_buy_order_id:
            raise RuntimeError(
                "unverified Steam buy has no buy-order id; cannot safely hide without confirming its terminal state"
            )
        dismiss_buy_order_resolution: SteamBuyOrderResolution | None = None
        if tracked_buy_order_id:
            if str(note.get("steamBuyMethod") or "").strip() != "createbuyorder":
                raise RuntimeError("tracked Steam order is not a createbuyorder; cannot safely hide")
            steam_buy_price = safe_float(row["steam_buy_price"]) or safe_float(note.get("steamBuyPrice"))
            if steam_buy_price is None or steam_buy_price <= 0:
                raise RuntimeError("tracked Steam buy order is missing its expected price; cannot safely hide")
            client = _build_steam_client_for_profit_trade(
                settings,
                row,
                steam_client=steam_client,
            )
            purchase_requested_at = str(
                note.get("steamBuyRequestedAt")
                or note.get("steamBuyUnverifiedAt")
                or row["created_at"]
                or ""
            ).strip() or None
            dismiss_buy_order_resolution = _cancel_and_resolve_steam_buy_order(
                client,
                market_hash_name=str(row["market_hash_name"] or "").strip(),
                expected_total=int(round(steam_buy_price * 100.0)),
                wallet_before_balance=safe_float(note.get("walletBalanceBefore")),
                buy_order_id=tracked_buy_order_id,
                purchase_requested_at=purchase_requested_at,
                actual_total_hint=safe_float(note.get("walletDelta")),
            )
            if dismiss_buy_order_resolution.outcome == "purchased":
                verification = dismiss_buy_order_resolution.verification
                receipt = verification.purchase_receipt
                restore_error = _restore_profit_trade_after_verified_steam_purchase(
                    db,
                    row,
                    load_strategy_config(settings),
                    source=(
                        "market_history_event_type_4"
                        if receipt is not None
                        else "wallet_and_order_state_reconciliation"
                    ),
                    reason="purchase completed while user requested safe buy-order cancellation",
                    b_asset_id=(verification.new_inventory_asset_ids or [None])[0],
                    purchase_receipt=receipt,
                )
                if restore_error is not None:
                    if receipt is None:
                        receipt, receipt_error = _find_official_steam_purchase_receipt(
                            client,
                            market_hash_name=str(row["market_hash_name"] or "").strip(),
                            expected_total=int(round(steam_buy_price * 100.0)),
                            purchase_requested_at=purchase_requested_at,
                            actual_total_hint=safe_float(note.get("walletDelta")),
                        )
                        if receipt_error is not None:
                            raise RuntimeError(
                                f"{restore_error}; official Steam purchase verification failed: "
                                f"{receipt_error}"
                            )
                    if receipt is not None:
                        incident_trade = _close_confirmed_duplicate_purchase_incident(
                            db,
                            row,
                            load_strategy_config(settings),
                            reason=reason,
                            restore_error=restore_error,
                            purchase_receipt=receipt,
                        )
                        if incident_trade is not None:
                            return {
                                "ok": True,
                                "changed": True,
                                "dismissed": True,
                                "message": (
                                    "Steam purchase was confirmed and recorded as a duplicate "
                                    "purchase incident; the interrupted card was closed"
                                ),
                                "trade": incident_trade,
                            }
                    raise RuntimeError(restore_error)
                updated = db.get_profit_trade(trade_id)
                return {
                    "ok": False,
                    "changed": True,
                    "dismissed": False,
                    "message": "Steam buy completed; the trade was restored and was not hidden",
                    "trade": _trade_row_to_dict(updated),
                }
            if dismiss_buy_order_resolution.outcome != "cancelled":
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    error="Tracked Steam buy order is still active or its terminal state is uncertain; cannot safely hide",
                    note=_build_note(
                        {
                            **note,
                            "dismissAttemptedAt": utc_now_iso(),
                            "dismissBuyOrderResolution": dismiss_buy_order_resolution.outcome,
                            "activeBuyOrdersAfterCancel": dismiss_buy_order_resolution.verification.active_buy_orders,
                            "steamBuyOrderCancelError": dismiss_buy_order_resolution.cancel_error,
                            "steamBuyOrderCancelPayload": dismiss_buy_order_resolution.cancel_payload,
                        }
                    ),
                )
                raise RuntimeError(
                    "Tracked Steam buy order is still active or uncertain; cannot safely hide"
                )
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
                    "dismissBuyOrderResolution": (
                        dismiss_buy_order_resolution.outcome
                        if dismiss_buy_order_resolution is not None
                        else None
                    ),
                    "steamBuyOrderCancellationConfirmedAt": (
                        utc_now_iso()
                        if dismiss_buy_order_resolution is not None
                        else None
                    ),
                    "steamBuyOrderCancelPayload": (
                        dismiss_buy_order_resolution.cancel_payload
                        if dismiss_buy_order_resolution is not None
                        else None
                    ),
                }
            ),
        )
        updated = db.get_profit_trade(trade_id)
        return {
            "ok": True,
            "changed": True,
            "dismissed": True,
            "trade": _trade_row_to_dict(updated),
        }
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
    _require_profit_trade_real_execution(config)
    db = Database(settings.db_path)
    repriced_ids: list[int] = []
    skipped_ids: list[int] = []
    errors: list[str] = []
    try:
        db.initialize()
        rows = db.list_profit_trades(status="c5_listed", limit=500)
        now = datetime.now(timezone.utc)
        stale_reprice_after = max(0.0, float(config.profit_trade_stale_reprice_after_hours))
        stale_manual_after = max(0.0, float(config.profit_trade_stale_manual_review_after_hours))

        # The 24-hour terminal for automatic repricing is a local state-machine
        # decision.  It must not depend on C5 depth/statistics being readable.
        # Persist manual_required first; notification failure must never leave
        # the trade eligible for another sale_modify call.
        active_rows: list[Any] = []
        for row in rows:
            trade_id = int(row["id"])
            note = _read_note(row["note"])
            listed_age_hours = _profit_trade_stale_listing_age_hours(note, now=now)
            if not (
                listed_age_hours is not None
                and stale_manual_after > 0
                and listed_age_hours >= stale_manual_after
            ):
                active_rows.append(row)
                continue

            current_price = safe_float(row["c5_listing_price"])
            transitioned_at = utc_now_iso()
            manual_note = {
                **note,
                "lastListingCheckAt": transitioned_at,
                "listingRepriceDecision": "stale_manual_review",
                "listingRepriceBlockedReason": "listed too long without sale",
                "staleListedAgeHours": round(listed_age_hours, 2),
                "staleManualReviewAfterHours": stale_manual_after,
                "staleManualReviewAt": transitioned_at,
                "staleManualReviewNotificationAttemptedAt": transitioned_at,
            }
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                step_key="c5_listed",
                error="C5 listed for more than stale manual review hours",
                note=_build_note(manual_note),
            )

            notification_sent = False
            notification_error: str | None = None
            try:
                notification_sent = _send_profit_trade_listing_alert(
                    settings,
                    title="搬砖做T上架超过一天未售出",
                    row=row,
                    body_lines=[
                        f"- 已上架: {listed_age_hours:.1f} 小时",
                        (
                            f"- 当前挂价: CNY {current_price:.2f}"
                            if current_price is not None
                            else "- 当前挂价: -"
                        ),
                        "- 程序已停止自动改价并转人工；C5成交确认仍会继续。",
                    ],
                )
            except Exception as exc:
                notification_error = str(exc)
                errors.append(f"stale-manual-notify {trade_id}: {exc}")

            latest = db.get_profit_trade(trade_id)
            latest_note = _read_note(latest["note"]) if latest is not None else manual_note
            db.update_profit_trade(
                trade_id,
                note=_build_note(
                    {
                        **latest_note,
                        "staleManualReviewServerChanSent": notification_sent,
                        "staleManualReviewServerChanError": notification_error,
                    }
                ),
            )
            skipped_ids.append(trade_id)

        if not active_rows:
            return {
                "ok": True,
                "repricedTradeIds": repriced_ids,
                "skippedTradeIds": skipped_ids,
                "errors": errors,
            }

        if not config.profit_trade_reprice_enabled:
            skipped_ids.extend(int(row["id"]) for row in active_rows)
            errors.append("profitTrade reprice is disabled")
            return {
                "ok": True,
                "repricedTradeIds": repriced_ids,
                "skippedTradeIds": skipped_ids,
                "errors": errors,
            }

        if c5_client is None:
            if not settings.c5_api_key:
                raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
            c5_client = _build_profit_trade_c5_client(
                settings,
                run_id=f"PTREPRICE-{uuid.uuid4().hex}",
            )

        market_hash_names = sorted(
            {
                str(row["market_hash_name"] or "").strip()
                for row in active_rows
                if str(row["market_hash_name"] or "").strip()
            }
        )
        known_own_price_cents_by_name: dict[str, set[int]] = {}
        own_price_rows = [
            *rows,
            *[
                row
                for row in db.list_profit_trades(status="manual_required", limit=500)
                if str(row["step_key"] or "") == "c5_listed"
            ],
        ]
        for own_row in own_price_rows:
            own_name = str(own_row["market_hash_name"] or "").strip()
            own_product_id = str(own_row["c5_product_id"] or "").strip()
            own_price = safe_float(own_row["c5_listing_price"])
            if not own_name or not own_product_id or own_price is None or own_price <= 0:
                continue
            known_own_price_cents_by_name.setdefault(own_name, set()).add(
                int(round(own_price * 100))
            )

        try:
            depth_by_name = _fetch_c5_price_batch_references(
                c5_client,
                settings,
                market_hash_names=market_hash_names,
            )
            depth_batch_error: Exception | None = None
        except Exception as exc:
            depth_by_name = {}
            depth_batch_error = exc
            errors.append(f"price-batch: {exc}")
        try:
            statistics_by_name = (
                _fetch_c5_recent_sale_risks(
                    c5_client,
                    app_id=settings.app_id,
                    market_hash_names=market_hash_names,
                )
                if market_hash_names
                else {}
            )
        except Exception as exc:
            statistics_by_name = {}
            errors.append(f"listing-statistics: {exc}")

        for row in active_rows:
            trade_id = int(row["id"])
            note = _read_note(row["note"])
            market_hash_name = str(row["market_hash_name"] or "").strip()
            product_id = str(row["c5_product_id"] or note.get("c5ProductId") or "").strip()
            current_price = safe_float(row["c5_listing_price"])
            steam_buy_price = safe_float(row["steam_buy_price"])
            steam_cost_ratio = safe_float(row["steam_balance_discount"]) or _profit_trade_steam_cost_ratio(config)
            if (
                not market_hash_name
                or not product_id
                or current_price is None
                or current_price <= 0
                or steam_buy_price is None
                or steam_buy_price <= 0
            ):
                reason = "listed trade is missing market, C5 product, current price, or Steam buy price"
                db.update_profit_trade(
                    trade_id,
                    error=reason,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingRepriceDecision": "invalid_trade_evidence",
                            "listingRepriceBlockedReason": reason,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            protected_reason = _profit_trade_protection_reason(
                config,
                asset_id=str(row["a_asset_id"] or note.get("assetId") or "").strip(),
                market_hash_name=market_hash_name,
                steam_id=str(row["a_steam_id"] or note.get("steamId") or "").strip(),
            )
            if protected_reason is None:
                protected_reason = _profit_trade_type_block_reason(config, market_hash_name)
            if protected_reason is not None:
                db.update_profit_trade(
                    trade_id,
                    error=None,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingRepriceDecision": "protected",
                            "listingRepriceBlockedReason": protected_reason,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            listed_age_hours = _profit_trade_stale_listing_age_hours(note, now=now)
            clearance_mode = (
                listed_age_hours is not None
                and stale_reprice_after > 0
                and listed_age_hours >= stale_reprice_after
            )
            statistics_for_name = statistics_by_name.get(market_hash_name)
            depth = depth_by_name.get(market_hash_name)
            if depth is None:
                exc = depth_batch_error or RuntimeError("C5 price_batch returned no matching item")
                reason = f"C5 price_batch reference unavailable; current price kept: {exc}"
                errors.append(f"price-batch {trade_id}: {exc}")
                db.update_profit_trade(
                    trade_id,
                    error=reason,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "listingRepriceDecision": "listing_evidence_unavailable",
                            "listingRepriceBlockedReason": reason,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            depth_ok, depth_reason = _evaluate_c5_orderbook_depth_risk(config, depth=depth)
            if not depth_ok:
                db.update_profit_trade(
                    trade_id,
                    error=depth_reason,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "c5Pricing": depth,
                            "listingDepth": depth,
                            "listingRepriceDecision": "blocked_c5_listing_depth",
                            "listingRepriceBlockedReason": depth_reason,
                            "listingRiskAt": utc_now_iso(),
                            "listingRiskReason": depth_reason,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            reference_price = safe_float(depth.get("effectiveReferencePrice"))
            if reference_price is None or reference_price <= 0:
                reason = "C5 competitor reference is unavailable"
                db.update_profit_trade(
                    trade_id,
                    error=reason,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "c5Pricing": depth,
                            "listingDepth": depth,
                            "listingRepriceDecision": "competitor_reference_unavailable",
                            "listingRepriceBlockedReason": reason,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            current_cents = int(round(current_price * 100))
            reference_cents = int(round(reference_price * 100))
            known_own_price_cents = known_own_price_cents_by_name.get(market_hash_name, set())
            # price_batch does not expose productId, so exact row subtraction is
            # impossible.  A reference equal to any known own active price may
            # be our own listing; conservatively keep every same-item listing.
            if reference_cents in known_own_price_cents:
                db.update_profit_trade(
                    trade_id,
                    error=None,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "c5Pricing": depth,
                            "listingDepth": depth,
                            "listingRepriceDecision": "kept_possible_own_lowest",
                            "listingRepriceBlockedReason": (
                                "price_batch lowest matches a known own active listing; "
                                "automatic self-undercutting is forbidden"
                            ),
                            "knownOwnListingPrices": [
                                cents / 100.0 for cents in sorted(known_own_price_cents)
                            ],
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            if current_cents <= reference_cents:
                db.update_profit_trade(
                    trade_id,
                    error=None,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "c5Pricing": depth,
                            "listingDepth": depth,
                            "listingRepriceDecision": "kept_price_advantage",
                            "listingRepriceBlockedReason": None,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            cooldown_ok, cooldown_reason = _profit_trade_reprice_cooldown_passed(
                config,
                note,
                now=now,
            )
            if not clearance_mode and not cooldown_ok:
                db.update_profit_trade(
                    trade_id,
                    error=None,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "c5Pricing": depth,
                            "listingDepth": depth,
                            "listingRepriceDecision": "cooldown",
                            "listingRepriceBlockedReason": cooldown_reason,
                            "listingRepriceCooldownReason": cooldown_reason,
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            target_price = _profit_trade_competitive_listing_price(
                config,
                current_lowest_price=reference_price,
                fallback_price=current_price,
            )
            target_cents = int(round(target_price * 100))
            if target_cents >= current_cents:
                db.update_profit_trade(
                    trade_id,
                    error=None,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": utc_now_iso(),
                            "c5Pricing": depth,
                            "listingDepth": depth,
                            "listingRepriceDecision": "target_not_lower",
                            "listingRepriceBlockedReason": "competitive target would not reduce the current price",
                        }
                    ),
                )
                skipped_ids.append(trade_id)
                continue

            market_stats = _merge_c5_listing_depth_and_statistics(
                depth=depth,
                statistics=statistics_for_name,
            )
            purchase_floor = safe_float(market_stats.purchase_max_price)
            if purchase_floor is not None and purchase_floor > target_price:
                adjusted_target = _floor_cny_cent(purchase_floor)
                adjusted_cents = int(round(adjusted_target * 100))
                if adjusted_cents >= reference_cents or adjusted_cents >= current_cents:
                    reason = "purchase-price floor would reduce profit without winning the competitor price"
                    db.update_profit_trade(
                        trade_id,
                        error=None,
                        note=_build_note(
                            {
                                **note,
                                "lastListingCheckAt": utc_now_iso(),
                                "c5Pricing": depth,
                                "listingDepth": depth,
                                "listingMarketStats": _c5_risk_note(market_stats),
                                "listingRepriceDecision": "purchase_floor_cannot_win",
                                "listingRepriceBlockedReason": reason,
                                "purchaseFloorPrice": round(purchase_floor, 2),
                                "repriceTargetPrice": round(target_price, 2),
                            }
                        ),
                    )
                    skipped_ids.append(trade_id)
                    continue
                target_price = adjusted_target

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
                                "c5Pricing": depth,
                                "listingDepth": depth,
                                "listingRepriceDecision": "blocked_c5_market_depth",
                                "listingRepriceBlockedReason": evaluated_market_stats.reason,
                                "listingRiskAt": utc_now_iso(),
                                "listingRiskReason": evaluated_market_stats.reason,
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
            min_roi_at_open, min_roi_source = _profit_trade_min_roi_at_open(config, note)
            stale_roi_factor = _profit_trade_stale_roi_factor_at_open(config, note)
            roi_floor = min_roi_at_open * stale_roi_factor if clearance_mode else min_roi_at_open

            manual_review_roi = float(config.profit_trade_manual_review_roi)
            if manual_review_roi > 0 and expected_roi > manual_review_roi:
                reason = f"reprice ROI {expected_roi * 100:.2f}% > manual review threshold"
                blocked_at = utc_now_iso()
                db.update_profit_trade(
                    trade_id,
                    status="manual_required",
                    step_key="c5_listed",
                    error=reason,
                    note=_build_note(
                        {
                            **note,
                            "lastListingCheckAt": blocked_at,
                            "c5Pricing": depth,
                            "listingDepth": depth,
                            "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                            "listingRepriceDecision": "manual_review_high_roi",
                            "listingRepriceBlockedReason": reason,
                            "repriceBlockedAt": blocked_at,
                            "repriceTargetPrice": round(target_price, 2),
                            "repriceExpectedRoi": round(expected_roi, 4),
                        }
                    ),
                )
                try:
                    sent = _send_profit_trade_listing_alert(
                        settings,
                        title="搬砖做T改价异常收益需人工确认",
                        row=row,
                        body_lines=[
                            f"- 当前价: CNY {current_price:.2f}",
                            f"- C5竞争参考价: CNY {reference_price:.2f}",
                            f"- 目标价: CNY {target_price:.2f}",
                            f"- 目标ROI: {expected_roi * 100:.2f}%",
                            "- 程序已停止改价，请检查价格源。",
                        ],
                    )
                    latest = db.get_profit_trade(trade_id)
                    latest_note = _read_note(latest["note"]) if latest is not None else note
                    db.update_profit_trade(
                        trade_id,
                        note=_build_note({**latest_note, "highRoiServerChanSent": sent}),
                    )
                except Exception as exc:
                    errors.append(f"high-roi-notify {trade_id}: {exc}")
                skipped_ids.append(trade_id)
                continue

            if expected_roi < roi_floor:
                decision = "clearance_roi_floor_reached" if clearance_mode else "below_min_roi"
                reason = (
                    f"reprice ROI {expected_roi * 100:.2f}% < "
                    f"{'clearance' if clearance_mode else 'open'} ROI floor {roi_floor * 100:.2f}%"
                )
                db.update_profit_trade(
                    trade_id,
                    error=reason,
                    note=_build_note(
                        {
                            **note,
                            "minRoiAtOpen": min_roi_at_open,
                            "minRoiAtOpenSource": min_roi_source,
                            "staleMinRoiFactorAtOpen": stale_roi_factor,
                            "lastListingCheckAt": utc_now_iso(),
                            "c5Pricing": depth,
                            "listingDepth": depth,
                            "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                            "listingRepriceDecision": decision,
                            "listingRepriceBlockedReason": reason,
                            "repriceBlockedAt": utc_now_iso(),
                            "repriceBlockedReason": reason,
                            "repriceTargetPrice": round(target_price, 2),
                            "repriceExpectedRoi": round(expected_roi, 4),
                            "staleListedAgeHours": (
                                round(listed_age_hours, 2)
                                if clearance_mode and listed_age_hours is not None
                                else None
                            ),
                            "repriceRoiFloor": roi_floor,
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
                            "c5Pricing": depth,
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
                            "c5Pricing": depth,
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

            repriced_at = utc_now_iso()
            initial_listing_price = safe_float(note.get("initialC5ListingPrice")) or current_price
            cumulative_reduction = max(0.0, initial_listing_price - target_price)
            cumulative_reduction_pct = (
                cumulative_reduction / initial_listing_price
                if initial_listing_price > 0
                else 0.0
            )
            reprice_count = max(0, safe_int(note.get("repriceCount")) or 0) + 1
            updated_note = {
                **note,
                "minRoiAtOpen": min_roi_at_open,
                "minRoiAtOpenSource": min_roi_source,
                "staleMinRoiFactorAtOpen": stale_roi_factor,
                "initialC5ListingPrice": round(initial_listing_price, 2),
                "c5SalePrice": round(target_price, 2),
                "c5ListingPrice": round(target_price, 2),
                "c5ExpectedNetPrice": round(target_net, 2),
                "expectedProfit": round(expected_profit, 2),
                "expectedRoi": round(expected_roi, 4),
                "c5Pricing": depth,
                "listingDepth": depth,
                "listingMarketStats": _c5_risk_note(evaluated_market_stats),
                "listingRepriceDecision": "repriced",
                "listingRepriceBlockedReason": None,
                "listingRepriceMode": "clearance" if clearance_mode else "normal",
                "lastRepriceAt": repriced_at,
                "repriceAt": repriced_at,
                "repriceFrom": round(current_price, 2),
                "repriceTo": round(target_price, 2),
                "repriceReferencePrice": round(reference_price, 2),
                "repriceReferenceSource": depth.get("referenceSource"),
                "repriceExpectedNet": round(target_net, 2),
                "repriceExpectedRoi": round(expected_roi, 4),
                "repriceRoiFloor": roi_floor,
                "repriceCount": reprice_count,
                "cumulativeRepriceReductionCny": round(cumulative_reduction, 2),
                "cumulativeRepriceReductionPct": round(cumulative_reduction_pct, 6),
                "staleListedAgeHours": (
                    round(listed_age_hours, 2)
                    if clearance_mode and listed_age_hours is not None
                    else None
                ),
                "repriceRaw": payload,
            }
            db.update_profit_trade(
                trade_id,
                c5_listing_price=target_price,
                c5_expected_net_price=target_net,
                expected_profit=expected_profit,
                expected_roi=expected_roi,
                error=None,
                note=_build_note(updated_note),
            )
            try:
                db.add_profit_trade_audit_event(
                    trade_id,
                    event_type="c5_repriced",
                    reason="automatic C5 competition reprice completed",
                    context={
                        "mode": "clearance" if clearance_mode else "normal",
                        "referencePrice": round(reference_price, 2),
                        "referenceSource": depth.get("referenceSource"),
                        "fromPrice": round(current_price, 2),
                        "toPrice": round(target_price, 2),
                        "expectedRoi": round(expected_roi, 6),
                        "roiFloor": round(roi_floor, 6),
                        "repriceOrdinal": reprice_count,
                    },
                )
            except Exception as exc:
                errors.append(f"reprice-audit {trade_id}: {exc}")
            repriced_ids.append(trade_id)
    finally:
        db.close()
    return {
        "ok": True,
        "repricedTradeIds": repriced_ids,
        "skippedTradeIds": skipped_ids,
        "errors": errors,
    }


def execute_manual_profit_trade_request(
    settings: Settings,
    *,
    request_id: str,
    market_hash_name: str,
    quantity: int,
    approved_expected_roi: float,
    approved_scan_id: str | None = None,
    approved_observed_at: str | None = None,
    requested_at: str | None = None,
    config: StrategyConfig | None = None,
    inventory_payload: dict[str, Any] | None = None,
    market_service: MarketService | None = None,
    steam_client: Any | None = None,
    c5_client: Any | None = None,
    new_action_guard: Callable[[], bool] | None = None,
    refresh_config_each_item: bool = False,
) -> dict[str, Any]:
    """Execute one explicitly confirmed inventory-watch batch.

    Only the automatic minimum ROI gate is replaced by the approved snapshot
    floor.  Every other real-trading guard remains in the normal buy/list
    state machine.  The request id is persisted on every trade so a reclaimed
    scheduled task resumes the same batch instead of creating duplicates.
    """

    config = config or load_strategy_config(settings)
    _require_profit_trade_real_execution(config)
    normalized_request_id = str(request_id or "").strip()
    normalized_name = str(market_hash_name or "").strip()
    requested_quantity = int(quantity)
    approved_roi_floor = _profit_trade_roi_gate_value(approved_expected_roi)
    if not normalized_request_id:
        raise ValueError("manual execution requestId is required")
    if not normalized_name:
        raise ValueError("manual execution marketHashName is required")
    if requested_quantity <= 0 or requested_quantity > PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY:
        raise ValueError(
            "manual execution quantity must be between 1 and "
            f"{PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY}"
        )
    if approved_roi_floor <= 0:
        raise ValueError("manual execution requires a positive approved ROI")
    if not _profit_trade_new_action_allowed(new_action_guard):
        raise RuntimeError("Profit Trade runtime is disabled; manual execution was not started")

    logger = get_profit_trade_event_logger()
    logger.emit(
        provider="local",
        component="profit_trade_manual_execution",
        operation="batch_started",
        message="User-confirmed Profit Trade batch started",
        run_id=normalized_request_id,
        market_hash_name=normalized_name,
        safe_context={
            "requested_quantity": requested_quantity,
            "approved_expected_roi": approved_roi_floor,
            "approved_observed_at": approved_observed_at,
        },
    )

    db = Database(settings.db_path)
    created_trade_ids: list[int] = []
    try:
        db.initialize()
        db.release_expired_asset_reservations()
        existing_rows = db.list_profit_trades_for_manual_request(
            normalized_request_id,
            limit=PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY,
        )
        missing_quantity = max(0, requested_quantity - len(existing_rows))
        if missing_quantity > 0:
            existing_approval_expires_at: datetime | None = None
            if existing_rows:
                existing_note = _read_note(existing_rows[0]["note"])
                existing_approval_expires_at = _parse_iso(
                    existing_note.get("manualExecutionApprovalExpiresAt")
                )
                if (
                    existing_approval_expires_at is not None
                    and existing_approval_expires_at.tzinfo is None
                ):
                    existing_approval_expires_at = (
                        existing_approval_expires_at.replace(tzinfo=timezone.utc)
                    )
                if (
                    existing_approval_expires_at is None
                    or datetime.now(timezone.utc)
                    > existing_approval_expires_at.astimezone(timezone.utc)
                ):
                    raise RuntimeError(
                        "manual execution approval expired before the recovered batch "
                        "could finish reserving its assets"
                    )
            else:
                requested_time = _parse_iso(requested_at)
                if requested_time is None:
                    raise RuntimeError("manual execution request is missing its confirmation time")
                if requested_time.tzinfo is None:
                    requested_time = requested_time.replace(tzinfo=timezone.utc)
                request_age = (
                    datetime.now(timezone.utc) - requested_time.astimezone(timezone.utc)
                ).total_seconds()
                if request_age > PROFIT_TRADE_MANUAL_EXECUTION_REQUEST_TTL_SECONDS:
                    raise RuntimeError(
                        "manual execution confirmation expired before the batch could reserve assets; "
                        "please review the latest ROI and confirm again"
                    )
            if not _profit_trade_new_action_allowed(new_action_guard):
                raise RuntimeError("Profit Trade runtime was disabled before manual asset reservation")

            if c5_client is None:
                c5_client = _build_profit_trade_c5_client(
                    settings,
                    run_id=normalized_request_id,
                    market_hash_name=normalized_name,
                )
            if inventory_payload is None:
                inventory_payload = fetch_all_c5_inventories(
                    c5_client,
                    settings,
                    allow_cached_fallback=False,
                    cache_max_age_minutes=None,
                )
            inventory_items = [
                item
                for item in list((inventory_payload or {}).get("list") or [])
                if isinstance(item, dict)
            ]
            db.upsert_inventory_assets(inventory_items)
            item_type = next(
                (
                    item
                    for item in summarize_inventory_types(inventory_items)
                    if str(item.get("market_hash_name") or "").strip() == normalized_name
                ),
                None,
            )
            if item_type is None or int(item_type.get("tradable_count") or 0) <= 0:
                raise RuntimeError("the selected item no longer has tradable C5 inventory")
            reference_price = safe_float(item_type.get("reference_price"))
            if (
                reference_price is not None
                and reference_price < float(config.profit_trade_min_item_value)
            ):
                raise RuntimeError(
                    f"the selected item is below the configured minimum item value: "
                    f"CNY {reference_price:.2f} < CNY {config.profit_trade_min_item_value:.2f}"
                )

            executable_assets = _list_executable_sell_assets(
                db,
                config,
                inventory_items,
                market_hash_name=normalized_name,
            )
            if len(executable_assets) < missing_quantity:
                raise RuntimeError(
                    f"only {len(executable_assets)} unreserved executable assets remain; "
                    f"{missing_quantity} more are required for this confirmed batch"
                )

            market_service = market_service or _build_profit_trade_market_service(
                settings,
                telemetry_context={
                    "run_id": normalized_request_id,
                    "market_hash_name": normalized_name,
                    "stage": "manual_execution_refresh",
                },
            )
            states = market_service.refresh_items([item_type])
            state = next(
                (item for item in states if item.market_hash_name == normalized_name),
                None,
            )
            if state is None or not _state_price_is_usable_for_profit_trade(state):
                raise RuntimeError("manual execution could not refresh a usable Steam/C5 price pair")
            c5_risks = _fetch_c5_recent_sale_risks(
                c5_client,
                app_id=settings.app_id,
                market_hash_names=[normalized_name],
            )
            evaluation = _build_market_evaluation(
                config=config,
                item_type=item_type,
                state=state,
                c5_risk=c5_risks.get(normalized_name),
            )
            if evaluation is None:
                raise RuntimeError("manual execution market evaluation is unavailable")
            if evaluation.expected_roi <= 0:
                raise RuntimeError(
                    f"current ROI is no longer positive: {evaluation.expected_roi * 100:.2f}%"
                )
            if evaluation.execution_status not in {
                "executable",
                "below_min_roi",
            }:
                raise RuntimeError(
                    "manual ROI approval cannot bypass the current non-ROI guard: "
                    f"{evaluation.execution_reason}"
                )
            if _profit_trade_roi_gate_value(evaluation.expected_roi) + 1e-12 < approved_roi_floor:
                raise RuntimeError(
                    "current ROI is lower than the value confirmed by the user: "
                    f"{evaluation.expected_roi * 100:.2f}% < {approved_roi_floor * 100:.2f}%; "
                    "please review and confirm again"
                )

            daily_budget = max(0.0, float(config.profit_trade_daily_steam_budget))
            daily_spent = _profit_trade_daily_steam_spent(db)
            planned_total = float(evaluation.steam_buy_price) * float(missing_quantity)
            if daily_budget > 0 and daily_spent + planned_total > daily_budget + 1e-9:
                raise RuntimeError(
                    "manual execution would exceed the daily Steam budget: "
                    f"spent CNY {daily_spent:.2f} + planned CNY {planned_total:.2f} "
                    f"> budget CNY {daily_budget:.2f}"
                )

            if existing_approval_expires_at is not None:
                approval_expires_at = existing_approval_expires_at.astimezone(
                    timezone.utc
                ).replace(microsecond=0).isoformat()
            else:
                approval_seconds = max(
                    PROFIT_TRADE_MANUAL_EXECUTION_MIN_APPROVAL_SECONDS,
                    requested_quantity * 5 * 60,
                )
                approval_expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=approval_seconds)
                ).replace(microsecond=0).isoformat()
            try:
                for batch_index, sell_item in enumerate(
                    executable_assets[:missing_quantity],
                    start=len(existing_rows) + 1,
                ):
                    opportunity = _opportunity_from_market_evaluation(
                        evaluation,
                        sell_item=sell_item,
                    )
                    trade_id = _create_profit_trade_from_opportunity(
                        db,
                        config,
                        opportunity,
                        lock_asset=True,
                        origin_scan_id=str(approved_scan_id or "").strip() or None,
                        origin_observed_at=approved_observed_at,
                        source="profit_trade_manual_execution",
                        reserved_until_override=approval_expires_at,
                        note_overrides={
                            "minRoiAtOpen": approved_roi_floor,
                            "minRoiAtOpenSource": "manual_execution_approved_snapshot",
                            "manualExecutionApproved": True,
                            "manualExecutionRequestId": normalized_request_id,
                            "manualExecutionRequestedAt": requested_at,
                            "manualExecutionApprovedAt": utc_now_iso(),
                            "manualExecutionApprovedObservedAt": approved_observed_at,
                            "manualExecutionRoiFloor": approved_roi_floor,
                            "manualExecutionFreshRoi": round(evaluation.expected_roi, 6),
                            "manualExecutionAutomaticMinRoi": float(config.profit_trade_min_roi),
                            "manualExecutionApprovalExpiresAt": approval_expires_at,
                            "manualExecutionBatchQuantity": requested_quantity,
                            "manualExecutionBatchIndex": batch_index,
                        },
                    )
                    if trade_id is None:
                        raise RuntimeError(
                            "an asset became unavailable while the manual batch was being reserved"
                        )
                    created_trade_ids.append(trade_id)
                    db.add_profit_trade_audit_event(
                        trade_id,
                        event_type="manual_execution_approved",
                        reason="user confirmed one-time inventory watch execution",
                        context={
                            "requestId": normalized_request_id,
                            "batchQuantity": requested_quantity,
                            "batchIndex": batch_index,
                            "approvedRoiFloor": approved_roi_floor,
                            "automaticMinRoi": float(config.profit_trade_min_roi),
                            "approvalExpiresAt": approval_expires_at,
                        },
                    )
            except Exception:
                for created_trade_id in created_trade_ids:
                    _cancel_locked_trade_before_steam_buy(
                        db,
                        created_trade_id,
                        reason=(
                            "manual batch reservation was rolled back before any Steam purchase"
                        ),
                    )
                raise
            existing_rows = db.list_profit_trades_for_manual_request(
                normalized_request_id,
                limit=PROFIT_TRADE_MANUAL_EXECUTION_MAX_QUANTITY,
            )
    finally:
        db.close()

    trade_ids = [int(row["id"]) for row in existing_rows[:requested_quantity]]
    bought_trade_ids: list[int] = []
    listed_trade_ids: list[int] = []
    skipped_trade_ids: list[int] = []
    errors: list[str] = []
    long_buy_fill_trade_ids: list[int] = []
    long_buy_listed_trade_ids: list[int] = []
    long_buy_manual_trade_ids: list[int] = []
    manual_long_buy_scan: ProfitTradeScanReport | None = None

    def stop_remaining(after_index: int, reason: str) -> None:
        for remaining_id in trade_ids[after_index + 1 :]:
            if _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                remaining_id,
                reason=reason,
            ):
                skipped_trade_ids.append(remaining_id)

    def manual_long_buy_fill_scan_context() -> ProfitTradeScanReport:
        """Build the minimum private scan input needed to hand off one fill.

        A direct-buy cancellation race is already a confirmed Steam purchase.
        It must use the same old-A reservation and C5-listing state machine as
        the automatic cycle, but a recovered manual batch may no longer have
        the original scan object in memory.  Only on that confirmed race do we
        read C5 inventory again, so normal manual direct purchases do not pay
        an extra inventory round trip.
        """

        nonlocal c5_client, inventory_payload, manual_long_buy_scan
        if manual_long_buy_scan is not None:
            return manual_long_buy_scan
        if c5_client is None:
            c5_client = _build_profit_trade_c5_client(
                settings,
                run_id=normalized_request_id,
                market_hash_name=normalized_name,
                stage="manual_long_buy_fill_handoff",
            )
        if inventory_payload is None:
            inventory_payload = fetch_all_c5_inventories(
                c5_client,
                settings,
                allow_cached_fallback=False,
                cache_max_age_minutes=None,
            )
        inventory_items = [
            item
            for item in list((inventory_payload or {}).get("list") or [])
            if isinstance(item, dict)
        ]
        inventory_db = Database(settings.db_path)
        try:
            inventory_db.initialize()
            inventory_db.upsert_inventory_assets(inventory_items)
        finally:
            inventory_db.close()
        manual_long_buy_scan = ProfitTradeScanReport(
            generated_at=utc_now_iso(),
            inventory_source=str((inventory_payload or {}).get("source") or "live"),
            inventory_count=len(inventory_items),
            evaluated_count=0,
            opportunity_count=0,
            missing_price_count=0,
            skipped_count=0,
            opportunities=[],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=inventory_items,
            watch_records=[],
        )
        return manual_long_buy_scan

    def advance_manual_direct_cancel_race_fills(
        fill_ids: list[int] | tuple[int, ...],
        *,
        source_trade_id: int,
    ) -> bool:
        """Immediately hand an old-buy cancellation race to C5 listing.

        ``execute_profit_trade_buy`` returns these IDs only after it has found
        real Steam purchase evidence while cancelling the old managed buy order.
        A second direct purchase would risk buying B twice, so this function
        imports only those receipts, locks an old A, and lists it in the same
        manual request instead of waiting for the next automatic cycle.
        """

        normalized_fill_ids = sorted(
            {
                normalized_fill_id
                for value in fill_ids
                if (normalized_fill_id := safe_int(value)) is not None
                and normalized_fill_id > 0
            }
        )
        if not normalized_fill_ids:
            return False
        try:
            processed = _process_pending_profit_trade_long_buy_fills(
                settings,
                config,
                scanned=manual_long_buy_fill_scan_context(),
                fill_ids=normalized_fill_ids,
            )
        except Exception as exc:
            errors.append(
                f"long-buy cancellation-race handoff after trade {source_trade_id}: {exc}"
            )
            return False
        errors.extend(processed.errors)

        known_trade_ids = list(
            dict.fromkeys(
                [
                    *processed.imported_trade_ids,
                    *processed.imported_steam_bought_trade_ids,
                    *processed.manual_trade_ids,
                ]
            )
        )
        # A concurrent reconciler can consume the fill between the direct-buy
        # gate and this handoff.  Re-read the exact receipt IDs so the manual
        # request remains idempotent and still advances a freshly created
        # steam_bought row rather than reporting a false failure.
        fill_db = Database(settings.db_path)
        try:
            fill_db.initialize()
            for fill_id in normalized_fill_ids:
                fill = fill_db.get_profit_trade_long_buy_fill(fill_id)
                if fill is None:
                    errors.append(
                        f"long-buy cancellation race reported missing fill {fill_id}"
                    )
                    continue
                trade_id = safe_int(fill["profit_trade_id"])
                if trade_id is not None and trade_id > 0 and trade_id not in known_trade_ids:
                    known_trade_ids.append(trade_id)
        finally:
            fill_db.close()

        if not known_trade_ids:
            errors.append(
                "long-buy cancellation race had no importable Profit Trade receipt; "
                "direct purchase was stopped for safety"
            )
            return False

        for imported_trade_id in known_trade_ids:
            if imported_trade_id not in long_buy_fill_trade_ids:
                long_buy_fill_trade_ids.append(imported_trade_id)
            trade_db = Database(settings.db_path)
            try:
                trade_db.initialize()
                imported_row = trade_db.get_profit_trade(imported_trade_id)
                imported_status = (
                    str(imported_row["status"] or "")
                    if imported_row is not None
                    else "missing"
                )
            finally:
                trade_db.close()

            if imported_status in {"completed", "c5_listed"}:
                if imported_trade_id not in listed_trade_ids:
                    listed_trade_ids.append(imported_trade_id)
                if imported_trade_id not in long_buy_listed_trade_ids:
                    long_buy_listed_trade_ids.append(imported_trade_id)
                continue
            if imported_status == "manual_required":
                if imported_trade_id not in long_buy_manual_trade_ids:
                    long_buy_manual_trade_ids.append(imported_trade_id)
                continue
            if imported_status not in {"steam_bought", "listing_c5"}:
                errors.append(
                    "long-buy cancellation-race handoff produced an unsafe "
                    f"trade state for {imported_trade_id}: {imported_status}"
                )
                continue
            try:
                list_result = execute_profit_trade_list_c5(
                    settings,
                    imported_trade_id,
                    config=config,
                    c5_client=c5_client,
                )
            except Exception as exc:
                errors.append(
                    "list-c5 long-buy cancellation-race fill "
                    f"{imported_trade_id} after direct trade {source_trade_id}: {exc}"
                )
                continue
            if list_result.get("ok"):
                if imported_trade_id not in listed_trade_ids:
                    listed_trade_ids.append(imported_trade_id)
                if imported_trade_id not in long_buy_listed_trade_ids:
                    long_buy_listed_trade_ids.append(imported_trade_id)
            else:
                errors.append(
                    "list-c5 long-buy cancellation-race fill "
                    f"{imported_trade_id} did not complete"
                )

        if long_buy_manual_trade_ids:
            errors.append(
                "confirmed long-term buy fill has no safe old-A/C5 handoff and "
                "was moved to manual_required: "
                + ", ".join(str(value) for value in long_buy_manual_trade_ids)
            )
        return True

    for index, trade_id in enumerate(trade_ids):
        db_row = Database(settings.db_path)
        try:
            db_row.initialize()
            row = db_row.get_profit_trade(trade_id)
            status = str(row["status"] or "") if row is not None else "missing"
            daily_spent = _profit_trade_daily_steam_spent(db_row)
            planned_price = safe_float(row["steam_buy_price"]) if row is not None else None
        finally:
            db_row.close()
        if status in {"completed", "c5_listed"}:
            listed_trade_ids.append(trade_id)
            continue
        if status == "steam_bought":
            try:
                list_result = execute_profit_trade_list_c5(
                    settings,
                    trade_id,
                    config=config,
                    c5_client=c5_client,
                )
            except Exception as exc:
                errors.append(f"list-c5 {trade_id}: {exc}")
                stop_remaining(index, f"manual batch stopped after C5 listing error on trade {trade_id}")
                break
            if list_result.get("ok"):
                listed_trade_ids.append(trade_id)
                continue
            errors.append(f"list-c5 {trade_id}: C5 listing did not complete")
            stop_remaining(index, f"manual batch stopped after C5 listing failure on trade {trade_id}")
            break
        if status != "locked":
            skipped_trade_ids.append(trade_id)
            errors.append(f"trade {trade_id} cannot continue from status {status}")
            stop_remaining(index, f"manual batch stopped after unsafe status {status} on trade {trade_id}")
            break
        latest_config = (
            load_strategy_config(settings)
            if refresh_config_each_item
            else config
        )
        current_protection_reason = _profit_trade_protection_reason(
            latest_config,
            asset_id=str(row["a_asset_id"] or "") if row is not None else None,
            market_hash_name=str(row["market_hash_name"] or "") if row is not None else normalized_name,
            steam_id=str(row["a_steam_id"] or "") if row is not None else None,
        )
        if current_protection_reason is None:
            current_protection_reason = _profit_trade_type_block_reason(
                latest_config,
                str(row["market_hash_name"] or "") if row is not None else normalized_name,
            )
        if (
            not latest_config.profit_trade_enabled
            or not latest_config.profit_trade_allow_real_execution
            or current_protection_reason is not None
        ):
            reason = (
                f"manual batch stopped by the latest protection rule: {current_protection_reason}"
                if current_protection_reason is not None
                else "manual batch stopped because Profit Trade real execution was disabled"
            )
            skipped_trade_ids.append(trade_id)
            _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                trade_id,
                reason=reason,
            )
            stop_remaining(index, reason)
            errors.append(reason)
            break
        config = latest_config
        if not _profit_trade_new_action_allowed(new_action_guard):
            skipped_trade_ids.append(trade_id)
            _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                trade_id,
                reason="Profit Trade runtime was disabled before this manual batch purchase",
            )
            stop_remaining(index, "Profit Trade runtime was disabled during the manual batch")
            errors.append("Profit Trade runtime was disabled during the manual batch")
            break
        daily_budget = max(0.0, float(config.profit_trade_daily_steam_budget))
        if (
            daily_budget > 0
            and planned_price is not None
            and daily_spent + planned_price > daily_budget + 1e-9
        ):
            skipped_trade_ids.append(trade_id)
            _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                trade_id,
                reason="manual batch stopped by the daily Steam budget guard",
            )
            stop_remaining(index, "manual batch stopped by the daily Steam budget guard")
            errors.append(
                f"daily Steam budget would be exceeded before trade {trade_id}: "
                f"CNY {daily_spent:.2f} + CNY {planned_price:.2f} > CNY {daily_budget:.2f}"
            )
            break
        try:
            buy_result = execute_profit_trade_buy(
                settings,
                trade_id,
                config=config,
                steam_client=steam_client,
                c5_client=c5_client,
                new_action_guard=new_action_guard,
                refresh_config_before_purchase=refresh_config_each_item,
            )
        except Exception as exc:
            errors.append(f"buy {trade_id}: {exc}")
            _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                trade_id,
                reason=f"manual batch buy failed before a confirmed purchase: {exc}",
            )
            stop_remaining(index, f"manual batch stopped after buy error on trade {trade_id}")
            break
        if not buy_result.get("ok"):
            skipped_trade_ids.append(trade_id)
            cancellation_race_fill_ids = list(buy_result.get("longBuyFillIds") or [])
            if cancellation_race_fill_ids:
                # The direct-buy gate already cancels this pre-buy row before it
                # reports a confirmed long-buy fill.  This extra idempotent
                # cancellation keeps the invariant true if an adapter returns
                # the receipt after a local pre-buy state has not yet persisted.
                _cancel_locked_trade_before_steam_buy_by_id(
                    settings,
                    trade_id,
                    reason=(
                        "manual batch stopped its direct purchase because the "
                        "managed long-term buy order filled during cancellation"
                    ),
                )
                advance_manual_direct_cancel_race_fills(
                    cancellation_race_fill_ids,
                    source_trade_id=trade_id,
                )
                stop_remaining(
                    index,
                    (
                        "manual batch stopped remaining direct purchases because "
                        "a managed long-term buy order filled for this item"
                    ),
                )
                break
            errors.append(f"buy {trade_id}: purchase did not reach a confirmed success state")
            stop_remaining(index, f"manual batch stopped after buy failure on trade {trade_id}")
            break
        bought_trade_ids.append(trade_id)
        try:
            list_result = execute_profit_trade_list_c5(
                settings,
                trade_id,
                config=config,
                c5_client=c5_client,
            )
        except Exception as exc:
            errors.append(f"list-c5 {trade_id}: {exc}")
            stop_remaining(index, f"manual batch stopped after C5 listing error on trade {trade_id}")
            break
        if list_result.get("ok"):
            listed_trade_ids.append(trade_id)
            continue
        skipped_trade_ids.append(trade_id)
        errors.append(f"list-c5 {trade_id}: C5 listing did not complete")
        stop_remaining(index, f"manual batch stopped after C5 listing failure on trade {trade_id}")
        break

    result = {
        "ok": not errors,
        "requestId": normalized_request_id,
        "marketHashName": normalized_name,
        "requestedQuantity": requested_quantity,
        "createdTradeIds": created_trade_ids,
        "tradeIds": trade_ids,
        "boughtTradeIds": bought_trade_ids,
        "listedTradeIds": listed_trade_ids,
        "longBuyFillTradeIds": long_buy_fill_trade_ids,
        "longBuyListedTradeIds": long_buy_listed_trade_ids,
        "longBuyManualTradeIds": long_buy_manual_trade_ids,
        "skippedTradeIds": sorted(set(skipped_trade_ids)),
        "errors": errors,
        "summary": (
            f"人工确认 {requested_quantity} 件，已买入 {len(bought_trade_ids)} 件，"
            f"已上架 C5 {len(listed_trade_ids)} 件；"
            f"长期求购成交接入 {len(long_buy_fill_trade_ids)} 件，"
            f"C5 上架 {len(long_buy_listed_trade_ids)} 件"
        ),
    }
    logger.emit(
        level="INFO" if result["ok"] else "WARN",
        provider="local",
        component="profit_trade_manual_execution",
        operation="batch_completed",
        message="User-confirmed Profit Trade batch completed",
        run_id=normalized_request_id,
        market_hash_name=normalized_name,
        safe_context={
            "requested_quantity": requested_quantity,
            "trade_ids": trade_ids,
            "bought_trade_ids": bought_trade_ids,
            "listed_trade_ids": listed_trade_ids,
            "long_buy_fill_trade_ids": long_buy_fill_trade_ids,
            "long_buy_listed_trade_ids": long_buy_listed_trade_ids,
            "long_buy_manual_trade_ids": long_buy_manual_trade_ids,
            "skipped_trade_ids": result["skippedTradeIds"],
            "errors": errors,
        },
    )
    return result


@dataclass(slots=True)
class ProfitTradeLongBuyReconcileResult:
    checked_order_ids: list[int] = field(default_factory=list)
    new_fill_ids: list[int] = field(default_factory=list)
    new_fill_market_hash_names: list[str] = field(default_factory=list)
    uncertain_order_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProfitTradeLongBuyCycleResult:
    imported_trade_ids: list[int] = field(default_factory=list)
    imported_steam_bought_trade_ids: list[int] = field(default_factory=list)
    manual_trade_ids: list[int] = field(default_factory=list)
    processed_market_hash_names: list[str] = field(default_factory=list)
    fill_market_hash_names: list[str] = field(default_factory=list)
    created_order_ids: list[int] = field(default_factory=list)
    replaced_order_ids: list[int] = field(default_factory=list)
    direct_purchase_block_reasons: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProfitTradeMyListingsBuyOrderSnapshot:
    """A complete-or-explicitly-partial Steam active-buy-order read.

    ``/market/mylistings`` exposes an ordinary paginated endpoint.  A short
    first page is useful positive evidence, but a bounded first page must not
    be mistaken for the account's complete buy-order book: capacity accounting
    and remote-absence decisions both depend on that distinction.
    """

    buy_orders: list[dict[str, Any]]
    complete: bool
    pages_scanned: int
    official_buy_order_count: int | None = None
    error: str | None = None


def _profit_trade_long_buy_note(order: Any) -> dict[str, Any]:
    try:
        raw = order["note_json"]
    except (KeyError, IndexError, TypeError):
        raw = None
    return _read_note(raw)


def _emit_profit_trade_long_buy_log(
    *,
    operation: str,
    message: str,
    market_hash_name: str | None = None,
    level: str = "INFO",
    provider: str = "local",
    safe_context: dict[str, Any] | None = None,
) -> None:
    """Emit an operator-visible Profit Trade log without affecting state."""

    try:
        get_profit_trade_event_logger().emit(
            level=level,
            provider=provider,
            component="profit_trade_long_buy",
            operation=operation,
            message=message,
            market_hash_name=str(market_hash_name or "").strip() or None,
            safe_context=dict(safe_context or {}),
        )
    except Exception:
        # Observability is deliberately non-authoritative.  A logging failure
        # must never change a Steam order, fill, or Profit Trade state.
        pass


def _steam_buy_order_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw_orders = (payload or {}).get("buy_orders")
    if raw_orders is None:
        raw_orders = (payload or {}).get("buyOrders")
    if isinstance(raw_orders, dict):
        # Some adapters expose the order list together with a scoped total.
        # Prefer the real nested collection, while retaining the historical
        # ``{buyOrderId: order}`` shape used by Steam itself.
        for key in ("orders", "rows", "items", "data"):
            nested = raw_orders.get(key)
            if isinstance(nested, list):
                return [dict(row) for row in nested if isinstance(row, dict)]
            if isinstance(nested, dict):
                return [dict(row) for row in nested.values() if isinstance(row, dict)]
        return [dict(row) for row in raw_orders.values() if isinstance(row, dict)]
    if isinstance(raw_orders, list):
        return [dict(row) for row in raw_orders if isinstance(row, dict)]
    return []


def _steam_buy_order_id(row: dict[str, Any]) -> str:
    return str(
        row.get("buy_orderid")
        or row.get("buy_order_id")
        or row.get("buyOrderId")
        or row.get("orderid")
        or ""
    ).strip()


def _steam_buy_order_market_hash_name(row: dict[str, Any]) -> str:
    return str(
        row.get("market_hash_name")
        or row.get("marketHashName")
        or row.get("hash_name")
        or row.get("name")
        or ""
    ).strip()


def _steam_buy_order_price_cents(row: dict[str, Any]) -> int | None:
    raw = (
        row.get("price_total")
        if row.get("price_total") not in (None, "")
        else row.get("priceTotal")
    )
    if raw in (None, ""):
        raw = row.get("price")
    if isinstance(raw, bool) or raw in (None, ""):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        if not math.isfinite(raw) or raw <= 0:
            return None
        if raw.is_integer():
            return int(raw)
        return price_to_cents(raw)
    text = str(raw).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        value = int(text)
        return value if value > 0 else None
    match = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    return price_to_cents(match.group(1)) if match else None


def _steam_buy_order_remaining_quantity(row: dict[str, Any]) -> int | None:
    value = safe_int(
        row.get("quantity_remaining")
        if row.get("quantity_remaining") not in (None, "")
        else row.get("quantityRemaining")
        if row.get("quantityRemaining") not in (None, "")
        else row.get("remaining_quantity")
        if row.get("remaining_quantity") not in (None, "")
        else row.get("quantity")
    )
    return max(0, int(value)) if value is not None else None


def _steam_buy_order_nominal_total_cents(
    rows: list[dict[str, Any]],
) -> tuple[int | None, str | None]:
    total = 0
    for row in rows:
        remaining = _steam_buy_order_remaining_quantity(row)
        price_cents = _steam_buy_order_price_cents(row)
        if remaining is None or price_cents is None:
            return None, (
                "Steam active buy-order nominal total is unreadable: "
                f"order={_steam_buy_order_id(row) or '-'}"
            )
        total += price_cents * remaining
    return total, None


def _read_profit_trade_my_listings_page(
    client: Any,
    *,
    start: int,
    count: int,
) -> dict[str, Any]:
    try:
        payload = client.my_listings(
            start=max(0, int(start)),
            count=max(1, int(count)),
            safety_terminal=True,
        )
    except TypeError as exc:
        if "safety_terminal" not in str(exc):
            raise
        payload = client.my_listings(
            start=max(0, int(start)),
            count=max(1, int(count)),
        )
    if not isinstance(payload, dict):
        raise RuntimeError("Steam mylistings returned a non-object payload")
    if payload.get("success") not in (None, 1, True, "1"):
        raise RuntimeError("Steam mylistings returned an unsuccessful payload")
    return payload


def _profit_trade_my_listings_buy_order_total_signal(
    payload: dict[str, Any],
) -> tuple[int | None, str | None]:
    """Read only buy-order-specific totals; generic ``total_count`` is sell-side."""

    total_keys = {
        "buyordercount",
        "buyorderscount",
        "totalbuyordercount",
        "totalbuyorders",
        "numbuyorders",
        "numactivebuyorders",
        "activebuyordercount",
        "activebuyorderscount",
        "buyorderstotal",
        "buyordertotal",
    }
    containers: list[dict[str, Any]] = [payload]
    raw_orders = payload.get("buy_orders")
    if raw_orders is None:
        raw_orders = payload.get("buyOrders")
    if isinstance(raw_orders, dict):
        containers.append(raw_orders)

    found: list[tuple[str, int]] = []
    for container_index, container in enumerate(containers):
        for raw_key, raw_value in container.items():
            key = re.sub(r"[^a-z0-9]", "", str(raw_key).lower())
            # ``total_count`` at the response root describes sell listings on
            # Steam, but it is a valid scoped total inside a structured
            # ``buy_orders`` container.
            if key not in total_keys and not (
                container_index > 0 and key == "totalcount"
            ):
                continue
            value = safe_int(raw_value)
            if isinstance(raw_value, bool) or value is None or value < 0:
                return None, (
                    "Steam mylistings buy-order total is unreadable: "
                    f"{raw_key}={raw_value!r}"
                )
            found.append((str(raw_key), int(value)))
    if not found:
        return None, None
    values = {value for _key, value in found}
    if len(values) != 1:
        rendered = ", ".join(f"{key}={value}" for key, value in found)
        return None, f"Steam mylistings buy-order totals disagree: {rendered}"
    return found[0][1], None


def _incomplete_profit_trade_my_listings_buy_order_snapshot(
    buy_orders: list[dict[str, Any]],
    *,
    pages_scanned: int,
    official_buy_order_count: int | None,
    error: str,
) -> ProfitTradeMyListingsBuyOrderSnapshot:
    return ProfitTradeMyListingsBuyOrderSnapshot(
        buy_orders=buy_orders,
        complete=False,
        pages_scanned=pages_scanned,
        official_buy_order_count=official_buy_order_count,
        error=error,
    )


def _call_profit_trade_my_listings(client: Any) -> ProfitTradeMyListingsBuyOrderSnapshot:
    """Read every active Steam buy order, or retain an explicitly partial view.

    A page with fewer than ``count`` buy orders is an end-of-list signal when
    Steam does not provide an explicit buy-order total.  When a page is full,
    we must paginate.  A failed/unstable continuation remains usable only for
    positive matches; it is never sufficient evidence that a remote order is
    absent or that a new order fits the 10x nominal limit.
    """

    page_size = PROFIT_TRADE_LONG_BUY_MY_LISTINGS_PAGE_SIZE
    all_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    official_count: int | None = None
    pages_scanned = 0

    for page_index in range(PROFIT_TRADE_LONG_BUY_MY_LISTINGS_MAX_PAGES):
        start = page_index * page_size
        try:
            payload = _read_profit_trade_my_listings_page(
                client,
                start=start,
                count=page_size,
            )
        except Exception as exc:
            if page_index == 0:
                raise
            return _incomplete_profit_trade_my_listings_buy_order_snapshot(
                all_rows,
                pages_scanned=pages_scanned,
                official_buy_order_count=official_count,
                error=(
                    "Steam mylistings pagination failed at "
                    f"start={start}: {exc}"
                ),
            )

        pages_scanned += 1
        page_rows = _steam_buy_order_rows(payload)
        page_total, total_error = _profit_trade_my_listings_buy_order_total_signal(
            payload
        )
        if total_error:
            return _incomplete_profit_trade_my_listings_buy_order_snapshot(
                all_rows + page_rows,
                pages_scanned=pages_scanned,
                official_buy_order_count=official_count,
                error=total_error,
            )
        if official_count is None:
            official_count = page_total
        elif page_total is not None and page_total != official_count:
            return _incomplete_profit_trade_my_listings_buy_order_snapshot(
                all_rows + page_rows,
                pages_scanned=pages_scanned,
                official_buy_order_count=official_count,
                error=(
                    "Steam mylistings buy-order total changed during pagination: "
                    f"{official_count} -> {page_total}"
                ),
            )

        # A scoped official total paired with every row in this response is a
        # self-contained complete read, even if this adapter did not cap the
        # buy-order section to the requested page size.
        if page_index == 0 and official_count is not None and len(page_rows) == official_count:
            return ProfitTradeMyListingsBuyOrderSnapshot(
                buy_orders=page_rows,
                complete=True,
                pages_scanned=pages_scanned,
                official_buy_order_count=official_count,
            )

        if len(page_rows) > page_size:
            return _incomplete_profit_trade_my_listings_buy_order_snapshot(
                all_rows + page_rows,
                pages_scanned=pages_scanned,
                official_buy_order_count=official_count,
                error=(
                    "Steam mylistings returned more active buy orders than the "
                    f"requested page size at start={start} without a matching total"
                ),
            )

        # A short first page is a complete snapshot.  For all other cases we
        # need stable order identities before the pages can be merged safely.
        if page_index == 0 and len(page_rows) < page_size and official_count is None:
            return ProfitTradeMyListingsBuyOrderSnapshot(
                buy_orders=page_rows,
                complete=True,
                pages_scanned=pages_scanned,
            )

        for row in page_rows:
            buy_order_id = _steam_buy_order_id(row)
            if not buy_order_id:
                return _incomplete_profit_trade_my_listings_buy_order_snapshot(
                    all_rows + page_rows,
                    pages_scanned=pages_scanned,
                    official_buy_order_count=official_count,
                    error=(
                        "Steam mylistings needs another buy-order page but one "
                        "or more rows have no stable buy-order id"
                    ),
                )
            if buy_order_id in seen_ids:
                return _incomplete_profit_trade_my_listings_buy_order_snapshot(
                    all_rows,
                    pages_scanned=pages_scanned,
                    official_buy_order_count=official_count,
                    error=(
                        "Steam mylistings buy-order pagination repeated "
                        f"buy_orderid={buy_order_id} at start={start}"
                    ),
                )
            seen_ids.add(buy_order_id)
            all_rows.append(row)

        if official_count is not None:
            if len(all_rows) == official_count:
                return ProfitTradeMyListingsBuyOrderSnapshot(
                    buy_orders=all_rows,
                    complete=True,
                    pages_scanned=pages_scanned,
                    official_buy_order_count=official_count,
                )
            if len(all_rows) > official_count:
                return _incomplete_profit_trade_my_listings_buy_order_snapshot(
                    all_rows,
                    pages_scanned=pages_scanned,
                    official_buy_order_count=official_count,
                    error=(
                        "Steam mylistings returned more unique buy orders than "
                        f"its official total ({len(all_rows)}/{official_count})"
                    ),
                )
            if len(page_rows) < page_size:
                return _incomplete_profit_trade_my_listings_buy_order_snapshot(
                    all_rows,
                    pages_scanned=pages_scanned,
                    official_buy_order_count=official_count,
                    error=(
                        "Steam mylistings buy-order page ended before its official "
                        f"total ({len(all_rows)}/{official_count})"
                    ),
                )
        elif len(page_rows) < page_size:
            return ProfitTradeMyListingsBuyOrderSnapshot(
                buy_orders=all_rows,
                complete=True,
                pages_scanned=pages_scanned,
            )

    return _incomplete_profit_trade_my_listings_buy_order_snapshot(
        all_rows,
        pages_scanned=pages_scanned,
        official_buy_order_count=official_count,
        error=(
            "Steam mylistings buy-order pagination exceeded the safe page limit "
            f"({PROFIT_TRADE_LONG_BUY_MY_LISTINGS_MAX_PAGES})"
        ),
    )


def _profit_trade_long_buy_client_for_account(
    settings: Settings,
    *,
    steam_account_id: str,
    steam_id: str | None,
    steam_client: Any | None = None,
) -> Any:
    target_account = str(steam_account_id or "").strip()
    target_steam_id = str(steam_id or "").strip()
    if steam_client is not None:
        client_account = str(getattr(steam_client, "account_id", "") or "").strip()
        client_steam_id = str(getattr(steam_client, "steam_id64", "") or "").strip()
        account_matches = not target_account or not client_account or target_account == client_account
        steam_matches = not target_steam_id or not client_steam_id or target_steam_id == client_steam_id
        if account_matches and steam_matches:
            return steam_client

    store = AccountStore(PROJECT_ROOT / "config")
    account = next(
        (
            item
            for item in store.list_accounts()
            if (
                target_account
                and str(item.id or "").strip() == target_account
            )
            or (
                target_steam_id
                and str(item.steam_id64 or "").strip() == target_steam_id
            )
        ),
        None,
    )
    if account is None or not account.cookies:
        raise RuntimeError(
            "managed long-term buy order account is unavailable: "
            f"accountId={target_account or '-'}, steamId={target_steam_id or '-'}"
        )
    return _build_steam_client_for_account(
        settings,
        account,
        telemetry_context={
            "account_id": str(account.id or ""),
            "steam_id64": str(account.steam_id64 or ""),
        },
    )


def _profit_trade_long_buy_receipt_lookup(
    client: Any,
    orders: list[Any],
) -> dict[str, Any]:
    finder = getattr(
        client,
        "find_purchase_receipts_for_targets_with_coverage",
        None,
    )
    if not callable(finder):
        return {
            "receipts": {str(int(order["id"])): () for order in orders},
            "coverageComplete": False,
            "lookupSucceeded": False,
            "error": "Steam client does not support batched purchase-history coverage",
        }
    targets = [
        {
            "key": str(int(order["id"])),
            "marketHashName": str(order["market_hash_name"] or ""),
            "maximumTotal": cents_to_price(order["bid_price_cents"]),
            "earliestTime": order["created_at"],
            "maxReceipts": max(1, int(order["quantity"] or 1)),
        }
        for order in orders
    ]
    try:
        result = finder(
            targets,
            count=PROFIT_TRADE_LONG_BUY_HISTORY_PAGE_SIZE,
            max_pages=PROFIT_TRADE_LONG_BUY_HISTORY_MAX_PAGES,
            safety_terminal=True,
        )
    except TypeError as exc:
        if "safety_terminal" not in str(exc):
            raise
        result = finder(
            targets,
            count=PROFIT_TRADE_LONG_BUY_HISTORY_PAGE_SIZE,
            max_pages=PROFIT_TRADE_LONG_BUY_HISTORY_MAX_PAGES,
        )
    receipts = getattr(result, "receipts", None)
    if receipts is None and isinstance(result, dict):
        receipts = result.get("receipts")
    return {
        "receipts": dict(receipts or {}),
        "coverageComplete": bool(
            getattr(result, "coverage_complete", None)
            if not isinstance(result, dict)
            else result.get("coverageComplete")
        ),
        "lookupSucceeded": bool(
            getattr(result, "lookup_succeeded", True)
            if not isinstance(result, dict)
            else result.get("lookupSucceeded", True)
        ),
        "error": (
            getattr(result, "error", None)
            if not isinstance(result, dict)
            else result.get("error")
        ),
    }


def _profit_trade_long_buy_receipt_time(receipt: dict[str, Any]) -> str | None:
    raw = receipt.get("timePurchased")
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(
            int(float(raw)),
            timezone.utc,
        ).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        text = str(raw or "").strip()
        return text or None


def _send_profit_trade_long_buy_uncertain_alert(
    settings: Settings,
    *,
    order: Any,
    reason: str,
) -> bool:
    if not settings.serverchan_sendkey:
        return False
    bid_price = cents_to_price(order["bid_price_cents"])
    price_line = (
        f"- 求购价: ¥{bid_price:.2f}"
        if bid_price is not None
        else "- 求购价: -"
    )
    body = "\n".join(
        [
            "## Profit Trade 长期求购终态不确定",
            "",
            f"- 本地订单 ID: {order['id']}",
            f"- Steam 求购单: {order['buy_order_id'] or '-'}",
            f"- 饰品: {order['market_hash_name']}",
            f"- Steam 账号: {order['steam_account_id']}",
            price_line,
            f"- 数量: {order['quantity']}",
            f"- 原因: {reason}",
            "",
            "程序已停止对这张订单执行撤换、重建或同品类直接购买，避免重复成交。",
        ]
    )
    ServerChanClient(
        settings.serverchan_sendkey,
        settings.serverchan_base_url,
    ).send("Profit Trade 长期求购需要人工核对", body)
    return True


def _mark_profit_trade_long_buy_uncertain(
    db: Database,
    settings: Settings,
    order: Any,
    *,
    reason: str,
    context: dict[str, Any] | None = None,
) -> Any:
    note = _profit_trade_long_buy_note(order)
    alert_attempted = bool(note.get("terminalUncertainAlertAttempted"))
    now = utc_now_iso()
    note.update(
        {
            "terminalUncertainAt": note.get("terminalUncertainAt") or now,
            "terminalUncertainReason": reason,
            "terminalUncertainAlertAttempted": alert_attempted,
        }
    )
    updated = db.update_profit_trade_long_buy_order(
        int(order["id"]),
        event_type="terminal_uncertain",
        reason=reason,
        context=context,
        state="terminal_uncertain",
        terminal_reason=reason,
        last_checked_at=now,
        note_json=note,
    )
    _emit_profit_trade_long_buy_log(
        operation="terminal_uncertain",
        message="Steam long-term buy terminal state is uncertain",
        market_hash_name=str(order["market_hash_name"] or ""),
        provider="steam",
        level="WARN",
        safe_context={
            "long_buy_order_id": int(order["id"]),
            "buy_order_id": str(order["buy_order_id"] or "") or None,
            "reason": reason,
            **dict(context or {}),
        },
    )
    if alert_attempted:
        return updated
    alert_error: str | None = None
    sent = False
    try:
        sent = _send_profit_trade_long_buy_uncertain_alert(
            settings,
            order=updated,
            reason=reason,
        )
    except Exception as exc:
        alert_error = str(exc)
    note = _profit_trade_long_buy_note(updated)
    note.update(
        {
            "terminalUncertainAlertAttempted": True,
            "terminalUncertainAlertSent": bool(sent),
            "terminalUncertainAlertAt": now if sent else None,
            "terminalUncertainAlertError": alert_error,
        }
    )
    return db.update_profit_trade_long_buy_order(
        int(updated["id"]),
        event_type="terminal_uncertain_alert_recorded",
        reason=alert_error,
        note_json=note,
    )


def _reconcile_profit_trade_long_buy_account(
    db: Database,
    settings: Settings,
    *,
    orders: list[Any],
    client: Any,
) -> ProfitTradeLongBuyReconcileResult:
    result = ProfitTradeLongBuyReconcileResult()
    if not orders:
        return result
    account_id = str(orders[0]["steam_account_id"] or "")
    try:
        listings_snapshot = _call_profit_trade_my_listings(client)
        remote_rows = listings_snapshot.buy_orders
    except Exception as exc:
        reason = f"Steam active buy-order lookup failed: {exc}"
        for order in orders:
            updated = _mark_profit_trade_long_buy_uncertain(
                db,
                settings,
                order,
                reason=reason,
                context={"accountId": account_id},
            )
            result.uncertain_order_ids.append(int(updated["id"]))
        result.errors.append(f"long-buy account {account_id}: {reason}")
        return result

    remote_snapshot_complete = bool(listings_snapshot.complete)
    remote_snapshot_error = str(listings_snapshot.error or "").strip() or None
    if not remote_snapshot_complete:
        result.errors.append(
            "long-buy account "
            f"{account_id}: Steam active buy-order snapshot is incomplete"
            f"{f': {remote_snapshot_error}' if remote_snapshot_error else ''}"
        )

    try:
        history = _profit_trade_long_buy_receipt_lookup(client, orders)
    except Exception as exc:
        history = {
            "receipts": {},
            "coverageComplete": False,
            "lookupSucceeded": False,
            "error": str(exc),
        }

    history_receipts = dict(history.get("receipts") or {})
    history_succeeded = bool(history.get("lookupSucceeded"))
    history_complete = bool(history.get("coverageComplete"))
    history_error = str(history.get("error") or "").strip() or None
    now = utc_now_iso()

    for original in orders:
        order = db.get_profit_trade_long_buy_order(int(original["id"])) or original
        order_id = int(order["id"])
        result.checked_order_ids.append(order_id)
        target_buy_order_id = str(order["buy_order_id"] or "").strip()
        target_name = str(order["market_hash_name"] or "").strip()
        target_price_cents = int(order["bid_price_cents"])
        exact_rows = [
            row
            for row in remote_rows
            if target_buy_order_id
            and _steam_buy_order_id(row) == target_buy_order_id
        ]
        create_match_count = 0
        if not target_buy_order_id:
            create_matches = [
                row
                for row in remote_rows
                if _steam_buy_order_market_hash_name(row) == target_name
                and _steam_buy_order_price_cents(row) == target_price_cents
                and (_steam_buy_order_remaining_quantity(row) or 0) > 0
            ]
            create_match_count = len(create_matches)
            if create_match_count == 1:
                exact_rows = create_matches
                target_buy_order_id = _steam_buy_order_id(create_matches[0])
        active_row = exact_rows[0] if exact_rows else None
        if (
            active_row is not None
            and (_steam_buy_order_remaining_quantity(active_row) or 0) <= 0
        ):
            active_row = None
        if create_match_count > 1:
            updated = _mark_profit_trade_long_buy_uncertain(
                db,
                settings,
                order,
                reason=(
                    "multiple same-item same-price Steam buy orders match one "
                    "local create intent"
                ),
                context={"matchCount": create_match_count},
            )
            result.uncertain_order_ids.append(int(updated["id"]))
            continue

        receipts = [
            dict(receipt)
            for receipt in list(history_receipts.get(str(order_id)) or [])
            if isinstance(receipt, dict)
        ]
        for receipt in receipts:
            currency_id = safe_int(receipt.get("currencyId"))
            if currency_id != 23:
                result.errors.append(
                    f"long-buy order {order_id}: ignored non-CNY receipt "
                    f"{receipt.get('purchaseId') or '-'} currency={currency_id}"
                )
                continue
            paid_total_cents = price_to_cents(receipt.get("paidTotal"))
            purchase_id = str(receipt.get("purchaseId") or "").strip()
            if (
                paid_total_cents is None
                or paid_total_cents > target_price_cents
                or not purchase_id
            ):
                result.errors.append(
                    f"long-buy order {order_id}: invalid purchase receipt "
                    f"{purchase_id or '-'}"
                )
                continue
            recorded = db.record_profit_trade_long_buy_fill(
                long_buy_order_id=order_id,
                steam_account_id=str(order["steam_account_id"] or ""),
                purchase_id=purchase_id,
                listing_id=str(receipt.get("listingId") or "").strip() or None,
                market_hash_name=target_name,
                paid_total_cents=paid_total_cents,
                asset_id=str(receipt.get("assetId") or "").strip() or None,
                new_asset_id=str(receipt.get("newAssetId") or "").strip() or None,
                purchased_at=_profit_trade_long_buy_receipt_time(receipt),
                evidence={
                    "source": "steam_market_history_event_type_4",
                    "receipt": receipt,
                    "historyCoverageComplete": history_complete,
                },
            )
            if recorded["inserted"]:
                result.new_fill_ids.append(int(recorded["id"]))
                if target_name not in result.new_fill_market_hash_names:
                    result.new_fill_market_hash_names.append(target_name)
                _emit_profit_trade_long_buy_log(
                    operation="fill_detected",
                    message="Steam official history confirmed a long-term buy fill",
                    market_hash_name=target_name,
                    provider="steam",
                    safe_context={
                        "long_buy_order_id": order_id,
                        "fill_id": int(recorded["id"]),
                        "purchase_id": purchase_id,
                        "paid_total": receipt.get("paidTotal"),
                        "currency_id": currency_id,
                    },
                )

        fill_rows = db.list_profit_trade_long_buy_fills(
            long_buy_order_id=order_id,
            limit=max(10, int(order["quantity"] or 1) + 5),
        )
        quantity = max(1, int(order["quantity"] or 1))
        receipt_fill_count = min(len(fill_rows), quantity)
        latest_fill_at = next(
            (
                str(row["purchased_at"])
                for row in reversed(fill_rows)
                if str(row["purchased_at"] or "").strip()
            ),
            None,
        )
        note = _profit_trade_long_buy_note(order)
        note.update(
            {
                "lastReconciledAt": now,
                "lastHistoryCoverageComplete": history_complete,
                "lastHistoryLookupSucceeded": history_succeeded,
                "lastHistoryError": history_error,
                "lastRemoteBuyOrdersComplete": remote_snapshot_complete,
                "lastRemoteBuyOrdersPageCount": listings_snapshot.pages_scanned,
                "lastRemoteBuyOrdersOfficialCount": (
                    listings_snapshot.official_buy_order_count
                ),
                "lastRemoteBuyOrdersError": remote_snapshot_error,
                "lastRemoteBuyOrder": (
                    _compact_steam_buy_order(active_row)
                    if active_row is not None
                    else None
                ),
            }
        )

        if active_row is not None:
            remote_remaining = _steam_buy_order_remaining_quantity(active_row)
            if remote_remaining is None:
                updated = _mark_profit_trade_long_buy_uncertain(
                    db,
                    settings,
                    order,
                    reason="matching Steam buy order has no readable remaining quantity",
                )
                result.uncertain_order_ids.append(int(updated["id"]))
                continue
            inferred_filled = max(0, quantity - remote_remaining)
            filled_quantity = min(
                quantity,
                max(receipt_fill_count, inferred_filled),
            )
            current_state = str(order["state"] or "")
            next_state = (
                "cancel_pending"
                if current_state == "cancel_pending"
                else "partial"
                if filled_quantity > 0
                else "active"
            )
            note.pop("remoteAbsenceObservedAt", None)
            db.update_profit_trade_long_buy_order(
                order_id,
                event_type="remote_order_reconciled",
                reason="matching Steam buy order is active",
                state=next_state,
                buy_order_id=target_buy_order_id or None,
                filled_quantity=filled_quantity,
                remaining_quantity=remote_remaining,
                last_checked_at=now,
                last_filled_at=latest_fill_at,
                terminal_reason=None,
                note_json=note,
            )
            _emit_profit_trade_long_buy_log(
                operation="order_reconciled",
                message="Steam long-term buy order remains active",
                market_hash_name=target_name,
                provider="steam",
                level="DEBUG",
                safe_context={
                    "long_buy_order_id": order_id,
                    "buy_order_id": target_buy_order_id,
                    "state": next_state,
                    "filled_quantity": filled_quantity,
                    "remaining_quantity": remote_remaining,
                },
            )
            continue

        if receipt_fill_count >= quantity:
            db.update_profit_trade_long_buy_order(
                order_id,
                event_type="remote_order_filled",
                reason="official Steam purchase receipts cover the full quantity",
                state="filled",
                filled_quantity=quantity,
                remaining_quantity=0,
                last_checked_at=now,
                last_filled_at=latest_fill_at,
                completed_at=now,
                terminal_reason="official Steam purchase receipts cover full quantity",
                note_json=note,
            )
            _emit_profit_trade_long_buy_log(
                operation="order_filled",
                message="Steam official history covers the full long-term buy quantity",
                market_hash_name=target_name,
                provider="steam",
                safe_context={
                    "long_buy_order_id": order_id,
                    "quantity": quantity,
                },
            )
            continue

        if not remote_snapshot_complete:
            absence_reason = (
                "Steam active buy-order snapshot is incomplete; remote absence "
                "cannot prove this long-term buy order was cancelled"
                f"{f': {remote_snapshot_error}' if remote_snapshot_error else ''}"
            )
            observed = db.update_profit_trade_long_buy_order(
                order_id,
                event_type="remote_snapshot_incomplete",
                reason=absence_reason,
                filled_quantity=receipt_fill_count,
                last_checked_at=now,
                last_filled_at=latest_fill_at,
                note_json=note,
            )
            updated = _mark_profit_trade_long_buy_uncertain(
                db,
                settings,
                observed,
                reason=absence_reason,
                context={
                    "accountId": account_id,
                    "remoteBuyOrdersPageCount": listings_snapshot.pages_scanned,
                    "remoteBuyOrdersOfficialCount": (
                        listings_snapshot.official_buy_order_count
                    ),
                },
            )
            result.uncertain_order_ids.append(int(updated["id"]))
            continue

        if not history_succeeded or not history_complete:
            reason = (
                "Steam buy order is absent but official purchase-history "
                f"coverage is incomplete{f': {history_error}' if history_error else ''}"
            )
            updated = _mark_profit_trade_long_buy_uncertain(
                db,
                settings,
                order,
                reason=reason,
            )
            result.uncertain_order_ids.append(int(updated["id"]))
            continue

        current_state = str(order["state"] or "")
        if current_state == "cancel_pending":
            db.update_profit_trade_long_buy_order(
                order_id,
                event_type="remote_cancel_confirmed",
                reason="buy order is absent and official history has complete coverage",
                state="cancelled",
                filled_quantity=receipt_fill_count,
                remaining_quantity=0,
                last_checked_at=now,
                last_filled_at=latest_fill_at,
                completed_at=now,
                terminal_reason="safe cancellation confirmed",
                note_json=note,
            )
            continue

        previous_absence = str(note.get("remoteAbsenceObservedAt") or "").strip()
        if current_state == "terminal_uncertain" and previous_absence:
            db.update_profit_trade_long_buy_order(
                order_id,
                event_type="remote_auto_cancel_confirmed",
                reason=(
                    "buy order remained absent across two complete official "
                    "history observations"
                ),
                state="auto_cancelled",
                filled_quantity=receipt_fill_count,
                remaining_quantity=0,
                last_checked_at=now,
                last_filled_at=latest_fill_at,
                completed_at=now,
                terminal_reason="Steam order disappeared without a complete fill",
                note_json=note,
            )
            _emit_profit_trade_long_buy_log(
                operation="auto_cancelled",
                message="Steam long-term buy order was confirmed automatically cancelled",
                market_hash_name=target_name,
                provider="steam",
                safe_context={
                    "long_buy_order_id": order_id,
                    "filled_quantity": receipt_fill_count,
                },
            )
            continue

        note["remoteAbsenceObservedAt"] = now
        order = db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_absence_observed",
            reason=(
                "buy order is absent with complete history coverage; one more "
                "cycle is required before classifying automatic cancellation"
            ),
            note_json=note,
            last_checked_at=now,
            filled_quantity=receipt_fill_count,
        )
        updated = _mark_profit_trade_long_buy_uncertain(
            db,
            settings,
            order,
            reason=(
                "Steam buy order disappeared without full-quantity purchase "
                "receipts; awaiting a second complete observation"
            ),
        )
        result.uncertain_order_ids.append(int(updated["id"]))
    return result


def _reconcile_profit_trade_long_buy_orders(
    settings: Settings,
    *,
    steam_client: Any | None = None,
) -> ProfitTradeLongBuyReconcileResult:
    db = Database(settings.db_path)
    combined = ProfitTradeLongBuyReconcileResult()
    try:
        db.initialize()
        live_orders = db.list_profit_trade_long_buy_orders(
            states=LONG_BUY_LIVE_STATES,
            limit=1000,
        )
        by_account: dict[str, list[Any]] = {}
        for order in live_orders:
            by_account.setdefault(
                str(order["steam_account_id"] or "").strip(),
                [],
            ).append(order)
        for account_id, account_orders in by_account.items():
            first = account_orders[0]
            try:
                client = _profit_trade_long_buy_client_for_account(
                    settings,
                    steam_account_id=account_id,
                    steam_id=str(first["steam_id"] or "").strip() or None,
                    steam_client=steam_client,
                )
                account_result = _reconcile_profit_trade_long_buy_account(
                    db,
                    settings,
                    orders=account_orders,
                    client=client,
                )
            except Exception as exc:
                reason = f"managed account reconciliation failed: {exc}"
                account_result = ProfitTradeLongBuyReconcileResult(
                    errors=[f"long-buy account {account_id}: {reason}"],
                )
                for order in account_orders:
                    updated = _mark_profit_trade_long_buy_uncertain(
                        db,
                        settings,
                        order,
                        reason=reason,
                        context={"accountId": account_id},
                    )
                    account_result.uncertain_order_ids.append(int(updated["id"]))
            combined.checked_order_ids.extend(account_result.checked_order_ids)
            combined.new_fill_ids.extend(account_result.new_fill_ids)
            for market_hash_name in account_result.new_fill_market_hash_names:
                if market_hash_name not in combined.new_fill_market_hash_names:
                    combined.new_fill_market_hash_names.append(market_hash_name)
            combined.uncertain_order_ids.extend(account_result.uncertain_order_ids)
            combined.errors.extend(account_result.errors)
    finally:
        db.close()
    return combined


def _profit_trade_long_buy_watch_by_market(
    scanned: ProfitTradeScanReport | None,
) -> dict[str, dict[str, Any]]:
    if scanned is None:
        return {}
    return {
        str(row.get("market_hash_name") or "").strip(): row
        for row in scanned.watch_records
        if isinstance(row, dict)
        and str(row.get("market_hash_name") or "").strip()
    }


def _process_pending_profit_trade_long_buy_fills(
    settings: Settings,
    config: StrategyConfig,
    *,
    scanned: ProfitTradeScanReport | None,
    fill_ids: list[int] | tuple[int, ...] | None = None,
) -> ProfitTradeLongBuyCycleResult:
    result = ProfitTradeLongBuyCycleResult()
    watch_by_market = _profit_trade_long_buy_watch_by_market(scanned)
    inventory_items = list(scanned.inventory_items if scanned is not None else [])
    target_fill_ids: set[int] | None = None
    if fill_ids is not None:
        target_fill_ids = set()
        for value in fill_ids:
            normalized_fill_id = safe_int(value)
            if normalized_fill_id is not None and normalized_fill_id > 0:
                target_fill_ids.add(normalized_fill_id)
    db = Database(settings.db_path)
    try:
        db.initialize()
        for fill in db.list_pending_profit_trade_long_buy_fills(limit=1000):
            fill_id = int(fill["id"])
            if target_fill_ids is not None and fill_id not in target_fill_ids:
                continue
            order = db.get_profit_trade_long_buy_order(int(fill["long_buy_order_id"]))
            if order is None:
                result.errors.append(
                    f"long-buy fill {fill_id}: parent order is missing"
                )
                continue
            market_hash_name = str(fill["market_hash_name"] or "").strip()
            watch = watch_by_market.get(market_hash_name) or {}
            raw = watch.get("raw") if isinstance(watch.get("raw"), dict) else {}
            proposal = (
                raw.get("longBuyProposal")
                if isinstance(raw.get("longBuyProposal"), dict)
                else {}
            )
            c5_price_batch = (
                safe_float(proposal.get("c5PriceBatch"))
                or safe_float(order["c5_price_batch"])
                or safe_float(watch.get("c5_listing_price"))
            )
            paid_price = cents_to_price(fill["paid_total_cents"])
            manual_reasons: list[str] = []
            if paid_price is None or paid_price <= 0:
                manual_reasons.append("official Steam receipt has no usable paidTotal")
                paid_price = max(
                    0.01,
                    cents_to_price(order["bid_price_cents"]) or 0.01,
                )
            if c5_price_batch is None or c5_price_batch <= 0:
                manual_reasons.append(
                    "no usable C5 price_batch snapshot is available for the confirmed fill"
                )
                c5_price_batch = max(
                    0.01,
                    safe_float(order["c5_price_batch"]) or 0.01,
                )

            sell_assets = _list_executable_sell_assets(
                db,
                config,
                inventory_items,
                market_hash_name=market_hash_name,
                limit=1,
            )
            sell_item = sell_assets[0] if sell_assets else None
            if sell_item is None:
                manual_reasons.append(
                    "Steam long-term buy order filled but no executable old A asset is available"
                )

            expected_net = float(c5_price_batch) * float(
                config.profit_trade_c5_current_sale_net_factor
            )
            balance_discount = float(
                safe_float(order["balance_discount"])
                or config.profit_trade_balance_discount
            )
            expected_profit = expected_net - float(paid_price) * balance_discount
            expected_roi = expected_net / float(paid_price) - balance_discount
            evidence = _read_note(fill["evidence_json"])
            receipt = (
                evidence.get("receipt")
                if isinstance(evidence.get("receipt"), dict)
                else {}
            )
            a_asset_id = (
                _inventory_item_key(sell_item)
                if sell_item is not None
                else None
            )
            a_steam_id = (
                str(sell_item.get("steamId") or "").strip() or None
                if sell_item is not None
                else None
            )
            note = _build_note(
                {
                    "source": "profit_trade_long_buy_fill",
                    "name": str(watch.get("name_cn") or market_hash_name),
                    "assetId": a_asset_id,
                    "steamId": a_steam_id or str(order["steam_id"] or "").strip() or None,
                    "token": (
                        str(sell_item.get("token") or "").strip() or None
                        if sell_item is not None
                        else None
                    ),
                    "styleToken": (
                        str(
                            sell_item.get("styleToken")
                            or sell_item.get("style_token")
                            or ""
                        ).strip()
                        or None
                        if sell_item is not None
                        else None
                    ),
                    "steamBuyMethod": "createbuyorder_long_term",
                    "steamBuySucceededAt": (
                        str(fill["purchased_at"] or "").strip()
                        or utc_now_iso()
                    ),
                    "purchaseRequestSent": True,
                    "steamBuyOrderId": str(order["buy_order_id"] or "").strip() or None,
                    "steamPurchaseReceipt": receipt or evidence,
                    "steamAccountId": str(order["steam_account_id"] or "").strip(),
                    "steamBuyPrice": round(float(paid_price), 2),
                    "steamBuyMaximumPrice": cents_to_price(order["bid_price_cents"]),
                    "steamBuyActualPrice": round(float(paid_price), 2),
                    "c5ListingPrice": round(float(c5_price_batch), 2),
                    "c5ExpectedNetPrice": round(expected_net, 2),
                    "c5Pricing": {
                        "source": "c5_price_batch",
                        "effectiveReferencePrice": round(float(c5_price_batch), 2),
                        "sourceLongBuyOrderId": int(order["id"]),
                    },
                    "expectedProfit": round(expected_profit, 2),
                    "expectedRoi": normalize_roi_four_decimals(expected_roi),
                    "minRoiAtOpen": float(
                        safe_float(order["standard_roi"])
                        or config.profit_trade_min_roi
                    ),
                    "minRoiAtOpenSource": "long_buy_order_snapshot",
                    "initialListingDiscountPctAtOpen": float(
                        config.profit_trade_initial_listing_discount_pct
                    ),
                    "repriceDiscountPctAtOpen": float(
                        config.profit_trade_reprice_discount_pct
                    ),
                    "staleMinRoiFactorAtOpen": float(
                        config.profit_trade_stale_min_roi_factor
                    ),
                    "pricingPolicyVersion": 2,
                    "longBuyOrderId": int(order["id"]),
                    "longBuyFillId": fill_id,
                    "longBuyCreateRequestId": order["create_request_id"],
                    "longBuyWorstCaseRoiAtOpen": order["worst_case_roi"],
                    "longBuyStandardSafePrice": cents_to_price(
                        order["standard_safe_price_cents"]
                    ),
                    "longBuyAggressiveSafePrice": cents_to_price(
                        order["aggressive_safe_price_cents"]
                    ),
                    "auditStatus": (
                        "manual_required" if manual_reasons else "passed"
                    ),
                    "auditReason": "; ".join(manual_reasons) if manual_reasons else "official_fill",
                }
            )
            manual_reason = "; ".join(manual_reasons) if manual_reasons else None
            try:
                trade_id = db.create_profit_trade_from_long_buy_fill(
                    fill_id=fill_id,
                    trade_no=_trade_no(),
                    market_hash_name=market_hash_name,
                    a_asset_id=a_asset_id,
                    a_steam_id=a_steam_id,
                    b_asset_id=str(fill["new_asset_id"] or "").strip()
                    or str(fill["asset_id"] or "").strip()
                    or None,
                    steam_listing_id=str(fill["listing_id"] or "").strip() or None,
                    steam_buy_price=float(paid_price),
                    steam_balance_discount=balance_discount,
                    steam_real_cost=float(paid_price) * balance_discount,
                    c5_listing_price=float(c5_price_batch),
                    c5_expected_net_price=expected_net,
                    expected_profit=expected_profit,
                    expected_roi=expected_roi,
                    note=note,
                    manual_reason=manual_reason,
                )
            except Exception as exc:
                result.errors.append(f"long-buy fill {fill_id}: {exc}")
                continue
            result.imported_trade_ids.append(trade_id)
            if market_hash_name not in result.processed_market_hash_names:
                result.processed_market_hash_names.append(market_hash_name)
            if manual_reason:
                result.manual_trade_ids.append(trade_id)
            else:
                result.imported_steam_bought_trade_ids.append(trade_id)
            _emit_profit_trade_long_buy_log(
                operation="fill_imported",
                message=(
                    "Steam long-term buy fill requires manual handling"
                    if manual_reason
                    else "Steam long-term buy fill entered the existing C5 listing chain"
                ),
                market_hash_name=market_hash_name,
                level="WARN" if manual_reason else "INFO",
                safe_context={
                    "long_buy_order_id": int(order["id"]),
                    "fill_id": fill_id,
                    "profit_trade_id": trade_id,
                    "paid_total": round(float(paid_price), 2),
                    "manual_reason": manual_reason,
                },
            )
    finally:
        db.close()
    return result


def _profit_trade_long_buy_live_account_selection(
    settings: Settings,
    config: StrategyConfig,
    *,
    required_balance: float,
    preferred_steam_id: str | None,
    steam_client: Any | None,
) -> SteamBuyAccountSelection:
    preferred = str(preferred_steam_id or "").strip()
    if steam_client is not None:
        client_steam_id = str(getattr(steam_client, "steam_id64", "") or "").strip()
        if not preferred or not client_steam_id or preferred == client_steam_id:
            wallet = _execution_wallet_balance(steam_client)
            currency_id = safe_int(
                wallet.get("currency_id")
                if wallet.get("currency_id") is not None
                else wallet.get("currencyId")
            )
            currency_code = str(wallet.get("currency") or "").strip().upper()
            if (currency_id is not None and currency_id != 23) or (
                currency_id is None and currency_code != "CNY"
            ):
                raise RuntimeError("Steam wallet currency is not CNY")
            balance = safe_float(wallet.get("balance"))
            if balance is None:
                raise RuntimeError("Steam wallet response is missing balance")
            store = AccountStore(PROJECT_ROOT / "config")
            account = next(
                (
                    item
                    for item in store.list_accounts()
                    if (
                        str(item.id or "").strip()
                        == str(getattr(steam_client, "account_id", "") or "").strip()
                    )
                    or (
                        client_steam_id
                        and str(item.steam_id64 or "").strip() == client_steam_id
                    )
                ),
                None,
            )
            reserved = _account_reserved_balance(
                account,
                config.profit_trade_account_reserved_balances,
                account_id=str(getattr(steam_client, "account_id", "") or ""),
                steam_id64=client_steam_id,
            )
            spendable = max(0.0, float(balance) - reserved)
            _persist_shared_steam_wallet(
                settings,
                steam_client,
                wallet,
                account=account,
            )
            if spendable + 1e-9 < required_balance:
                raise RuntimeError(
                    "Steam account has insufficient spendable wallet balance: "
                    f"required CNY {required_balance:.2f}, spendable CNY {spendable:.2f}"
                )
            return SteamBuyAccountSelection(
                account=account,
                client=steam_client,
                wallet_balance=float(balance),
                reserved_balance=reserved,
                spendable_balance=spendable,
                wallet=wallet,
                wallet_is_live=True,
            )
    return _select_live_steam_buy_account(
        settings,
        required_balance=required_balance,
        preferred_steam_id=preferred or None,
        account_reserved_balances=config.profit_trade_account_reserved_balances,
        telemetry_context={"market_hash_name": "profit_trade_long_buy"},
    )


def _validate_profit_trade_long_buy_capacity(
    settings: Settings,
    config: StrategyConfig,
    *,
    selection: SteamBuyAccountSelection,
    market_hash_name: str,
    bid_price_cents: int,
    quantity: int,
) -> dict[str, Any]:
    wallet = (
        selection.wallet
        if selection.wallet_is_live and isinstance(selection.wallet, dict)
        else _execution_wallet_balance(selection.client)
    )
    currency_id = safe_int(
        wallet.get("currency_id")
        if wallet.get("currency_id") is not None
        else wallet.get("currencyId")
    )
    currency_code = str(wallet.get("currency") or "").strip().upper()
    if (currency_id is not None and currency_id != 23) or (
        currency_id is None and currency_code != "CNY"
    ):
        raise RuntimeError("Steam wallet currency is not CNY (currencyId=23 required)")
    wallet_balance = safe_float(wallet.get("balance"))
    if wallet_balance is None:
        raise RuntimeError("Steam wallet balance is unavailable")
    account = selection.account
    reserved = _account_reserved_balance(
        account,
        config.profit_trade_account_reserved_balances,
        account_id=str(getattr(selection.client, "account_id", "") or ""),
        steam_id64=str(getattr(selection.client, "steam_id64", "") or ""),
    )
    spendable = max(0.0, float(wallet_balance) - reserved)
    unit_price = cents_to_price(bid_price_cents)
    if unit_price is None or unit_price > spendable + 1e-9:
        raise RuntimeError(
            "single long-term buy-order fill can exceed the current spendable "
            f"wallet: bid CNY {float(unit_price or 0):.2f}, "
            f"spendable CNY {spendable:.2f}"
        )

    listings_snapshot = _call_profit_trade_my_listings(selection.client)
    if not listings_snapshot.complete:
        raise RuntimeError(
            "Steam active buy-order snapshot is incomplete; refusing to create "
            "a long-term buy order"
            f"{f': {listings_snapshot.error}' if listings_snapshot.error else ''}"
        )
    active_rows = listings_snapshot.buy_orders
    same_item_rows = [
        row
        for row in active_rows
        if _steam_buy_order_market_hash_name(row)
        == str(market_hash_name or "").strip()
        and (_steam_buy_order_remaining_quantity(row) or 0) > 0
    ]
    if same_item_rows:
        raise RuntimeError(
            "Steam account already has an active buy order for this item; "
            "the program will not merge with or overwrite an untracked order"
        )
    nominal_total_cents, nominal_error = _steam_buy_order_nominal_total_cents(
        active_rows
    )
    if nominal_total_cents is None:
        raise RuntimeError(nominal_error or "Steam active buy-order total is unreadable")
    projected_total_cents = nominal_total_cents + int(bid_price_cents) * int(quantity)
    tenfold_limit_cents = int(
        Decimal(str(spendable * 10.0))
        .scaleb(2)
        .quantize(Decimal("1"), rounding=ROUND_DOWN)
    )
    if projected_total_cents > tenfold_limit_cents:
        raise RuntimeError(
            "Steam active buy-order nominal total would exceed 10x current "
            f"spendable wallet: projected CNY {projected_total_cents / 100:.2f}, "
            f"limit CNY {tenfold_limit_cents / 100:.2f}"
        )
    _persist_shared_steam_wallet(
        settings,
        selection.client,
        wallet,
        account=account,
    )
    return {
        "wallet": wallet,
        "walletBalance": float(wallet_balance),
        "reservedBalance": reserved,
        "spendableBalance": spendable,
        "activeBuyOrdersComplete": True,
        "activeBuyOrdersPageCount": listings_snapshot.pages_scanned,
        "activeBuyOrdersOfficialCount": listings_snapshot.official_buy_order_count,
        "activeBuyOrders": [
            _compact_steam_buy_order(row) for row in active_rows
        ],
        "activeNominalTotalCents": nominal_total_cents,
        "projectedNominalTotalCents": projected_total_cents,
        "tenfoldLimitCents": tenfold_limit_cents,
    }


def _refresh_profit_trade_long_buy_proposal(
    settings: Settings,
    config: StrategyConfig,
    *,
    market_hash_name: str,
    quantity: int,
    steam_client: Any,
    c5_client: Any,
    own_price_cents: list[int] | tuple[int, ...] = (),
) -> dict[str, Any]:
    c5_reference = _fetch_c5_price_batch_reference(
        c5_client,
        settings,
        market_hash_name=market_hash_name,
    )
    orderbook_payload = steam_client.order_book(
        app_id=settings.app_id,
        market_hash_name=market_hash_name,
    )
    orderbook_snapshot = build_orderbook_snapshot(
        orderbook_payload if isinstance(orderbook_payload, dict) else {},
        expected_currency=23,
    )
    proposal = build_long_buy_proposal(
        config,
        c5_price_batch=c5_reference.get("effectiveReferencePrice"),
        orderbook_snapshot=orderbook_snapshot,
        quantity=quantity,
        own_price_cents=own_price_cents,
    )
    if proposal is None:
        raise RuntimeError("fresh C5 price_batch/orderbook cannot produce a safe long-buy price")
    proposal.update(
        {
            "steamOrderbook": orderbook_snapshot,
            "c5Pricing": c5_reference,
            "refreshedAt": utc_now_iso(),
        }
    )
    return proposal


def _create_profit_trade_long_buy_order_remote(
    db: Database,
    settings: Settings,
    config: StrategyConfig,
    *,
    market_hash_name: str,
    proposal: dict[str, Any],
    quantity: int,
    selection: SteamBuyAccountSelection,
    source_scan_id: str | None,
    replaces_order_id: int | None = None,
    previous_bid_price_cents: int | None = None,
    new_action_guard: Callable[[], bool] | None = None,
) -> int:
    if not config.profit_trade_allow_real_execution:
        raise RuntimeError("profitTrade.allowRealExecution is false")
    if not config.profit_trade_long_buy_allow_real_execution:
        raise RuntimeError("profitTrade.longBuyAllowRealExecution is false")
    if not _profit_trade_new_action_allowed(new_action_guard):
        raise RuntimeError("Profit Trade runtime no longer admits new actions")
    if not _profit_trade_long_buy_remote_action_allowed(
        settings,
        new_action_guard,
    ):
        raise RuntimeError(
            "latest Profit Trade long-buy execution switches no longer allow "
            "remote createbuyorder"
        )
    target_cents = safe_int(proposal.get("targetPriceCents"))
    if target_cents is None or target_cents <= 0:
        raise RuntimeError("long-buy proposal has no usable target price")
    quantity = max(1, int(quantity))
    capacity = _validate_profit_trade_long_buy_capacity(
        settings,
        config,
        selection=selection,
        market_hash_name=market_hash_name,
        bid_price_cents=target_cents,
        quantity=quantity,
    )
    account_id = str(
        selection.account.id
        if selection.account is not None
        else getattr(selection.client, "account_id", "")
        or ""
    ).strip()
    steam_id = str(
        selection.account.steam_id64
        if selection.account is not None
        else getattr(selection.client, "steam_id64", "")
        or ""
    ).strip()
    if not account_id:
        raise RuntimeError("selected Steam account has no stable account id")
    create_request_id = f"PTLB-{uuid.uuid4().hex}"
    previous_expires_at = (
        (
            datetime.now(timezone.utc)
            + timedelta(seconds=LONG_BUY_PREVIOUS_PRICE_EXCLUSION_SECONDS)
        )
        .replace(microsecond=0)
        .isoformat()
        if previous_bid_price_cents
        else None
    )
    note = {
        "source": "profit_trade_long_buy",
        "createRequestId": create_request_id,
        "proposal": dict(proposal),
        "capacityAudit": capacity,
        "realRequestAllowedAtCreate": True,
        "requestSent": False,
        "replacesOrderId": replaces_order_id,
    }
    order_id = db.create_profit_trade_long_buy_order(
        market_hash_name=market_hash_name,
        steam_account_id=account_id,
        steam_id=steam_id or None,
        create_request_id=create_request_id,
        bid_price_cents=target_cents,
        quantity=quantity,
        c5_price_batch=safe_float(proposal.get("c5PriceBatch")),
        c5_expected_net_price=safe_float(proposal.get("c5ExpectedNetPrice")),
        balance_discount=safe_float(proposal.get("balanceDiscount")),
        standard_roi=safe_float(proposal.get("standardRoi")),
        aggressive_roi=safe_float(proposal.get("aggressiveRoi")),
        standard_safe_price_cents=safe_int(
            proposal.get("standardSafePriceCents")
        ),
        aggressive_safe_price_cents=safe_int(
            proposal.get("aggressiveSafePriceCents")
        ),
        competitor_buy_price_cents=safe_int(
            proposal.get("competitorBuyPriceCents")
        ),
        competitor_buy_status=str(
            proposal.get("competitorBuyStatus") or ""
        )
        or None,
        worst_case_roi=safe_float(proposal.get("worstCaseRoi")),
        source_scan_id=source_scan_id,
        wallet_before=safe_float(capacity.get("walletBalance")),
        previous_bid_price_cents=previous_bid_price_cents,
        previous_price_expires_at=previous_expires_at,
        replaces_order_id=replaces_order_id,
        note=note,
    )
    _emit_profit_trade_long_buy_log(
        operation="create_intent_recorded",
        message="Profit Trade recorded a long-term buy intent before Steam HTTP",
        market_hash_name=market_hash_name,
        safe_context={
            "long_buy_order_id": order_id,
            "account_id": account_id,
            "target_price": cents_to_price(target_cents),
            "quantity": quantity,
            "replaces_order_id": replaces_order_id,
        },
    )
    order = db.get_profit_trade_long_buy_order(order_id)
    assert order is not None
    note = _profit_trade_long_buy_note(order)
    note.update({"requestSent": True, "requestSentAt": utc_now_iso()})
    db.update_profit_trade_long_buy_order(
        order_id,
        event_type="create_request_started",
        reason="local intent exists before Steam createbuyorder",
        note_json=note,
    )
    create_buy_order = selection.client.create_buy_order
    create_kwargs: dict[str, Any] = {
        "app_id": settings.app_id,
        "market_hash_name": market_hash_name,
        "price_total": target_cents,
        "quantity": quantity,
        "currency": 23,
        "country": "CN",
    }
    if _callable_accepts_keyword_argument(
        create_buy_order,
        "return_uncertain_after_confirmation",
    ):
        create_kwargs["return_uncertain_after_confirmation"] = True
    if _callable_accepts_keyword_argument(create_buy_order, "execution_guard"):
        create_kwargs["execution_guard"] = (
            lambda: _profit_trade_long_buy_remote_action_allowed(
                settings,
                new_action_guard,
            )
        )

    try:
        payload = create_buy_order(**create_kwargs)
    except SteamRequestGuardRejected as exc:
        order = db.get_profit_trade_long_buy_order(order_id) or order
        note = _profit_trade_long_buy_note(order)
        note.update(
            {
                "requestSent": False,
                "requestSuppressedAt": utc_now_iso(),
                "requestSuppressedStage": "scheduler_before_createbuyorder_http",
                "requestSuppressedReason": str(exc),
            }
        )
        db.update_profit_trade_long_buy_order(
            order_id,
            event_type="create_request_suppressed",
            reason="latest execution switches rejected createbuyorder before HTTP",
            state="failed",
            terminal_reason=(
                "latest execution switches rejected createbuyorder before HTTP"
            ),
            completed_at=utc_now_iso(),
            note_json=note,
        )
        return order_id
    except Exception as exc:
        order = db.get_profit_trade_long_buy_order(order_id) or order
        _mark_profit_trade_long_buy_uncertain(
            db,
            settings,
            order,
            reason=f"Steam createbuyorder outcome is uncertain: {exc}",
        )
        return order_id

    payload = payload if isinstance(payload, dict) else {}
    buy_order_id = str(
        payload.get("buy_orderid")
        or payload.get("buy_order_id")
        or payload.get("buyOrderId")
        or ""
    ).strip()
    uncertain_payload = bool(payload.get("_outcome_uncertain_after_confirmation"))
    if payload.get("success") not in (1, True) or not buy_order_id or uncertain_payload:
        order = db.get_profit_trade_long_buy_order(order_id) or order
        note = _profit_trade_long_buy_note(order)
        note["createResponse"] = sanitize_public_payload(payload)
        order = db.update_profit_trade_long_buy_order(
            order_id,
            event_type="create_response_uncertain",
            reason="Steam createbuyorder did not return a definitive order id",
            note_json=note,
        )
        _mark_profit_trade_long_buy_uncertain(
            db,
            settings,
            order,
            reason="Steam createbuyorder did not return a definitive order id",
        )
        return order_id

    note = _profit_trade_long_buy_note(
        db.get_profit_trade_long_buy_order(order_id) or order
    )
    note.update(
        {
            "createResponse": sanitize_public_payload(payload),
            "createdRemotelyAt": utc_now_iso(),
        }
    )
    db.update_profit_trade_long_buy_order(
        order_id,
        event_type="remote_order_created",
        reason="Steam createbuyorder returned success and a buy_orderid",
        state="active",
        buy_order_id=buy_order_id,
        last_checked_at=utc_now_iso(),
        note_json=note,
    )
    _emit_profit_trade_long_buy_log(
        operation="remote_order_created",
        message="Steam confirmed the Profit Trade long-term buy order",
        market_hash_name=market_hash_name,
        provider="steam",
        safe_context={
            "long_buy_order_id": order_id,
            "buy_order_id": buy_order_id,
            "account_id": account_id,
            "target_price": cents_to_price(target_cents),
            "quantity": quantity,
            "replaces_order_id": replaces_order_id,
        },
    )
    return order_id


def _safe_cancel_profit_trade_long_buy_order(
    db: Database,
    settings: Settings,
    config: StrategyConfig,
    *,
    order: Any,
    client: Any,
    reason: str,
    new_action_guard: Callable[[], bool] | None = None,
) -> tuple[str, list[int]]:
    current_state = str(order["state"] or "")
    if current_state == "filled":
        return "purchased", []
    if current_state in {"cancelled", "auto_cancelled", "failed"}:
        return "cancelled", []
    if current_state not in LONG_BUY_MUTABLE_STATES:
        return "uncertain", []
    if not config.profit_trade_allow_real_execution:
        return "blocked", []
    if not config.profit_trade_long_buy_allow_real_execution:
        return "blocked", []
    if not _profit_trade_long_buy_remote_action_allowed(
        settings,
        new_action_guard,
    ):
        return "blocked", []
    buy_order_id = str(order["buy_order_id"] or "").strip()
    if not buy_order_id:
        return "uncertain", []

    before_fill_ids = {
        int(row["id"])
        for row in db.list_profit_trade_long_buy_fills(
            long_buy_order_id=int(order["id"]),
            limit=max(10, int(order["quantity"] or 1) + 5),
        )
    }
    note = _profit_trade_long_buy_note(order)
    note.update(
        {
            "cancelRequestedAt": utc_now_iso(),
            "cancelReason": reason,
            "cancelRequestSent": False,
        }
    )
    order = db.update_profit_trade_long_buy_order(
        int(order["id"]),
        event_type="cancel_intent_recorded",
        reason=reason,
        state="cancel_pending",
        note_json=note,
    )
    _emit_profit_trade_long_buy_log(
        operation="cancel_intent_recorded",
        message="Profit Trade is safely cancelling a managed long-term buy order",
        market_hash_name=str(order["market_hash_name"] or ""),
        provider="steam",
        safe_context={
            "long_buy_order_id": int(order["id"]),
            "buy_order_id": buy_order_id,
            "reason": reason,
        },
    )
    cancel_error: str | None = None
    cancel_buy_order = client.cancel_buy_order
    cancel_kwargs: dict[str, Any] = {"buy_order_id": buy_order_id}
    if _callable_accepts_keyword_argument(cancel_buy_order, "execution_guard"):
        cancel_kwargs["execution_guard"] = (
            lambda: _profit_trade_long_buy_remote_action_allowed(
                settings,
                new_action_guard,
            )
        )
    try:
        payload = cancel_buy_order(**cancel_kwargs)
        note = _profit_trade_long_buy_note(order)
        note.update(
            {
                "cancelRequestSent": True,
                "cancelResponse": sanitize_public_payload(
                    payload if isinstance(payload, dict) else {}
                ),
            }
        )
        order = db.update_profit_trade_long_buy_order(
            int(order["id"]),
            event_type="cancel_request_returned",
            reason="Steam cancelbuyorder returned",
            note_json=note,
        )
    except SteamRequestGuardRejected as exc:
        note = _profit_trade_long_buy_note(order)
        note.update(
            {
                "cancelRequestSent": False,
                "cancelRequestSuppressedAt": utc_now_iso(),
                "cancelRequestSuppressedStage": (
                    "scheduler_before_cancelbuyorder_http"
                ),
                "cancelRequestSuppressedReason": str(exc),
            }
        )
        db.update_profit_trade_long_buy_order(
            int(order["id"]),
            event_type="cancel_request_suppressed",
            reason="latest execution switches rejected cancelbuyorder before HTTP",
            state=current_state,
            note_json=note,
        )
        _emit_profit_trade_long_buy_log(
            operation="cancel_request_suppressed",
            message="Long-term buy cancellation was stopped before Steam HTTP",
            market_hash_name=str(order["market_hash_name"] or ""),
            provider="steam",
            level="WARN",
            safe_context={
                "long_buy_order_id": int(order["id"]),
                "buy_order_id": buy_order_id,
                "reason": str(exc),
            },
        )
        return "blocked", []
    except Exception as exc:
        cancel_error = str(exc)
        note = _profit_trade_long_buy_note(order)
        note.update(
            {
                "cancelRequestSent": True,
                "cancelRequestError": cancel_error,
            }
        )
        order = db.update_profit_trade_long_buy_order(
            int(order["id"]),
            event_type="cancel_request_error",
            reason=cancel_error,
            note_json=note,
        )

    new_fill_ids: list[int] = []
    for attempt in range(max(1, int(STEAM_BUY_CANCEL_VERIFY_ATTEMPTS))):
        reconciliation = _reconcile_profit_trade_long_buy_account(
            db,
            settings,
            orders=[db.get_profit_trade_long_buy_order(int(order["id"])) or order],
            client=client,
        )
        new_fill_ids.extend(reconciliation.new_fill_ids)
        latest = db.get_profit_trade_long_buy_order(int(order["id"]))
        if latest is None:
            return "uncertain", sorted(set(new_fill_ids))
        after_fill_ids = {
            int(row["id"])
            for row in db.list_profit_trade_long_buy_fills(
                long_buy_order_id=int(order["id"]),
                limit=max(10, int(order["quantity"] or 1) + 5),
            )
        }
        if after_fill_ids - before_fill_ids or str(latest["state"] or "") == "filled":
            _emit_profit_trade_long_buy_log(
                operation="cancel_race_fill",
                message="Steam purchase filled while the long-term buy was being cancelled",
                market_hash_name=str(latest["market_hash_name"] or ""),
                provider="steam",
                level="WARN",
                safe_context={
                    "long_buy_order_id": int(latest["id"]),
                    "buy_order_id": buy_order_id,
                    "new_fill_ids": sorted(set(new_fill_ids)),
                },
            )
            return "purchased", sorted(set(new_fill_ids))
        if str(latest["state"] or "") in {"cancelled", "auto_cancelled"}:
            _emit_profit_trade_long_buy_log(
                operation="cancel_confirmed",
                message="Steam confirmed the managed long-term buy is absent without a new fill",
                market_hash_name=str(latest["market_hash_name"] or ""),
                provider="steam",
                safe_context={
                    "long_buy_order_id": int(latest["id"]),
                    "buy_order_id": buy_order_id,
                    "reason": reason,
                },
            )
            return "cancelled", sorted(set(new_fill_ids))
        if attempt + 1 < max(1, int(STEAM_BUY_CANCEL_VERIFY_ATTEMPTS)):
            time.sleep(max(0.0, float(STEAM_BUY_CANCEL_VERIFY_DELAY_SECONDS)))

    latest = db.get_profit_trade_long_buy_order(int(order["id"])) or order
    uncertain_reason = (
        "Steam buy order did not reach a proven terminal state after cancel"
        + (f": {cancel_error}" if cancel_error else "")
    )
    _mark_profit_trade_long_buy_uncertain(
        db,
        settings,
        latest,
        reason=uncertain_reason,
    )
    return "uncertain", sorted(set(new_fill_ids))


def _prepare_profit_trade_long_buy_for_direct_purchase(
    db: Database,
    settings: Settings,
    config: StrategyConfig,
    *,
    market_hash_name: str,
    steam_client: Any | None,
    new_action_guard: Callable[[], bool] | None,
    orderbook_crossed: bool = False,
) -> dict[str, Any]:
    """Resolve a managed long buy immediately before a real direct purchase.

    The caller must invoke this only after the existing direct-purchase
    protection, ROI, C5-risk, wallet and daily-budget checks have passed.  This
    placement preserves the old order's time priority whenever the direct
    purchase cannot actually proceed.
    """

    order = db.get_live_profit_trade_long_buy_order_for_market(
        market_hash_name
    )
    if order is None:
        return {
            "ok": True,
            "outcome": "not_present",
            "fillIds": [],
            "orderId": None,
            "buyOrderId": None,
        }

    if orderbook_crossed:
        return {
            "ok": False,
            "outcome": "blocked",
            "fillIds": [],
            "orderId": int(order["id"]),
            "buyOrderId": str(order["buy_order_id"] or "").strip() or None,
            "reason": (
                "Steam orderbook is crossed while a managed long-term buy "
                "remains unfilled; preserve the old buy order and do not "
                "enter the direct-purchase path"
            ),
        }

    order_id = int(order["id"])
    buy_order_id = str(order["buy_order_id"] or "").strip() or None
    state = str(order["state"] or "")
    if (
        not config.profit_trade_long_buy_enabled
        or not config.profit_trade_allow_real_execution
        or not config.profit_trade_long_buy_allow_real_execution
    ):
        return {
            "ok": False,
            "outcome": "blocked",
            "fillIds": [],
            "orderId": order_id,
            "buyOrderId": buy_order_id,
            "reason": (
                "managed long-term buy order remains active while its real "
                "cancel switch is disabled"
            ),
        }
    if state not in LONG_BUY_MUTABLE_STATES:
        return {
            "ok": False,
            "outcome": "uncertain",
            "fillIds": [],
            "orderId": order_id,
            "buyOrderId": buy_order_id,
            "reason": (
                f"managed long-term buy order is {state}; its terminal state "
                "is not safe to prove"
            ),
        }

    try:
        account_client = _profit_trade_long_buy_client_for_account(
            settings,
            steam_account_id=str(order["steam_account_id"] or ""),
            steam_id=str(order["steam_id"] or "").strip() or None,
            steam_client=steam_client,
        )
        outcome, fill_ids = _safe_cancel_profit_trade_long_buy_order(
            db,
            settings,
            config,
            order=order,
            client=account_client,
            reason=(
                "current seller path passed the complete direct-purchase "
                "preflight immediately before Steam purchase"
            ),
            new_action_guard=new_action_guard,
        )
    except Exception as exc:
        return {
            "ok": False,
            "outcome": "uncertain",
            "fillIds": [],
            "orderId": order_id,
            "buyOrderId": buy_order_id,
            "reason": f"managed long-term buy cancellation failed: {exc}",
        }

    unique_fill_ids = sorted(set(int(value) for value in fill_ids))
    if outcome == "cancelled" and not unique_fill_ids:
        return {
            "ok": True,
            "outcome": "cancelled",
            "fillIds": [],
            "orderId": order_id,
            "buyOrderId": buy_order_id,
        }
    if outcome == "purchased" or unique_fill_ids:
        reason = (
            "managed long-term buy filled during cancellation; direct purchase "
            "must stop for this item"
        )
    elif outcome == "blocked":
        reason = (
            "managed long-term buy cancellation was blocked before Steam HTTP"
        )
    else:
        reason = (
            "managed long-term buy terminal state is not proven after "
            "cancellation"
        )
    return {
        "ok": False,
        "outcome": outcome,
        "fillIds": unique_fill_ids,
        "orderId": order_id,
        "buyOrderId": buy_order_id,
        "reason": reason,
    }


def _profit_trade_long_buy_proposal_from_watch(
    watch: dict[str, Any] | None,
) -> dict[str, Any] | None:
    raw = (
        watch.get("raw")
        if isinstance(watch, dict) and isinstance(watch.get("raw"), dict)
        else {}
    )
    proposal = raw.get("longBuyProposal")
    return dict(proposal) if isinstance(proposal, dict) else None


def _profit_trade_long_buy_current_roi(
    order: Any,
    proposal: dict[str, Any],
) -> float | None:
    expected_net = safe_float(proposal.get("c5ExpectedNetPrice"))
    bid_price = cents_to_price(order["bid_price_cents"])
    balance_discount = (
        safe_float(order["balance_discount"])
        or safe_float(proposal.get("balanceDiscount"))
    )
    if (
        expected_net is None
        or expected_net <= 0
        or bid_price is None
        or bid_price <= 0
        or balance_discount is None
    ):
        return None
    return normalize_roi_four_decimals(
        expected_net / bid_price - float(balance_discount)
    )


def _run_profit_trade_long_buy_cycle(
    settings: Settings,
    config: StrategyConfig,
    *,
    scanned: ProfitTradeScanReport,
    steam_client: Any | None,
    c5_client: Any | None,
    new_action_guard: Callable[[], bool] | None,
    skip_market_hash_names: set[str] | None = None,
) -> ProfitTradeLongBuyCycleResult:
    result = ProfitTradeLongBuyCycleResult()
    skipped_markets = {
        str(value or "").strip()
        for value in set(skip_market_hash_names or set())
        if str(value or "").strip()
    }
    watch_by_market = _profit_trade_long_buy_watch_by_market(scanned)
    inventory_items = list(scanned.inventory_items)
    direct_opportunities = {
        opportunity.market_hash_name: opportunity
        for opportunity in scanned.opportunities
        if opportunity.audit_status != "manual_required"
    }
    real_long_buy_actions = bool(
        config.profit_trade_long_buy_enabled
        and config.profit_trade_allow_real_execution
        and config.profit_trade_long_buy_allow_real_execution
    )
    if c5_client is None and real_long_buy_actions:
        c5_client = _build_profit_trade_c5_client(
            settings,
            run_id=f"PTLB-{uuid.uuid4().hex[:12]}",
        )

    db = Database(settings.db_path)
    try:
        db.initialize()
        # First lower only orders whose latest C5 net would breach the agreed
        # aggressive ROI floor. Safe old prices retain their time priority.
        mutable_orders = db.list_profit_trade_long_buy_orders(
            states=LONG_BUY_MUTABLE_STATES,
            limit=1000,
        )
        for original in mutable_orders:
            market_hash_name = str(original["market_hash_name"] or "").strip()
            if market_hash_name in skipped_markets:
                continue
            if market_hash_name in direct_opportunities:
                continue
            watch = watch_by_market.get(market_hash_name)
            proposal = _profit_trade_long_buy_proposal_from_watch(watch)
            if proposal is None:
                continue
            raw = (
                watch.get("raw")
                if isinstance(watch, dict) and isinstance(watch.get("raw"), dict)
                else {}
            )
            orderbook = (
                raw.get("steamOrderbook")
                if isinstance(raw.get("steamOrderbook"), dict)
                else {}
            )
            # Confirmed special-case policy: when the aggregated book appears
            # crossed but the normal seller path is not executable, do not
            # touch an existing order.
            if orderbook.get("crossed") is True:
                continue
            current_roi = _profit_trade_long_buy_current_roi(original, proposal)
            aggressive_floor = normalize_roi_four_decimals(
                safe_float(original["aggressive_roi"])
                if safe_float(original["aggressive_roi"]) is not None
                else safe_float(proposal.get("aggressiveRoi"))
            )
            if (
                current_roi is None
                or aggressive_floor is None
                or current_roi >= aggressive_floor
            ):
                continue
            if not real_long_buy_actions:
                continue
            old_price_cents = int(original["bid_price_cents"])
            old_remaining = max(0, int(original["remaining_quantity"] or 0))
            if old_remaining <= 0:
                continue
            try:
                account_client = _profit_trade_long_buy_client_for_account(
                    settings,
                    steam_account_id=str(original["steam_account_id"] or ""),
                    steam_id=str(original["steam_id"] or "").strip() or None,
                    steam_client=steam_client,
                )
                cancel_outcome, new_fill_ids = _safe_cancel_profit_trade_long_buy_order(
                    db,
                    settings,
                    config,
                    order=original,
                    client=account_client,
                    reason=(
                        f"latest worst ROI {current_roi:.4f} is below "
                        f"aggressive floor {aggressive_floor:.4f}"
                    ),
                    new_action_guard=new_action_guard,
                )
                if new_fill_ids:
                    skipped_markets.add(market_hash_name)
                    if market_hash_name not in result.fill_market_hash_names:
                        result.fill_market_hash_names.append(market_hash_name)
                    result.errors.append(
                        f"long-buy {original['id']}: fill detected during safe reprice cancellation"
                    )
                if cancel_outcome != "cancelled":
                    continue
                available_assets = _list_executable_sell_assets(
                    db,
                    config,
                    inventory_items,
                    market_hash_name=market_hash_name,
                )
                replacement_quantity = min(old_remaining, len(available_assets))
                if replacement_quantity <= 0:
                    continue
                assert c5_client is not None
                refreshed = _refresh_profit_trade_long_buy_proposal(
                    settings,
                    config,
                    market_hash_name=market_hash_name,
                    quantity=replacement_quantity,
                    steam_client=account_client,
                    c5_client=c5_client,
                    own_price_cents=[old_price_cents],
                )
                pre_target = safe_int(proposal.get("targetPriceCents"))
                fresh_target = safe_int(refreshed.get("targetPriceCents"))
                if pre_target is None or fresh_target is None:
                    raise RuntimeError("replacement proposal has no target price")
                replacement_target = min(
                    pre_target,
                    fresh_target,
                    old_price_cents - 1,
                )
                if replacement_target <= 0 or replacement_target >= old_price_cents:
                    raise RuntimeError("replacement target is not strictly below the old bid")
                refreshed["targetPriceCents"] = replacement_target
                refreshed["targetPrice"] = cents_to_price(replacement_target)
                selection = _profit_trade_long_buy_live_account_selection(
                    settings,
                    config,
                    required_balance=float(
                        cents_to_price(replacement_target) or 0
                    ),
                    preferred_steam_id=str(original["steam_id"] or "").strip()
                    or None,
                    steam_client=account_client,
                )
                replacement_id = _create_profit_trade_long_buy_order_remote(
                    db,
                    settings,
                    config,
                    market_hash_name=market_hash_name,
                    proposal=refreshed,
                    quantity=replacement_quantity,
                    selection=selection,
                    source_scan_id=str(
                        proposal.get("sourceScanId")
                        or original["source_scan_id"]
                        or ""
                    )
                    or None,
                    replaces_order_id=int(original["id"]),
                    previous_bid_price_cents=old_price_cents,
                    new_action_guard=new_action_guard,
                )
                result.created_order_ids.append(replacement_id)
                result.replaced_order_ids.append(int(original["id"]))
            except Exception as exc:
                result.errors.append(
                    f"long-buy reprice {original['id']} ({market_hash_name}): {exc}"
                )

        # Create a bounded batch of new managed orders only for rows whose
        # normal seller path is below the full execution threshold.
        if real_long_buy_actions:
            daily_spent = _profit_trade_daily_steam_spent(db)
            daily_budget = max(0.0, float(config.profit_trade_daily_steam_budget))
            budget_allows_new = daily_budget <= 0 or daily_spent < daily_budget
            max_active = max(
                0,
                int(config.profit_trade_long_buy_max_active_orders),
            )
            per_cycle_limit = max(
                1,
                int(
                    math.ceil(
                        max_active
                        * float(
                            config.profit_trade_long_buy_create_fraction_per_cycle
                        )
                    )
                ),
            ) if max_active > 0 else 0
            available_slots = max(
                0,
                max_active - db.count_live_profit_trade_long_buy_orders(),
            )
            create_limit = min(per_cycle_limit, available_slots)
            candidates: list[
                tuple[float, int, int, int, int, str, dict[str, Any]]
            ] = []
            if budget_allows_new and create_limit > 0:
                for market_hash_name, watch in watch_by_market.items():
                    if market_hash_name in skipped_markets:
                        continue
                    if market_hash_name in direct_opportunities:
                        continue
                    proposal = _profit_trade_long_buy_proposal_from_watch(watch)
                    if proposal is None or not bool(proposal.get("eligible")):
                        continue
                    if db.get_live_profit_trade_long_buy_order_for_market(
                        market_hash_name
                    ) is not None:
                        continue
                    raw_watch = (
                        watch.get("raw")
                        if isinstance(watch.get("raw"), dict)
                        else {}
                    )
                    orderbook = (
                        raw_watch.get("steamOrderbook")
                        if isinstance(raw_watch.get("steamOrderbook"), dict)
                        else {}
                    )
                    # Defense in depth: a stale/crafted watch proposal must not
                    # bypass the scan-level crossed-book safety rule and create
                    # a new Steam long-term buy order.
                    if orderbook.get("crossed") is True:
                        continue
                    candidates.append(
                        (
                            -float(safe_float(proposal.get("competitorBuyRoi")) or -999),
                            -max(0, safe_int(watch.get("c5_recent_sold_count")) or 0),
                            -max(0, safe_int(watch.get("c5_on_sale_count")) or 0),
                            -max(0, safe_int(watch.get("c5_purchase_count")) or 0),
                            -max(
                                0,
                                safe_int(
                                    raw_watch.get("manualExecutableQuantity")
                                )
                                or 0,
                            ),
                            market_hash_name,
                            proposal,
                        )
                    )
            for (
                _,
                _,
                _,
                _,
                _,
                market_hash_name,
                proposal,
            ) in sorted(candidates):
                if len(result.created_order_ids) >= create_limit + len(
                    result.replaced_order_ids
                ):
                    break
                available_assets = _list_executable_sell_assets(
                    db,
                    config,
                    inventory_items,
                    market_hash_name=market_hash_name,
                )
                quantity = min(
                    PROFIT_TRADE_LONG_BUY_BASE_QUANTITY,
                    len(available_assets),
                )
                if quantity <= 0:
                    continue
                target_price = safe_float(proposal.get("targetPrice"))
                if target_price is None or target_price <= 0:
                    continue
                preferred_steam_id = str(
                    available_assets[0].get("steamId") or ""
                ).strip() or None
                try:
                    selection = _profit_trade_long_buy_live_account_selection(
                        settings,
                        config,
                        required_balance=target_price,
                        preferred_steam_id=preferred_steam_id,
                        steam_client=steam_client,
                    )
                    assert c5_client is not None
                    refreshed = _refresh_profit_trade_long_buy_proposal(
                        settings,
                        config,
                        market_hash_name=market_hash_name,
                        quantity=quantity,
                        steam_client=selection.client,
                        c5_client=c5_client,
                    )
                    refreshed_target = safe_float(refreshed.get("targetPrice"))
                    if refreshed_target is None or refreshed_target <= 0:
                        raise RuntimeError("fresh long-buy target is unavailable")
                    if (
                        selection.spendable_balance is None
                        or refreshed_target
                        > float(selection.spendable_balance) + 1e-9
                    ):
                        selection = _profit_trade_long_buy_live_account_selection(
                            settings,
                            config,
                            required_balance=refreshed_target,
                            preferred_steam_id=preferred_steam_id,
                            steam_client=None,
                        )
                    order_id = _create_profit_trade_long_buy_order_remote(
                        db,
                        settings,
                        config,
                        market_hash_name=market_hash_name,
                        proposal=refreshed,
                        quantity=quantity,
                        selection=selection,
                        source_scan_id=str(
                            proposal.get("sourceScanId") or ""
                        )
                        or None,
                        new_action_guard=new_action_guard,
                    )
                    result.created_order_ids.append(order_id)
                except Exception as exc:
                    result.errors.append(
                        f"long-buy create ({market_hash_name}): {exc}"
                    )

        # Do not cancel mutable orders here.  This cycle runs before the real
        # direct-purchase capacity, daily budget and final market checks are
        # known.  Mutable orders are resolved by
        # ``execute_profit_trade_buy`` immediately before its Steam HTTP.
        # Pre-block only states that cannot safely reach that final gate.
        for market_hash_name in direct_opportunities:
            if market_hash_name in skipped_markets:
                result.direct_purchase_block_reasons[market_hash_name] = (
                    "a managed long-term buy fill was confirmed in this cycle"
                )
                continue
            live_order = db.get_live_profit_trade_long_buy_order_for_market(
                market_hash_name
            )
            if live_order is None:
                continue
            watch = watch_by_market.get(market_hash_name)
            raw_watch = (
                watch.get("raw")
                if isinstance(watch, dict) and isinstance(watch.get("raw"), dict)
                else {}
            )
            orderbook = (
                raw_watch.get("steamOrderbook")
                if isinstance(raw_watch.get("steamOrderbook"), dict)
                else {}
            )
            if orderbook.get("crossed") is True:
                result.direct_purchase_block_reasons[market_hash_name] = (
                    "Steam orderbook is crossed while a managed long-term buy "
                    "remains unfilled; preserve the old buy order and do not "
                    "enter the direct-purchase path"
                )
                continue
            if not real_long_buy_actions:
                result.direct_purchase_block_reasons[market_hash_name] = (
                    "managed long-term buy order remains active while "
                    "longBuyAllowRealExecution is false"
                )
                continue
            if str(live_order["state"] or "") not in LONG_BUY_MUTABLE_STATES:
                result.direct_purchase_block_reasons[market_hash_name] = (
                    f"managed long-term buy order is {live_order['state']}; "
                    "terminal state is not safe to prove"
                )
                continue
    finally:
        db.close()
    return result


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
    new_action_guard: Callable[[], bool] | None = None,
) -> ProfitTradeRunReport:
    config = config or load_strategy_config(settings)
    errors: list[str] = []
    bought_trade_ids: list[int] = []
    listed_trade_ids: list[int] = []
    settled_trade_ids: list[int] = []
    skipped_trade_ids: list[int] = []
    scanned: ProfitTradeScanReport | None = None

    def persist_shared_selection_snapshots(
        state_map: dict[str, MarketState],
        _: MarketService,
    ) -> None:
        # Pass no market service here on purpose.  Any selected item that was
        # not part of this inventory scan must use the selection path's own
        # no-relogin Steam client; overlapping items are persisted from
        # ``state_map`` with no second C5 or Steam request.
        refresh_profit_trade_selection_watch(
            settings,
            config=config,
            shared_state_map=state_map,
            force=True,
        )

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

    if config.profit_trade_allow_real_execution:
        try:
            reprice_result = refresh_profit_trade_listings(
                settings,
                config,
                c5_client=c5_client,
            )
            skipped_trade_ids.extend(
                int(value) for value in reprice_result.get("skippedTradeIds", [])
            )
            errors.extend(str(value) for value in reprice_result.get("errors", []))
        except Exception as exc:
            errors.append(f"refresh-listings: {exc}")

        try:
            recover_result = recover_unverified_profit_trade_steam_buys(
                settings,
                config=config,
                steam_client=steam_client,
                remote_audit=True,
            )
            skipped_trade_ids.extend(
                int(value) for value in recover_result.get("skippedTradeIds", [])
            )
            errors.extend(str(value) for value in recover_result.get("errors", []))
        except Exception as exc:
            errors.append(f"recover-buys: {exc}")

    pre_scan_db = Database(settings.db_path)
    try:
        pre_scan_db.initialize()
        pre_scan_db.release_expired_asset_reservations()
        _mark_expired_locked_trades(pre_scan_db)
        _cancel_stale_pre_buy_manual_trades(pre_scan_db)
        _cancel_stale_buying_trades_without_steam_evidence(pre_scan_db)
        if config.profit_trade_allow_real_execution:
            _cancel_recorded_pre_buy_candidates(
                pre_scan_db,
                reason="fresh automatic run superseded recorded pre-buy candidate",
            )
    finally:
        pre_scan_db.close()

    # The complete scan must finish before any newly discovered item can
    # reserve A or enter the purchase path. Long-term buy-order reconciliation
    # depends on the full watch set and has priority over direct purchases.
    scanned = scan_profit_trade_opportunities(
        settings,
        config,
        allow_cached_fallback=allow_cached_fallback,
        cache_max_age_minutes=cache_max_age_minutes,
        limit=max(1, max_buy or 1),
        scan_max_items=scan_max_items,
        record=not config.profit_trade_allow_real_execution,
        lock_asset=False,
        inventory_payload=inventory_payload,
        market_service=market_service,
        c5_client=c5_client,
        on_market_states_ready=persist_shared_selection_snapshots,
    )
    reconcile_result = _reconcile_profit_trade_long_buy_orders(
        settings,
        steam_client=steam_client,
    )
    errors.extend(reconcile_result.errors)
    imported_fills = _process_pending_profit_trade_long_buy_fills(
        settings,
        config,
        scanned=scanned,
    )
    errors.extend(imported_fills.errors)
    filled_markets_this_cycle = {
        *reconcile_result.new_fill_market_hash_names,
        *imported_fills.processed_market_hash_names,
    }

    if not config.profit_trade_allow_real_execution:
        return _record_profit_trade_run(settings, ProfitTradeRunReport(
            generated_at=utc_now_iso(),
            enabled=True,
            allow_real_execution=False,
            scanned=scanned,
            bought_trade_ids=[],
            listed_trade_ids=[],
            settled_trade_ids=settled_trade_ids,
            skipped_trade_ids=[
                *skipped_trade_ids,
                *list(scanned.created_trade_ids),
                *imported_fills.imported_trade_ids,
            ],
            errors=[
                *errors,
                "profitTrade.allowRealExecution is false; recorded candidates only",
            ],
        ))

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
        listings_circuit = _get_profit_trade_listings_circuit(db)
        locked_rows = list(db.list_profit_trades(status="locked", limit=200))
        # User-confirmed multi-item batches own their locked rows.  The
        # persistent manual task must be the only path that advances them;
        # otherwise a normal ten-minute cycle could race a recovered batch.
        locked = [
            int(row["id"])
            for row in locked_rows
            if not str(
                _read_note(row["note"]).get("manualExecutionRequestId") or ""
            ).strip()
        ]
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

    long_buy_cycle = _run_profit_trade_long_buy_cycle(
        settings,
        config,
        scanned=scanned,
        steam_client=steam_client,
        c5_client=c5_client,
        new_action_guard=new_action_guard,
        skip_market_hash_names=filled_markets_this_cycle,
    )
    errors.extend(long_buy_cycle.errors)
    filled_markets_this_cycle.update(long_buy_cycle.fill_market_hash_names)
    post_cancel_fills = _process_pending_profit_trade_long_buy_fills(
        settings,
        config,
        scanned=scanned,
    )
    errors.extend(post_cancel_fills.errors)
    filled_markets_this_cycle.update(
        post_cancel_fills.processed_market_hash_names
    )
    for trade_id in post_cancel_fills.imported_steam_bought_trade_ids:
        try:
            list_result = execute_profit_trade_list_c5(
                settings,
                trade_id,
                config=config,
                c5_client=c5_client,
            )
        except Exception as exc:
            errors.append(f"list-c5 long-buy fill {trade_id}: {exc}")
            skipped_trade_ids.append(trade_id)
            continue
        if list_result.get("ok"):
            listed_trade_ids.append(trade_id)
        else:
            skipped_trade_ids.append(trade_id)

    def advance_direct_cancel_race_fills(
        fill_ids: list[int] | tuple[int, ...],
        *,
        source_trade_id: int,
    ) -> None:
        if not fill_ids:
            return
        processed = _process_pending_profit_trade_long_buy_fills(
            settings,
            config,
            scanned=scanned,
        )
        errors.extend(processed.errors)
        filled_markets_this_cycle.update(
            processed.processed_market_hash_names
        )
        for imported_trade_id in processed.imported_steam_bought_trade_ids:
            try:
                list_result = execute_profit_trade_list_c5(
                    settings,
                    imported_trade_id,
                    config=config,
                    c5_client=c5_client,
                )
            except Exception as exc:
                errors.append(
                    "list-c5 long-buy fill "
                    f"{imported_trade_id} after direct trade "
                    f"{source_trade_id}: {exc}"
                )
                skipped_trade_ids.append(imported_trade_id)
                continue
            if list_result.get("ok"):
                listed_trade_ids.append(imported_trade_id)
            else:
                skipped_trade_ids.append(imported_trade_id)

    buy_capacity = max_buy
    if "remaining_budget" in locals() and float(remaining_budget) <= 0:
        buy_capacity = 0
    if listings_circuit["status"] == "open":
        errors.append(
            "Steam listings cooling down; eligible trades will revalidate markets and use the safe buy-order fallback "
            f"until cooldown ends at {listings_circuit.get('cooldownUntil') or '-'}; normal listings queries then resume automatically"
        )
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
        locked_market_hash_name = (
            str(row_for_budget["market_hash_name"] or "").strip()
            if row_for_budget is not None
            else ""
        )
        if locked_market_hash_name in filled_markets_this_cycle:
            skipped_trade_ids.append(trade_id)
            _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                trade_id,
                reason=(
                    "automatic run cancelled this direct purchase because a "
                    "managed long-term buy fill for the same item was confirmed "
                    "in the current cycle"
                ),
            )
            continue
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
            result = _execute_profit_trade_buy_with_queue_timeout_retry(
                settings,
                trade_id,
                config=config,
                steam_client=steam_client,
                c5_client=c5_client,
                new_action_guard=new_action_guard,
                refresh_config_before_purchase=new_action_guard is not None,
            )
        except SteamRequestTimeout as exc:
            errors.append(
                f"buy {trade_id}: {exc}; A remains locked and will be retried after full market revalidation"
            )
            skipped_trade_ids.append(trade_id)
            continue
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
            advance_direct_cancel_race_fills(
                list(result.get("longBuyFillIds") or []),
                source_trade_id=trade_id,
            )

    def execute_new_locked_trade(
        trade_id: int,
    ) -> None:
        nonlocal buy_capacity, remaining_budget
        if buy_capacity <= 0:
            return
        row_for_budget = None
        db_budget = Database(settings.db_path)
        try:
            db_budget.initialize()
            row_for_budget = db_budget.get_profit_trade(trade_id)
        finally:
            db_budget.close()
        locked_market_hash_name = (
            str(row_for_budget["market_hash_name"] or "").strip()
            if row_for_budget is not None
            else ""
        )
        if locked_market_hash_name in filled_markets_this_cycle:
            skipped_trade_ids.append(trade_id)
            _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                trade_id,
                reason=(
                    "automatic run cancelled this direct purchase because a "
                    "managed long-term buy fill for the same item was confirmed "
                    "in the current cycle"
                ),
            )
            return
        planned_buy_price = safe_float(row_for_budget["steam_buy_price"]) if row_for_budget is not None else None
        if (
            planned_buy_price is not None
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
            return
        try:
            result = _execute_profit_trade_buy_with_queue_timeout_retry(
                settings,
                trade_id,
                config=config,
                steam_client=steam_client,
                c5_client=c5_client,
                new_action_guard=new_action_guard,
            )
        except SteamRequestTimeout as exc:
            errors.append(
                f"buy {trade_id}: {exc}; A remains locked and will be retried after full market revalidation"
            )
            skipped_trade_ids.append(trade_id)
            return
        except Exception as exc:
            errors.append(f"buy {trade_id}: {exc}")
            _cancel_locked_trade_before_steam_buy_by_id(
                settings,
                trade_id,
                reason=f"automatic run cancelled locked trade before Steam buy after error: {exc}",
            )
            return
        if not result.get("ok"):
            skipped_trade_ids.append(trade_id)
            advance_direct_cancel_race_fills(
                list(result.get("longBuyFillIds") or []),
                source_trade_id=trade_id,
            )
            return
        bought_trade_ids.append(trade_id)
        buy_capacity -= 1
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
            return
        if list_result.get("ok"):
            listed_trade_ids.append(trade_id)
        else:
            skipped_trade_ids.append(trade_id)

    watch_by_market = _profit_trade_long_buy_watch_by_market(scanned)
    for opportunity in scanned.opportunities:
        if buy_capacity <= 0:
            break
        market_hash_name = opportunity.market_hash_name
        if market_hash_name in filled_markets_this_cycle:
            errors.append(
                f"direct purchase skipped for {market_hash_name}: a managed "
                "long-term buy fill was confirmed in the current cycle"
            )
            continue
        blocked_reason = long_buy_cycle.direct_purchase_block_reasons.get(
            market_hash_name
        )
        if blocked_reason:
            errors.append(
                f"direct purchase blocked for {market_hash_name}: {blocked_reason}"
            )
            continue
        if not _profit_trade_new_action_allowed(new_action_guard):
            errors.append(
                "Profit Trade runtime was disabled after the complete scan; "
                "no further direct purchase was opened"
            )
            break
        proposal = _profit_trade_long_buy_proposal_from_watch(
            watch_by_market.get(market_hash_name)
        )
        origin_scan_id = (
            str(proposal.get("sourceScanId") or "").strip()
            if proposal is not None
            else None
        )
        db_create = Database(settings.db_path)
        try:
            db_create.initialize()
            trade_id = _create_profit_trade_from_opportunity(
                db_create,
                config,
                opportunity,
                lock_asset=True,
                origin_scan_id=origin_scan_id,
                origin_observed_at=scanned.generated_at,
            )
        finally:
            db_create.close()
        if trade_id is None:
            continue
        scanned.created_trade_ids.append(trade_id)
        if opportunity.audit_status == "manual_required":
            try:
                _send_profit_trade_manual_review_alert(
                    settings,
                    opportunity,
                    trade_id=trade_id,
                )
            except Exception as exc:
                errors.append(f"manual-review alert {trade_id}: {exc}")
            skipped_trade_ids.append(trade_id)
            continue
        scanned.locked_trade_ids.append(trade_id)
        execute_new_locked_trade(trade_id)
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


def _profit_trade_steam_bought_at(note: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in (
        "manualSteamBoughtAtOverride",
        "manualSteamBoughtAt",
        "steamBuySucceededAt",
        "steamBuyUnverifiedAt",
        "steamBuyRecoveredAt",
    ):
        value = str(note.get(key) or "").strip()
        if value:
            return value, key
    return None, None


def _profit_trade_has_purchase_request_evidence(row: Any, note: dict[str, Any]) -> bool:
    if note.get("purchaseRequestSent") is True:
        return True
    if note.get("purchaseRequestSent") is False:
        if str(row["b_asset_id"] or "").strip() or str(row["c5_product_id"] or "").strip():
            return True
        if any(
            note.get(key)
            for key in (
                "steamBuyRequestReturnedAt",
                "steamBuyOrderId",
                "steamBuySucceededAt",
                "steamBuyUnverifiedAt",
                "steamBuyRecoveredAt",
            )
        ):
            return True
        wallet_delta = safe_float(note.get("walletDelta"))
        return wallet_delta is not None and abs(wallet_delta) > 1e-9
    if note.get("cancelledBeforeSteamBuyAt"):
        return False
    if int(row["step_index"] or 0) > 2:
        return True
    if str(row["b_asset_id"] or "").strip() or str(row["c5_product_id"] or "").strip():
        return True
    if any(
        note.get(key)
        for key in PROFIT_TRADE_PURCHASE_REQUEST_EVIDENCE_NOTE_KEYS
    ):
        return True
    wallet_delta = safe_float(note.get("walletDelta"))
    return wallet_delta is not None and abs(wallet_delta) > 1e-9


def _profit_trade_purchase_request_projection(
    row: Any,
    note: dict[str, Any],
) -> tuple[bool | None, bool | None]:
    """Project strict request/listing evidence without rewriting historical note."""

    has_purchase_evidence = _profit_trade_has_purchase_request_evidence(row, note)
    explicit_request = note.get("purchaseRequestSent")
    explicit_listing = note.get("listingIdObtained")
    saved_listing_id = str(row["steam_listing_id"] or "").strip()
    if has_purchase_evidence:
        purchase_request_sent: bool | None = True
    elif isinstance(explicit_request, bool):
        purchase_request_sent = explicit_request
    else:
        status = str(row["status"] or "").strip()
        step_index = int(row["step_index"] or 0)
        cancel_source = str(note.get("cancelSource") or "").strip().lower()
        reason = " ".join(
            value
            for value in (
                str(row["error"] or "").strip(),
                str(note.get("cancelReason") or "").strip(),
            )
            if value
        ).lower()
        explicit_pre_buy_source = "pre_buy" in cancel_source or "pre-buy" in cancel_source
        listing_price_guard = cancel_source == "profit_trade_buy_listing_price_guard"
        search_listings_failed = (
            "search_listings" in reason or "listings search failed" in reason
        ) and "before steam buy" in reason
        cancelled_before_request = bool(note.get("cancelledBeforeSteamBuyAt"))
        can_infer_not_sent = (
            status in {"cancelled", "failed", "manual_required"}
            and (
                cancelled_before_request
                or (
                    step_index <= 2
                    and (
                        explicit_pre_buy_source
                        or listing_price_guard
                        or search_listings_failed
                    )
                )
            )
        )
        purchase_request_sent = False if can_infer_not_sent else None

    if isinstance(explicit_listing, bool):
        listing_id_obtained: bool | None = explicit_listing
    elif saved_listing_id and str(note.get("steamBuyMethod") or "") != "createbuyorder":
        listing_id_obtained = True
    else:
        reason = " ".join(
            value
            for value in (
                str(row["error"] or "").strip(),
                str(note.get("cancelReason") or "").strip(),
            )
            if value
        ).lower()
        search_listings_failed = (
            "search_listings" in reason or "listings search failed" in reason
        ) and "before steam buy" in reason
        listing_id_obtained = (
            False
            if not has_purchase_evidence
            and (
                search_listings_failed
                or bool(note.get("cancelledBeforeSteamBuyAt"))
            )
            else None
        )
    return purchase_request_sent, listing_id_obtained


def _public_profit_trade_orderbook_snapshot(
    value: Any,
    *,
    stage: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    snapshot = {
        key: value.get(key)
        for key in (
            "observedAt",
            "currencyId",
            "currencyValid",
            "sellerFloorPrice",
            "sellerFloorCount",
            "buyerMaxPrice",
            "buyerMaxCount",
            "spreadAmount",
            "spreadPct",
            "crossed",
            "sellOrderCountTotal",
            "buyOrderCountTotal",
        )
    }
    snapshot["stage"] = str(value.get("stage") or stage or "unknown")
    snapshot["sellLevels"] = [
        {
            "price": safe_float(item.get("price")),
            "count": safe_float(item.get("count")),
        }
        for item in list(value.get("sellLevels") or [])[:5]
        if isinstance(item, dict)
    ]
    snapshot["buyLevels"] = [
        {
            "price": safe_float(item.get("price")),
            "count": safe_float(item.get("count")),
        }
        for item in list(value.get("buyLevels") or [])[:5]
        if isinstance(item, dict)
    ]
    if (
        snapshot.get("observedAt") is None
        and snapshot.get("sellerFloorPrice") is None
        and snapshot.get("buyerMaxPrice") is None
    ):
        return None
    return snapshot


def _profit_trade_orderbook_evidence(note: dict[str, Any]) -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    scan = _public_profit_trade_orderbook_snapshot(
        note.get("scanOrderbookSnapshot"),
        stage="scan",
    )
    if scan is not None:
        snapshots.append(scan)
    for item in list(note.get("executionOrderbookSnapshots") or []):
        snapshot = _public_profit_trade_orderbook_snapshot(
            item,
            stage=str(item.get("stage") or "execution")
            if isinstance(item, dict)
            else "execution",
        )
        if snapshot is not None:
            snapshots.append(snapshot)

    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for snapshot in snapshots:
        key = (
            snapshot.get("stage"),
            snapshot.get("observedAt"),
            snapshot.get("sellerFloorPrice"),
            snapshot.get("buyerMaxPrice"),
            snapshot.get("crossed"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(snapshot)
    return {
        "crossedObserved": any(
            snapshot.get("crossed") is True for snapshot in deduplicated
        ),
        "snapshots": deduplicated,
    }


def _trade_row_to_dict(row: Any) -> dict[str, Any]:
    step_index = int(row["step_index"] or 0)
    status = str(row["status"] or "candidate")
    note = _read_note(row["note"])
    expected_roi = safe_float(row["expected_roi"])
    realized_roi = safe_float(row["realized_roi"])
    market_hash_name = str(row["market_hash_name"] or "")
    name = str(note.get("name") or market_hash_name)
    steam_bought_at, steam_bought_at_source = _profit_trade_steam_bought_at(note)
    completed_at = str(
        note.get("manualCompletedAtOverride")
        or note.get("manualCompletedAt")
        or row["completed_at"]
        or ""
    ).strip() or None
    if note.get("manualCompletedAtOverride"):
        completed_at_source = "manualCompletedAtOverride"
    elif note.get("manualCompletedAt"):
        completed_at_source = "manualCompletedAt"
    elif row["completed_at"]:
        completed_at_source = "completed_at"
    else:
        completed_at_source = None
    record_origin = str(note.get("recordOrigin") or "automatic")
    purchase_request_sent, listing_id_obtained = _profit_trade_purchase_request_projection(
        row,
        note,
    )
    public_note = dict(note)
    if purchase_request_sent is not None:
        public_note["purchaseRequestSent"] = purchase_request_sent
    if listing_id_obtained is not None:
        public_note["listingIdObtained"] = listing_id_obtained
    steam_orderbook_evidence = _profit_trade_orderbook_evidence(note)
    payload = {
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
        "note": public_note,
        "cancelSource": note.get("cancelSource"),
        "cancelReason": note.get("cancelReason"),
        "purchaseRequestSent": purchase_request_sent,
        "listingIdObtained": listing_id_obtained,
        "steamOrderbookEvidence": steam_orderbook_evidence,
        "steamBoughtAt": steam_bought_at,
        "steamBoughtAtSource": steam_bought_at_source,
        "recordOrigin": record_origin,
        "manuallyEdited": bool(note.get("manuallyEdited")),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "completedAt": completed_at,
        "completedAtSource": completed_at_source,
    }
    return sanitize_public_payload(payload)


def _profit_trade_persistent_timeline(
    row: Any,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge append-only state events with durable evidence saved on the trade.

    Older rows stored orderbook and Steam request evidence in ``note`` before
    dedicated audit events existed.  Projecting those immutable timestamps
    keeps their timeline truthful without rewriting production history.
    """

    note = _read_note(row["note"])
    merged = [dict(event) for event in events]
    cancel_reason = str(
        note.get("cancelReason") or row["error"] or ""
    ).strip() or None
    cancel_source = str(note.get("cancelSource") or "").strip() or None

    for event in reversed(merged):
        if str(event.get("statusTo") or "") not in TERMINAL_PROFIT_TRADE_STATUSES:
            continue
        if not str(event.get("reason") or "").strip() and cancel_reason:
            event["reason"] = cancel_reason
            event["reasonProjectedFromPersistentTrade"] = True
        context = event.get("context")
        if not isinstance(context, dict):
            context = {}
            event["context"] = context
        if cancel_source and not context.get("cancelSource"):
            context["cancelSource"] = cancel_source
        break

    existing_orderbooks: set[tuple[str, str]] = set()
    existing_types = {
        str(event.get("eventType") or "")
        for event in merged
    }
    for event in merged:
        if str(event.get("eventType") or "") != "orderbook_snapshot":
            continue
        context = event.get("context")
        if not isinstance(context, dict):
            continue
        snapshot = context.get("steamOrderbook")
        if not isinstance(snapshot, dict):
            continue
        existing_orderbooks.add(
            (
                str(context.get("stage") or snapshot.get("stage") or ""),
                str(snapshot.get("observedAt") or ""),
            )
        )

    evidence = _profit_trade_orderbook_evidence(note)
    for snapshot in list(evidence.get("snapshots") or []):
        if not isinstance(snapshot, dict):
            continue
        stage = str(snapshot.get("stage") or "unknown")
        observed_at = str(snapshot.get("observedAt") or "").strip()
        if (stage, observed_at) in existing_orderbooks:
            continue
        merged.append(
            {
                "id": None,
                "tradeId": int(row["id"]),
                "eventType": "orderbook_snapshot",
                "statusFrom": None,
                "statusTo": None,
                "stepKeyFrom": None,
                "stepKeyTo": None,
                "stepIndexFrom": None,
                "stepIndexTo": None,
                "reason": (
                    "Steam 盘口交叉，公开卖一可能滞后"
                    if snapshot.get("crossed") is True
                    else "Steam 盘口快照已记录"
                ),
                "logEventId": None,
                "context": {
                    "stage": stage,
                    "steamOrderbook": snapshot,
                },
                "createdAt": observed_at or row["created_at"],
                "isSnapshot": True,
                "isProjected": True,
                "projectedFrom": "profit_trades.note",
            }
        )

    purchase_request_sent = _profit_trade_has_purchase_request_evidence(row, note)
    requested_at = str(note.get("steamBuyRequestedAt") or "").strip()
    if (
        purchase_request_sent
        and requested_at
        and "steam_purchase_requested" not in existing_types
    ):
        merged.append(
            {
                "id": None,
                "tradeId": int(row["id"]),
                "eventType": "steam_purchase_requested",
                "statusFrom": None,
                "statusTo": None,
                "stepKeyFrom": None,
                "stepKeyTo": "steam_bought",
                "stepIndexFrom": None,
                "stepIndexTo": 3,
                "reason": "Steam 购买请求已发送",
                "logEventId": None,
                "context": {
                    "steamBuyMethod": note.get("steamBuyMethod"),
                    "steamBuyPrice": safe_float(
                        note.get("steamBuyPrice") or row["steam_buy_price"]
                    ),
                    "steamListingId": note.get("steamListingId"),
                    "steamBuyOrderId": note.get("steamBuyOrderId"),
                    "purchaseRequestSent": True,
                },
                "createdAt": requested_at,
                "isSnapshot": True,
                "isProjected": True,
                "projectedFrom": "profit_trades.note",
            }
        )

    request_returned_at = str(
        note.get("steamBuyRequestReturnedAt") or ""
    ).strip()
    if (
        request_returned_at
        and "steam_purchase_request_returned" not in existing_types
    ):
        merged.append(
            {
                "id": None,
                "tradeId": int(row["id"]),
                "eventType": "steam_purchase_request_returned",
                "statusFrom": None,
                "statusTo": None,
                "stepKeyFrom": None,
                "stepKeyTo": "steam_bought",
                "stepIndexFrom": None,
                "stepIndexTo": 3,
                "reason": "Steam 购买请求已返回",
                "logEventId": None,
                "context": {
                    "steamBuyMethod": note.get("steamBuyMethod"),
                    "steamListingId": note.get("steamListingId"),
                    "steamBuyOrderId": note.get("steamBuyOrderId"),
                    "purchaseRequestSent": True,
                },
                "createdAt": request_returned_at,
                "isSnapshot": True,
                "isProjected": True,
                "projectedFrom": "profit_trades.note",
            }
        )

    cancelled_at = str(
        note.get("steamBuyOrderCancellationConfirmedAt")
        or note.get("steamBuyOrderCancelledAt")
        or ""
    ).strip()
    if cancelled_at and "steam_buy_order_cancelled" not in existing_types:
        merged.append(
            {
                "id": None,
                "tradeId": int(row["id"]),
                "eventType": "steam_buy_order_cancelled",
                "statusFrom": None,
                "statusTo": None,
                "stepKeyFrom": None,
                "stepKeyTo": "steam_bought",
                "stepIndexFrom": None,
                "stepIndexTo": 3,
                "reason": "Steam 未成交求购已撤销并确认终态",
                "logEventId": None,
                "context": {
                    "steamBuyMethod": note.get("steamBuyMethod"),
                    "steamBuyOrderId": note.get("steamBuyOrderId"),
                    "cancellationConfirmedAt": cancelled_at,
                },
                "createdAt": cancelled_at,
                "isSnapshot": True,
                "isProjected": True,
                "projectedFrom": "profit_trades.note",
            }
        )

    def sort_key(event: dict[str, Any]) -> tuple[datetime, int]:
        parsed = _parse_iso(event.get("createdAt"))
        if parsed is None:
            parsed = datetime.min.replace(tzinfo=timezone.utc)
        return parsed, int(event.get("id") or 0)

    return sorted(merged, key=sort_key)


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


def _profit_trade_manual_entry_accounts() -> list[dict[str, str | None]]:
    try:
        accounts = AccountStore(PROJECT_ROOT / "config").list_accounts()
    except Exception:
        return []
    return [
        {
            "accountId": account.id,
            "name": account.name,
            "steamId": account.steam_id64,
        }
        for account in accounts
    ]


def search_profit_trade_catalog_items(
    settings: Settings,
    keyword: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    normalized = str(keyword or "").strip()
    safe_limit = max(1, min(int(limit), 50))
    safe_offset = max(0, int(offset))
    db = Database(settings.db_path)
    try:
        db.initialize()
        rows, total = db.search_items_page(
            normalized,
            limit=safe_limit,
            offset=safe_offset,
        )
    finally:
        db.close()
    next_offset = safe_offset + len(rows)
    return sanitize_public_payload(
        {
            "ok": True,
            "items": [
                {
                    "marketHashName": str(row["market_hash_name"] or ""),
                    "name": str(row["name_cn"] or row["market_hash_name"] or ""),
                }
                for row in rows
            ],
            "pagination": {
                "offset": safe_offset,
                "limit": safe_limit,
                "total": total,
                "hasMore": next_offset < total,
                "nextOffset": next_offset if next_offset < total else None,
            },
        }
    )


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
        listings_circuit = _get_profit_trade_listings_circuit(db)
        protected_market_hash_name_items = _protected_market_hash_name_items(db, config)
        long_buy_active_orders = db.count_live_profit_trade_long_buy_orders()
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
    return sanitize_public_payload(
        {
            "generatedAt": utc_now_iso(),
            "config": _config_payload(
                config,
                protected_market_hash_name_items=protected_market_hash_name_items,
            ),
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
                "longBuyActiveOrders": long_buy_active_orders,
            },
            "trades": trades,
            "reservations": reservations,
            "manualEntryOptions": {
                "accounts": _profit_trade_manual_entry_accounts(),
            },
            "listingsCircuit": listings_circuit,
            "lastRun": _read_profit_trade_last_run(settings),
        }
    )


def build_profit_trade_completed_payload(
    settings: Settings,
    *,
    bought_from: str | None = None,
    bought_to: str | None = None,
) -> dict[str, Any]:
    """Return completed trades for a real Steam purchase-time range."""

    start = _parse_iso(bought_from)
    end = _parse_iso(bought_to)
    if bought_from and start is None:
        raise ValueError("invalid boughtFrom timestamp")
    if bought_to and end is None:
        raise ValueError("invalid boughtTo timestamp")
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start is not None and end is not None and start > end:
        raise ValueError("boughtFrom cannot be later than boughtTo")

    db = Database(settings.db_path)
    try:
        db.initialize()
        trades: list[dict[str, Any]] = []
        for row in db.list_profit_trades(status="completed", limit=None):
            trade = _trade_row_to_dict(row)
            bought_at = _parse_iso(trade.get("steamBoughtAt"))
            if bought_at is None:
                if start is not None or end is not None:
                    continue
            else:
                if bought_at.tzinfo is None:
                    bought_at = bought_at.replace(tzinfo=timezone.utc)
                bought_at = bought_at.astimezone(timezone.utc)
                if start is not None and bought_at < start.astimezone(timezone.utc):
                    continue
                if end is not None and bought_at > end.astimezone(timezone.utc):
                    continue
            trades.append(trade)
        _hydrate_trade_names(db, trades)
    finally:
        db.close()

    trades.sort(
        key=lambda trade: (
            _parse_iso(trade.get("steamBoughtAt"))
            or datetime.min.replace(tzinfo=timezone.utc),
            int(trade.get("id") or 0),
        ),
        reverse=True,
    )
    return sanitize_public_payload(
        {
            "generatedAt": utc_now_iso(),
            "filters": {
                "boughtFrom": bought_from or None,
                "boughtTo": bought_to or None,
            },
            "summary": {
                "count": len(trades),
                "realizedProfit": round(
                    sum(float(trade.get("realizedProfit") or 0) for trade in trades),
                    2,
                ),
                "steamBuyTotal": round(
                    sum(float(trade.get("steamBuyPrice") or 0) for trade in trades),
                    2,
                ),
            },
            "items": trades,
        }
    )


def _profit_trade_roi_link_summary(row: Any) -> dict[str, Any]:
    trade = _trade_row_to_dict(row)
    return {
        "tradeId": trade.get("id"),
        "tradeNo": trade.get("tradeNo"),
        "status": trade.get("status"),
        "stepKey": trade.get("stepKey"),
        "stepIndex": trade.get("stepIndex"),
        "progress": trade.get("progress"),
        "steamBoughtAt": trade.get("steamBoughtAt"),
        "completedAt": trade.get("completedAt"),
        "steamBuyPrice": trade.get("steamBuyPrice"),
        "c5ListingPrice": trade.get("c5ListingPrice"),
        "c5SoldNetPrice": trade.get("c5SoldNetPrice"),
        "expectedProfit": trade.get("expectedProfit"),
        "realizedProfit": trade.get("realizedProfit"),
        "expectedRoi": trade.get("expectedRoi"),
        "realizedRoi": trade.get("realizedRoi"),
        "error": trade.get("error"),
        "createdAt": trade.get("createdAt"),
        "manuallyEdited": trade.get("manuallyEdited"),
    }


def _profit_trade_roi_links_for_market(
    db: Database,
    market_hash_name: str,
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    latest: dict[str, Any] | None = None
    by_scan_id: dict[str, dict[str, Any]] = {}
    for row in db.list_profit_trades_for_market_hash_name(market_hash_name, limit=500):
        note = _read_note(row["note"])
        scan_id = str(note.get("originScanId") or "").strip()
        if not scan_id:
            continue
        summary = _profit_trade_roi_link_summary(row)
        if latest is None:
            latest = summary
        by_scan_id.setdefault(scan_id, summary)
    return latest, by_scan_id


def build_profit_trade_roi_watch_payload(
    settings: Settings,
    *,
    active: bool | None = True,
    keyword: str | None = None,
    execution_status: str | None = None,
    sort: str = "roi_desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    db = Database(settings.db_path)
    try:
        db.initialize()
        payload = db.list_profit_trade_roi_watch(
            active=active,
            keyword=keyword,
            execution_status=execution_status,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        live_long_buy_orders = {
            str(row["market_hash_name"]): row
            for row in db.list_profit_trade_long_buy_orders(
                states=LONG_BUY_LIVE_STATES,
                limit=max(
                    500,
                    int(getattr(
                        load_strategy_config(settings),
                        "profit_trade_long_buy_max_active_orders",
                        25,
                    ))
                    * 2,
                ),
            )
        }
        for item in list(payload.get("items") or []):
            market_hash_name = str(item.get("marketHashName") or "")
            latest_trade, _ = _profit_trade_roi_links_for_market(
                db,
                market_hash_name,
            )
            item["latestTrade"] = latest_trade
            item["longBuyOrder"] = long_buy_order_public(
                live_long_buy_orders.get(market_hash_name)
            )
        payload.setdefault("summary", {})["longBuyActiveOrders"] = len(
            live_long_buy_orders
        )
        listings_circuit = _get_profit_trade_listings_circuit(db)
    finally:
        db.close()
    return sanitize_public_payload(
        {
            "generatedAt": utc_now_iso(),
            "listingsCircuit": listings_circuit,
            **payload,
        }
    )


def _manual_record_utc_time(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    parsed = _parse_iso(text)
    if parsed is None:
        raise ValueError(f"{field_name} must be a valid ISO 8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat()


def _manual_record_account(account_id: Any) -> dict[str, str | None]:
    normalized = str(account_id or "").strip()
    if not normalized:
        return {"accountId": None, "accountName": None, "steamId": None}
    account = AccountStore(PROJECT_ROOT / "config").get_account(normalized)
    if account is None:
        raise ValueError(f"Steam account not found: {normalized}")
    return {
        "accountId": account.id,
        "accountName": account.name,
        "steamId": account.steam_id64,
    }


def _manual_record_values(
    *,
    market_hash_name: Any,
    steam_buy_price: Any,
    balance_discount: Any,
    c5_sold_net_price: Any,
    steam_bought_at: Any,
    completed_at: Any,
) -> dict[str, Any]:
    normalized_name = str(market_hash_name or "").strip()
    if not normalized_name:
        raise ValueError("marketHashName is required")
    buy_price = safe_float(steam_buy_price)
    sold_net = safe_float(c5_sold_net_price)
    discount = safe_float(balance_discount)
    if buy_price is None or buy_price <= 0:
        raise ValueError("steamBuyPrice must be positive")
    if sold_net is None or sold_net <= 0:
        raise ValueError("c5SoldNetPrice must be positive")
    if discount is None or discount <= 0 or discount > 1:
        raise ValueError("balanceDiscount must be greater than 0 and no more than 1")
    bought_at_utc = _manual_record_utc_time(steam_bought_at, field_name="steamBoughtAt")
    completed_at_utc = _manual_record_utc_time(completed_at, field_name="completedAt")
    if datetime.fromisoformat(completed_at_utc) < datetime.fromisoformat(bought_at_utc):
        raise ValueError("completedAt cannot be earlier than steamBoughtAt")
    realized_profit, realized_roi = _realized_values(
        sold_net_price=float(sold_net),
        steam_buy_price=float(buy_price),
        steam_cost_ratio=float(discount),
    )
    return {
        "market_hash_name": normalized_name,
        "steam_buy_price": float(buy_price),
        "steam_balance_discount": float(discount),
        "steam_real_cost": float(buy_price) * float(discount),
        "c5_sold_net_price": float(sold_net),
        "realized_profit": realized_profit,
        "realized_roi": realized_roi,
        "steam_bought_at": bought_at_utc,
        "completed_at": completed_at_utc,
    }


def _manual_record_display_name(
    db: Database,
    market_hash_name: str,
    supplied_name: Any,
) -> str:
    normalized = str(supplied_name or "").strip()
    if normalized:
        return normalized
    item = db.get_item(market_hash_name)
    if item is not None:
        name_cn = str(item["name_cn"] or "").strip()
        if name_cn:
            return name_cn
    return market_hash_name


def create_manual_profit_trade_record(
    settings: Settings,
    *,
    market_hash_name: Any,
    steam_buy_price: Any,
    balance_discount: Any,
    c5_sold_net_price: Any,
    steam_bought_at: Any,
    completed_at: Any,
    name: Any = None,
    steam_account_id: Any = None,
    a_asset_id: Any = None,
    b_asset_id: Any = None,
    memo: Any = None,
) -> dict[str, Any]:
    values = _manual_record_values(
        market_hash_name=market_hash_name,
        steam_buy_price=steam_buy_price,
        balance_discount=balance_discount,
        c5_sold_net_price=c5_sold_net_price,
        steam_bought_at=steam_bought_at,
        completed_at=completed_at,
    )
    account = _manual_record_account(steam_account_id)
    created_at = utc_now_iso()
    beijing_date = datetime.fromisoformat(values["steam_bought_at"]).astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%Y%m%d")
    trade_no = f"PT-MANUAL-{beijing_date}-{uuid.uuid4().hex[:8]}"
    db = Database(settings.db_path)
    try:
        db.initialize()
        display_name = _manual_record_display_name(db, values["market_hash_name"], name)
        note = {
            "name": display_name,
            "recordOrigin": "manual_backfill",
            "manualCreatedAt": created_at,
            "manualCreatedMemo": str(memo or "").strip() or None,
            "manualSteamBoughtAt": values["steam_bought_at"],
            "manualCompletedAt": values["completed_at"],
            "steamAccountId": account["accountId"],
            "steamAccountName": account["accountName"],
            "steamId": account["steamId"],
        }
        trade_id = db.add_profit_trade(
            trade_no=trade_no,
            market_hash_name=values["market_hash_name"],
            status="completed",
            step_key="settled",
            step_index=6,
            a_asset_id=str(a_asset_id or "").strip() or None,
            b_asset_id=str(b_asset_id or "").strip() or None,
            steam_buy_price=values["steam_buy_price"],
            steam_balance_discount=values["steam_balance_discount"],
            steam_real_cost=values["steam_real_cost"],
            c5_sold_net_price=values["c5_sold_net_price"],
            realized_profit=values["realized_profit"],
            realized_roi=values["realized_roi"],
            note=_build_note(note),
        )
        db.update_profit_trade(trade_id, completed_at=values["completed_at"])
        db.add_profit_trade_audit_event(
            trade_id,
            event_type="manual_create",
            reason="manual historical Profit Trade record created",
            context={
                "recordOrigin": "manual_backfill",
                "steamBoughtAt": values["steam_bought_at"],
                "completedAt": values["completed_at"],
            },
        )
        updated = db.get_profit_trade(trade_id)
        return {"ok": True, "changed": True, "trade": _trade_row_to_dict(updated)}
    finally:
        db.close()


def update_manual_profit_trade_record(
    settings: Settings,
    trade_id: int,
    *,
    market_hash_name: Any,
    steam_buy_price: Any,
    balance_discount: Any,
    c5_sold_net_price: Any,
    steam_bought_at: Any,
    completed_at: Any,
    name: Any = None,
    steam_account_id: Any = None,
    a_asset_id: Any = None,
    b_asset_id: Any = None,
    memo: Any = None,
) -> dict[str, Any]:
    values = _manual_record_values(
        market_hash_name=market_hash_name,
        steam_buy_price=steam_buy_price,
        balance_discount=balance_discount,
        c5_sold_net_price=c5_sold_net_price,
        steam_bought_at=steam_bought_at,
        completed_at=completed_at,
    )
    account = _manual_record_account(steam_account_id)
    db = Database(settings.db_path)
    try:
        db.initialize()
        row = db.get_profit_trade(int(trade_id))
        if row is None:
            raise ValueError(f"profit trade not found: {trade_id}")
        if str(row["status"] or "") != "completed":
            raise RuntimeError("only completed Profit Trade records can be edited")
        old_note = _read_note(row["note"])
        is_manual = str(old_note.get("recordOrigin") or "") == "manual_backfill"
        display_name = _manual_record_display_name(db, values["market_hash_name"], name)
        new_note = {
            **old_note,
            "name": display_name,
            "steamAccountId": account["accountId"],
            "steamAccountName": account["accountName"],
            "steamId": account["steamId"],
            "manualEditedAt": utc_now_iso(),
            "manualEditedMemo": str(memo or "").strip() or None,
            "manuallyEdited": True,
        }
        if is_manual:
            new_note["manualSteamBoughtAt"] = values["steam_bought_at"]
            new_note["manualCompletedAt"] = values["completed_at"]
        else:
            new_note["manualSteamBoughtAtOverride"] = values["steam_bought_at"]
            new_note["manualCompletedAtOverride"] = values["completed_at"]
        old_public = _trade_row_to_dict(row)
        update_fields = {
            "market_hash_name": values["market_hash_name"],
            "a_asset_id": str(a_asset_id or "").strip() or None,
            "b_asset_id": str(b_asset_id or "").strip() or None,
            "steam_buy_price": values["steam_buy_price"],
            "steam_balance_discount": values["steam_balance_discount"],
            "steam_real_cost": values["steam_real_cost"],
            "c5_sold_net_price": values["c5_sold_net_price"],
            "realized_profit": values["realized_profit"],
            "realized_roi": values["realized_roi"],
            "note": _build_note(new_note),
        }
        if is_manual:
            update_fields["completed_at"] = values["completed_at"]
        db.update_profit_trade(int(trade_id), **update_fields)
        updated = db.get_profit_trade(int(trade_id))
        new_public = _trade_row_to_dict(updated)
        comparable_fields = (
            "marketHashName", "name", "steamBuyPrice", "steamBalanceDiscount",
            "steamRealCost", "c5SoldNetPrice", "realizedProfit", "realizedRoi",
            "steamBoughtAt", "completedAt", "aAssetId", "bAssetId",
        )
        changes = {
            key: {"old": old_public.get(key), "new": new_public.get(key)}
            for key in comparable_fields
            if old_public.get(key) != new_public.get(key)
        }
        old_account = {
            "accountId": old_note.get("steamAccountId"),
            "accountName": old_note.get("steamAccountName"),
            "steamId": old_note.get("steamId"),
        }
        if old_account != account:
            changes["steamBuyAccount"] = {"old": old_account, "new": account}
        if str(old_note.get("manualEditedMemo") or "") != str(memo or "").strip():
            changes["memo"] = {
                "old": old_note.get("manualEditedMemo"),
                "new": str(memo or "").strip() or None,
            }
        db.add_profit_trade_audit_event(
            int(trade_id),
            event_type="manual_edit",
            reason="completed Profit Trade record manually corrected",
            context={"changes": changes, "recordOrigin": new_note.get("recordOrigin", "automatic")},
        )
        return {"ok": True, "changed": bool(changes), "trade": new_public}
    finally:
        db.close()


def build_profit_trade_roi_history_payload(
    settings: Settings,
    market_hash_name: str,
    *,
    from_time: str | None = None,
    to_time: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    normalized_name = str(market_hash_name or "").strip()
    if not normalized_name:
        raise ValueError("market_hash_name is required")
    db = Database(settings.db_path)
    try:
        db.initialize()
        payload = db.list_profit_trade_roi_history(
            normalized_name,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
        )
        _, trades_by_scan_id = _profit_trade_roi_links_for_market(db, normalized_name)
        for item in list(payload.get("items") or []):
            item["relatedTrade"] = trades_by_scan_id.get(
                str(item.get("scanId") or "").strip()
            )
    finally:
        db.close()
    return sanitize_public_payload(
        {
            "generatedAt": utc_now_iso(),
            "marketHashName": normalized_name,
            **payload,
        }
    )


def build_profit_trade_interruptions_payload(
    settings: Settings,
    *,
    statuses: tuple[str, ...] = ("cancelled", "failed", "manual_required"),
    step_key: str | None = None,
    acknowledged: str = "exclude",
    keyword: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    allowed_statuses = {"cancelled", "failed", "manual_required"}
    normalized_statuses = tuple(str(value).strip() for value in statuses if str(value).strip())
    invalid_statuses = sorted(set(normalized_statuses) - allowed_statuses)
    if not normalized_statuses or invalid_statuses:
        detail = ", ".join(invalid_statuses) if invalid_statuses else "empty status list"
        raise ValueError(f"invalid Profit Trade interruption status filter: {detail}")
    if acknowledged not in {"exclude", "include", "only"}:
        raise ValueError("acknowledged must be exclude, include, or only")
    db = Database(settings.db_path)
    try:
        db.initialize()
        payload = db.list_profit_trade_interruptions(
            statuses=normalized_statuses,
            step_key=step_key,
            acknowledged=acknowledged,
            keyword=keyword,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
        )
        items: list[dict[str, Any]] = []
        for row in payload["items"]:
            trade = _trade_row_to_dict(row)
            trade.update(
                {
                    "acknowledged": bool(row.get("acknowledged")),
                    "acknowledgementReason": row.get("acknowledgement_reason"),
                    "acknowledgedAt": row.get("acknowledged_at"),
                    "acknowledgementRestoredAt": row.get("restored_at"),
                }
            )
            items.append(trade)
        _hydrate_trade_names(db, items)
        summary = db.get_profit_trade_interruption_summary(
            statuses=normalized_statuses,
            acknowledged=acknowledged,
            keyword=keyword,
            from_time=from_time,
            to_time=to_time,
        )
        listings_circuit = _get_profit_trade_listings_circuit(db)
    finally:
        db.close()
    return sanitize_public_payload(
        {
            "generatedAt": utc_now_iso(),
            "steps": PROFIT_TRADE_STEPS,
            "summary": summary,
            "stepCounts": summary["stepCounts"],
            "listingsCircuit": listings_circuit,
            "items": items,
            "total": payload["total"],
            "page": payload["page"],
            "pageSize": payload["pageSize"],
        }
    )


def build_profit_trade_interruption_timeline_payload(
    settings: Settings,
    trade_id: int,
) -> dict[str, Any]:
    db = Database(settings.db_path)
    try:
        db.initialize()
        row = db.get_profit_trade(int(trade_id))
        if row is None:
            raise RuntimeError(f"profit trade not found: {trade_id}")
        trade = _trade_row_to_dict(row)
        _hydrate_trade_names(db, [trade])
        acknowledgement = db.conn.execute(
            "SELECT * FROM profit_trade_acknowledgements WHERE trade_id = ?",
            (int(trade_id),),
        ).fetchone()
        trade["acknowledged"] = bool(acknowledgement["acknowledged"]) if acknowledgement else False
        trade["acknowledgementReason"] = acknowledgement["reason"] if acknowledgement else None
        trade["acknowledgedAt"] = acknowledgement["acknowledged_at"] if acknowledgement else None
        trade["acknowledgementRestoredAt"] = acknowledgement["restored_at"] if acknowledgement else None
        events = db.list_profit_trade_state_events(int(trade_id))
        events = _profit_trade_persistent_timeline(row, events)
        listings_circuit = _get_profit_trade_listings_circuit(db)
    finally:
        db.close()
    return sanitize_public_payload(
        {
            "generatedAt": utc_now_iso(),
            "steps": PROFIT_TRADE_STEPS,
            "listingsCircuit": listings_circuit,
            "trade": trade,
            "events": events,
        }
    )


def profit_trade_interruption_acknowledgement_safety(row: Any) -> dict[str, Any]:
    status = str(row["status"] or "").strip()
    if status not in {"cancelled", "failed", "manual_required"}:
        return {
            "allowed": False,
            "requiresRemoteResolution": False,
            "reason": f"profit trade is not an interruptible terminal record: {status}",
        }
    note = _read_note(row["note"])
    has_purchase_evidence = bool(
        str(row["b_asset_id"] or "").strip()
        or str(row["c5_product_id"] or "").strip()
        or note.get("steamBuySucceededAt")
        or note.get("steamPurchaseReceipt")
    )
    tracked_buy_order_id = str(note.get("steamBuyOrderId") or "").strip()
    cancellation_confirmed = bool(
        note.get("steamBuyOrderCancellationConfirmedAt")
        or note.get("orphanBuyOrderCancellationConfirmedAt")
    )
    if tracked_buy_order_id and not has_purchase_evidence and not cancellation_confirmed:
        return {
            "allowed": False,
            "requiresRemoteResolution": True,
            "reason": (
                "tracked Steam buy order has no confirmed terminal state; "
                "resolve it before acknowledging"
            ),
            "steamBuyOrderId": tracked_buy_order_id,
        }
    if note.get("steamBuyUnverifiedAt") and not has_purchase_evidence and not tracked_buy_order_id:
        return {
            "allowed": False,
            "requiresRemoteResolution": True,
            "reason": (
                "unverified Steam buy has no order id and no purchase evidence; "
                "manual terminal-state verification is required"
            ),
        }
    return {
        "allowed": True,
        "requiresRemoteResolution": False,
        "reason": "local evidence contains no unresolved Steam buy order",
    }


def set_profit_trade_interruption_acknowledged(
    settings: Settings,
    trade_id: int,
    *,
    acknowledged: bool,
    reason: str | None = None,
    remote_terminal_confirmed: bool = False,
) -> dict[str, Any]:
    db = Database(settings.db_path)
    try:
        db.initialize()
        row = db.get_profit_trade(int(trade_id))
        if row is None:
            raise RuntimeError(f"profit trade not found: {trade_id}")
        safety = profit_trade_interruption_acknowledgement_safety(row)
        remote_override_allowed = bool(
            remote_terminal_confirmed and safety.get("requiresRemoteResolution")
        )
        if acknowledged and not safety["allowed"] and not remote_override_allowed:
            return {
                "ok": False,
                "changed": False,
                "conflict": True,
                "requiresRemoteResolution": bool(safety["requiresRemoteResolution"]),
                "message": safety["reason"],
                "safety": safety,
                "trade": _trade_row_to_dict(row),
            }
        result = db.set_profit_trade_acknowledgement(
            int(trade_id),
            acknowledged=bool(acknowledged),
            reason=reason,
        )
        updated = db.get_profit_trade(int(trade_id))
    finally:
        db.close()
    return {
        "ok": True,
        "changed": True,
        "conflict": False,
        "requiresRemoteResolution": False,
        "acknowledgement": result,
        "trade": _trade_row_to_dict(updated),
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
    long_buy_enabled: bool | None = None,
    long_buy_allow_real_execution: bool | None = None,
    long_buy_max_active_orders: int | None = None,
    long_buy_create_fraction_per_cycle: float | None = None,
    long_buy_aggressive_roi_delta: float | None = None,
    long_buy_min_price_advantage: float | None = None,
    long_buy_max_price_advantage: float | None = None,
    sticker_slab_status: str | None = None,
    sticker_status: str | None = None,
    daily_steam_budget: float | None = None,
    account_reserved_balances: dict[str, Any] | None = None,
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
    if long_buy_enabled is not None:
        config.profit_trade_long_buy_enabled = bool(long_buy_enabled)
    if long_buy_allow_real_execution is not None:
        config.profit_trade_long_buy_allow_real_execution = bool(
            long_buy_allow_real_execution
        )
    if long_buy_max_active_orders is not None:
        maximum = int(long_buy_max_active_orders)
        if maximum < 0:
            raise ValueError("longBuyMaxActiveOrders must be >= 0")
        config.profit_trade_long_buy_max_active_orders = maximum
    if long_buy_create_fraction_per_cycle is not None:
        fraction = float(long_buy_create_fraction_per_cycle)
        if not math.isfinite(fraction) or fraction <= 0 or fraction > 1:
            raise ValueError(
                "longBuyCreateFractionPerCycle must be > 0 and <= 1"
            )
        config.profit_trade_long_buy_create_fraction_per_cycle = fraction
    if long_buy_aggressive_roi_delta is not None:
        delta = float(long_buy_aggressive_roi_delta)
        if not math.isfinite(delta) or delta < 0:
            raise ValueError("longBuyAggressiveRoiDelta must be >= 0")
        config.profit_trade_long_buy_aggressive_roi_delta = delta
    if long_buy_min_price_advantage is not None:
        advantage = float(long_buy_min_price_advantage)
        if not math.isfinite(advantage) or advantage <= 0:
            raise ValueError("longBuyMinPriceAdvantage must be > 0")
        config.profit_trade_long_buy_min_price_advantage = advantage
    if long_buy_max_price_advantage is not None:
        advantage = float(long_buy_max_price_advantage)
        if not math.isfinite(advantage) or advantage <= 0:
            raise ValueError("longBuyMaxPriceAdvantage must be > 0")
        config.profit_trade_long_buy_max_price_advantage = advantage
    if (
        float(config.profit_trade_long_buy_max_price_advantage)
        < float(config.profit_trade_long_buy_min_price_advantage)
    ):
        raise ValueError(
            "longBuyMaxPriceAdvantage must be >= longBuyMinPriceAdvantage"
        )
    if sticker_slab_status is not None:
        config.profit_trade_sticker_slab_status = _normalize_status(sticker_slab_status)
    if sticker_status is not None:
        config.profit_trade_sticker_status = _normalize_status(sticker_status)
    if daily_steam_budget is not None:
        budget = float(daily_steam_budget)
        if not math.isfinite(budget) or budget < 0:
            raise ValueError("dailySteamBudget must be >= 0")
        config.profit_trade_daily_steam_budget = budget
    if account_reserved_balances is not None:
        normalized_balances: dict[str, float] = {}
        for raw_key, raw_value in account_reserved_balances.items():
            key = str(raw_key or "").strip()
            if not key:
                raise ValueError("accountReservedBalances keys cannot be empty")
            try:
                amount = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"accountReservedBalances[{key}] must be a number"
                ) from exc
            if not math.isfinite(amount) or amount < 0:
                raise ValueError(
                    f"accountReservedBalances[{key}] must be >= 0"
                )
            normalized_balances[key] = round(amount, 2)
        config.profit_trade_account_reserved_balances = normalized_balances
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


# ---------------------------------------------------------------------------
# All-market selection watch (research-only; never a trade queue)
# ---------------------------------------------------------------------------


def _selection_watch_after(
    seconds: float = PROFIT_TRADE_SELECTION_WATCH_INTERVAL_SECONDS,
    *,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (
        current.astimezone(timezone.utc)
        + timedelta(seconds=max(1.0, float(seconds)))
    ).isoformat()


def _selection_watch_orderbook_snapshot(state: MarketState) -> dict[str, Any]:
    """Return the bounded snapshot already attached to one MarketState.

    Selection research must never issue a second orderbook request merely to
    inspect buy orders.  ``MarketService`` puts both sides of the same Steam
    response in ``steam_orderbook_snapshot``; this helper only normalizes the
    in-memory representation for the selection persistence layer.
    """

    raw = state.raw_json if isinstance(state.raw_json, dict) else {}
    snapshot = raw.get("steam_orderbook_snapshot")
    if isinstance(snapshot, dict):
        return dict(snapshot)
    payload = raw.get("steam_orderbook")
    if isinstance(payload, dict):
        return build_orderbook_snapshot(
            payload,
            observed_at=utc_now_iso(),
            depth=5,
            expected_currency=23,
        )
    return {}


def _selection_watch_observation(
    *,
    config: StrategyConfig,
    selected: dict[str, Any],
    state: MarketState | None,
    error: str | None = None,
    event_type: str = "observed",
    shared_snapshot: bool = False,
) -> dict[str, Any]:
    """Project a market read into a non-executable selection observation.

    This intentionally does not call ``_build_market_evaluation``.  The
    ordinary Profit Trade evaluator has real-execution gates (inventory,
    minimum item value, C5 risk and automatic ROI thresholds) which are useful
    for a trade queue but would incorrectly discard research selections.
    """

    market_hash_name = str(selected.get("market_hash_name") or "").strip()
    name_cn = str(selected.get("name_cn") or market_hash_name).strip() or market_hash_name
    raw_state = state.raw_json if state is not None and isinstance(state.raw_json, dict) else {}
    snapshot = _selection_watch_orderbook_snapshot(state) if state is not None else {}
    currency_id = safe_int(snapshot.get("currencyId")) if snapshot else None
    currency_valid = (
        snapshot.get("currencyValid") is not False
        and (currency_id is None or int(currency_id) == 23)
    )
    c5_listing_price = safe_float(state.c5_sell_price) if state is not None else None
    if c5_listing_price is not None and c5_listing_price <= 0:
        c5_listing_price = None
    steam_buy_price = safe_float(state.steam_sell_price) if state is not None else None
    if steam_buy_price is not None and steam_buy_price <= 0:
        steam_buy_price = None
    if not currency_valid:
        # Do not ever treat a non-CNY minor-unit price as a CNY price.  Keep
        # the snapshot for diagnostics, but withhold it from ROI arithmetic.
        steam_buy_price = None

    buyer_max_price = safe_float(snapshot.get("buyerMaxPrice")) if snapshot else None
    if buyer_max_price is not None and buyer_max_price <= 0:
        buyer_max_price = None
    if not currency_valid:
        buyer_max_price = None

    c5_expected_net_price = (
        float(c5_listing_price) * float(config.profit_trade_c5_current_sale_net_factor)
        if c5_listing_price is not None
        else None
    )
    balance_discount = float(config.profit_trade_balance_discount)
    expected_profit: float | None = None
    expected_roi: float | None = None
    if c5_expected_net_price is not None and steam_buy_price is not None:
        expected_profit = float(c5_expected_net_price) - float(steam_buy_price) * balance_discount
        expected_roi = float(c5_expected_net_price) / float(steam_buy_price) - balance_discount

    buy_order_reference_profit: float | None = None
    buy_order_reference_roi: float | None = None
    crossed = snapshot.get("crossed") is True
    if not currency_valid:
        buy_order_reference_status = "currency_invalid"
    elif c5_expected_net_price is None:
        buy_order_reference_status = "c5_price_unavailable"
    elif buyer_max_price is None:
        buy_order_reference_status = "missing_buy_book"
    else:
        buy_order_reference_profit = (
            float(c5_expected_net_price) - float(buyer_max_price) * balance_discount
        )
        buy_order_reference_roi = (
            float(c5_expected_net_price) / float(buyer_max_price) - balance_discount
        )
        buy_order_reference_status = "crossed_possible_stale" if crossed else "valid"

    c5_batch_error = str(raw_state.get("c5_batch_error") or "").strip()
    steam_error = str(raw_state.get("steam_orderbook_error") or "").strip()
    status = "observed"
    last_error = str(error or "").strip() or None
    if last_error:
        status = "scan_failed"
    elif not currency_valid:
        status = "currency_invalid"
        last_error = (
            f"Steam orderbook currency must be CNY (23), got {currency_id!r}"
        )
    elif c5_listing_price is None or steam_buy_price is None:
        status = "price_unavailable"
        reasons: list[str] = []
        if c5_listing_price is None:
            reasons.append(c5_batch_error or "C5 batch listing price unavailable")
        if steam_buy_price is None:
            reasons.append(steam_error or "Steam public sell price unavailable")
        last_error = " | ".join(reasons) or None

    snapshot_payload = dict(snapshot)
    crossed_listing_probe = (
        dict(raw_state.get("crossed_listing_probe") or {})
        if isinstance(raw_state.get("crossed_listing_probe"), dict)
        else None
    )
    raw_payload = {
        "steamOrderbook": snapshot_payload,
        "crossedListingProbe": crossed_listing_probe,
        "selection": {
            "researchOnly": True,
            "sharedSnapshot": bool(shared_snapshot),
            "usesOnly": [
                "c5_price_batch",
                "steam_orderbook",
                *(["steam_search_listings_crossed_probe"] if crossed_listing_probe else []),
            ],
            "c5BatchError": c5_batch_error or None,
            "steamOrderbookError": steam_error or None,
        },
    }
    return {
        "market_hash_name": market_hash_name,
        "name_cn": name_cn,
        "status": status,
        "event_type": event_type,
        "last_error": last_error,
        "steam_buy_price": steam_buy_price,
        "steam_price_source": (
            state.steam_price_source if state is not None else None
        ) or ("steam_orderbook" if snapshot_payload else None),
        "c5_listing_price": c5_listing_price,
        "c5_price_source": state.c5_price_source if state is not None else None,
        "c5_expected_net_price": c5_expected_net_price,
        "balance_discount": balance_discount,
        "expected_profit": expected_profit,
        "expected_roi": expected_roi,
        "buy_order_reference_roi": buy_order_reference_roi,
        "buy_order_reference_profit": buy_order_reference_profit,
        "buy_order_reference_status": buy_order_reference_status,
        "risk_status": "selection_only",
        "risk_reason": "research-only selection watch; never an execution gate",
        "raw": raw_payload,
    }


def _selection_watch_load_c5_prices(
    market_service: MarketService,
    selected_rows: list[dict[str, Any]],
) -> dict[str, MarketState]:
    """Load only the C5 batch price for the rows that lack a shared state."""

    items = [
        {
            "market_hash_name": str(row.get("market_hash_name") or "").strip(),
            "name_cn": str(row.get("name_cn") or "").strip() or None,
        }
        for row in selected_rows
        if str(row.get("market_hash_name") or "").strip()
    ]
    states = MarketService._create_states(items)
    if not states:
        return states
    load_c5 = getattr(market_service, "_load_c5_prices", None)
    if callable(load_c5):
        load_c5(states, list(states.keys()))
    else:  # pragma: no cover - test injection fallback
        for state in states.values():
            state.raw_json["c5_batch_error"] = "selection market service has no C5 batch loader"
    return states


def refresh_profit_trade_selection_watch(
    settings: Settings,
    config: StrategyConfig | None = None,
    *,
    market_service: MarketService | None = None,
    shared_state_map: dict[str, MarketState] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh due all-market research selections without creating a trade.

    The selection watch is deliberately a P3 observation path.  It performs at
    most one C5 ``price_batch`` and one Steam ``order_book`` per selection.  A
    crossed orderbook may add exactly one non-buying ``search_listings`` witness;
    normal snapshots never consult listings, and an open listings circuit is
    always respected.  The scan stops the remainder of the batch immediately
    after a Steam orderbook 429.  A normal Profit Trade cycle passes
    ``force=True`` plus its ``MarketState`` map: every selected item is refreshed
    in that same inventory cycle, while shared items reuse the exact already-read
    snapshot and any already-captured listing witness.
    """

    config = config or load_strategy_config(settings)
    scan_id = f"PTSEL-{uuid.uuid4().hex}"
    observed_at = utc_now_iso()
    event_logger = get_profit_trade_event_logger()
    db = Database(settings.db_path)
    try:
        db.initialize()
        due_rows = [
            dict(row)
            for row in db.list_due_profit_trade_selection_watch(
                include_not_due=bool(force)
            )
        ]
        active_count = db.count_active_profit_trade_selection_watch()
    finally:
        db.close()

    if not due_rows:
        db = Database(settings.db_path)
        try:
            db.initialize()
            next_due_at = db.next_profit_trade_selection_watch_due_at()
            active_count = db.count_active_profit_trade_selection_watch()
        finally:
            db.close()
        return {
            "ok": True,
            "scanId": scan_id,
            "activeCount": active_count,
            "dueCount": 0,
            "observedCount": 0,
            "reusedSnapshotCount": 0,
            "deferredCount": 0,
            "rateLimited": False,
            "persisted": {"inserted": 0, "updated": 0},
            "nextDueAt": next_due_at,
            "errors": [],
        }

    shared = dict(shared_state_map or {})
    direct_rows = [
        row
        for row in due_rows
        if str(row.get("market_hash_name") or "").strip() not in shared
    ]
    # When this is called from a real Profit Trade cycle, its market service may
    # have a client that is allowed to relogin.  Reuse that service only for
    # injected/standalone tests; production selection reads build a separate
    # no-relogin service below whenever they need a new remote request.
    if direct_rows and (market_service is None or shared_state_map is not None):
        market_service = _build_profit_trade_market_service(
            settings,
            telemetry_context={"run_id": scan_id},
            allow_relogin=False,
        )
    direct_states = (
        _selection_watch_load_c5_prices(market_service, direct_rows)
        if direct_rows and market_service is not None
        else {}
    )
    steam_clients = (
        list(getattr(market_service, "steam_market_clients", []) or [])
        if market_service is not None
        else []
    )
    steam_client = steam_clients[0] if steam_clients else None

    observations: list[dict[str, Any]] = []
    deferred_names: list[str] = []
    errors: list[str] = []
    reused_snapshot_count = 0
    rate_limited = False
    for index, selected in enumerate(due_rows):
        market_hash_name = str(selected.get("market_hash_name") or "").strip()
        if not market_hash_name:
            continue
        if rate_limited:
            deferred_names.append(market_hash_name)
            continue
        shared_state = shared.get(market_hash_name)
        if shared_state is not None:
            reused_snapshot_count += 1
            observations.append(
                _selection_watch_observation(
                    config=config,
                    selected=selected,
                    state=shared_state,
                    shared_snapshot=True,
                )
            )
            continue

        state = direct_states.get(market_hash_name)
        if state is None:
            observations.append(
                _selection_watch_observation(
                    config=config,
                    selected=selected,
                    state=None,
                    error="selection market state was not created",
                    event_type="scan_failed",
                )
            )
            continue
        if steam_client is None:
            state.raw_json["steam_orderbook_error"] = (
                "Steam observation client unavailable; no orderbook request was sent"
            )
            observations.append(
                _selection_watch_observation(
                    config=config,
                    selected=selected,
                    state=state,
                )
            )
            continue
        try:
            payload = steam_client.order_book(
                app_id=settings.app_id,
                market_hash_name=market_hash_name,
            )
            if not isinstance(payload, dict):
                raise RuntimeError("Steam orderbook returned a non-object payload")
            snapshot = build_orderbook_snapshot(
                payload,
                observed_at=observed_at,
                depth=5,
                expected_currency=23,
            )
            state.raw_json["steam_orderbook"] = payload
            state.raw_json["steam_orderbook_snapshot"] = snapshot
            if snapshot.get("currencyValid") is False:
                state.raw_json["steam_orderbook_error"] = (
                    "Steam orderbook currency mismatch; CNY (23) is required"
                )
            else:
                decision = choose_orderbook_price(
                    payload,
                    wall_min_count=1,
                    price_offset=0.0,
                )
                if decision is None:
                    state.raw_json["steam_orderbook_error"] = "empty_sell_orderbook"
                else:
                    state.steam_sell_price = float(decision.list_price)
                    state.steam_price_source = "steam_orderbook"
        except Exception as exc:
            message = str(exc)[:1000]
            state.raw_json["steam_orderbook_error"] = message
            is_429 = getattr(exc, "status_code", None) == 429
            observations.append(
                _selection_watch_observation(
                    config=config,
                    selected=selected,
                    state=state,
                    error=message,
                    event_type="scan_failed",
                )
            )
            errors.append(f"{market_hash_name}: {message}")
            if is_429:
                rate_limited = True
                deferred_names.extend(
                    str(row.get("market_hash_name") or "").strip()
                    for row in due_rows[index + 1 :]
                    if str(row.get("market_hash_name") or "").strip()
                )
                # The remaining due rows are already recorded as deferred.
                # Break rather than re-visiting them through the top-of-loop
                # rate-limit branch, which would duplicate their history.
                break
            continue
        observations.append(
            _selection_watch_observation(
                config=config,
                selected=selected,
                state=state,
            )
        )

    db = Database(settings.db_path)
    try:
        db.initialize()
        persisted = db.record_profit_trade_selection_watch_scan(
            observations,
            scan_id=scan_id,
            observed_at=observed_at,
            interval_seconds=PROFIT_TRADE_SELECTION_WATCH_INTERVAL_SECONDS,
        )
        deferred_count = 0
        if deferred_names:
            deferred_count = db.defer_profit_trade_selection_watch_scan(
                deferred_names,
                scan_id=scan_id,
                reason="Steam orderbook rate limited; selection scan deferred without retry",
                next_scan_at=_selection_watch_after(),
                observed_at=observed_at,
            )
        next_due_at = db.next_profit_trade_selection_watch_due_at()
        active_count = db.count_active_profit_trade_selection_watch()
    finally:
        db.close()

    event_logger.emit(
        level="WARNING" if rate_limited else "INFO",
        provider="local",
        component="profit_trade_selection_watch",
        operation="scan_completed",
        message=(
            "Profit Trade selection watch stopped after Steam orderbook HTTP 429"
            if rate_limited
            else "Profit Trade selection watch scan completed"
        ),
        run_id=scan_id,
        safe_context={
            "due_count": len(due_rows),
            "observed_count": len(observations),
            "reused_snapshot_count": reused_snapshot_count,
            "deferred_count": deferred_count,
            "rate_limited": rate_limited,
        },
    )
    return {
        "ok": True,
        "scanId": scan_id,
        "activeCount": active_count,
        "dueCount": len(due_rows),
        "observedCount": len(observations),
        "reusedSnapshotCount": reused_snapshot_count,
        "deferredCount": deferred_count,
        "rateLimited": rate_limited,
        "persisted": persisted,
        "nextDueAt": next_due_at,
        "errors": errors,
    }


def _selection_roi_statistics(items: list[dict[str, Any]]) -> dict[str, Any]:
    values = [safe_float(item.get("expectedRoi")) for item in items]
    roi_values = [float(value) for value in values if value is not None]
    buy_values = [safe_float(item.get("buyOrderReferenceRoi")) for item in items]
    buy_roi_values = [float(value) for value in buy_values if value is not None]
    profit_values = [safe_float(item.get("expectedProfit")) for item in items]
    buy_profit_values = [safe_float(item.get("buyOrderReferenceProfit")) for item in items]
    positive_items = [
        item
        for item in items
        if (safe_float(item.get("expectedRoi")) or 0.0) > 0
    ]
    positive_roi_values = [
        float(value)
        for item in positive_items
        if (value := safe_float(item.get("expectedRoi"))) is not None
    ]
    positive_profit_values = [
        max(0.0, float(value))
        for item in positive_items
        if (value := safe_float(item.get("expectedProfit"))) is not None
    ]
    positive_cost_values = [
        float(steam_price) * float(balance_discount)
        for item in positive_items
        if (steam_price := safe_float(item.get("steamBuyPrice"))) is not None
        and (balance_discount := safe_float(item.get("balanceDiscount"))) is not None
    ]
    available_price_count = sum(
        1
        for item in items
        if safe_float(item.get("steamBuyPrice")) is not None
        and safe_float(item.get("c5ListingPrice")) is not None
    )
    distribution = {"gte20": 0, "gte10": 0, "gte5": 0, "lt5": 0}
    for value in roi_values:
        if value >= 0.20:
            distribution["gte20"] += 1
        elif value >= 0.10:
            distribution["gte10"] += 1
        elif value >= 0.05:
            distribution["gte5"] += 1
        else:
            distribution["lt5"] += 1
    return {
        "observedCount": len(roi_values),
        "availablePriceCount": available_price_count,
        "positiveOpportunityCount": len(positive_items),
        "positiveExpectedProfitTotal": sum(positive_profit_values),
        "positiveExpectedCostTotal": sum(positive_cost_values),
        "averagePositiveRoi": (
            sum(positive_roi_values) / len(positive_roi_values)
            if positive_roi_values
            else None
        ),
        "distribution": distribution,
        "maxExpectedRoi": max(roi_values) if roi_values else None,
        "minExpectedRoi": min(roi_values) if roi_values else None,
        "avgExpectedRoi": sum(roi_values) / len(roi_values) if roi_values else None,
        "maxBuyOrderReferenceRoi": max(buy_roi_values) if buy_roi_values else None,
        "avgBuyOrderReferenceRoi": (
            sum(buy_roi_values) / len(buy_roi_values) if buy_roi_values else None
        ),
        "expectedProfitTotal": sum(float(value) for value in profit_values if value is not None),
        "buyOrderReferenceProfitTotal": sum(
            float(value) for value in buy_profit_values if value is not None
        ),
    }


def _selection_watch_recommendation(item: dict[str, Any]) -> dict[str, str]:
    """Project research-only advice without touching real inventory state."""

    current = safe_float(item.get("expectedRoi"))
    average = safe_float(item.get("averageRoi7d"))
    positive_share = safe_float(item.get("positiveRoiShare7d")) or 0.0
    sample_count = int(safe_float(item.get("validObservationCount7d")) or 0)
    has_error = bool(str(item.get("lastError") or "").strip())
    if (
        current is not None
        and current > 0
        and average is not None
        and average > 0
        and positive_share >= 0.8
        and sample_count >= 12
        and not has_error
    ):
        return {
            "inventoryAdvice": "ready",
            "inventoryAdviceLabel": "可入库",
            "recommendationTone": "stable",
            "recommendationLabel": "稳定推荐",
        }
    if (
        current is not None
        and not has_error
        and ((average is not None and average > 0) or current > 0)
    ):
        return {
            "inventoryAdvice": "watch",
            "inventoryAdviceLabel": "观望",
            "recommendationTone": "observe",
            "recommendationLabel": "继续观察" if current > 0 else "等待回落",
        }
    return {
        "inventoryAdvice": "avoid",
        "inventoryAdviceLabel": "不建议",
        "recommendationTone": "avoid",
        "recommendationLabel": "暂不建议",
    }


def build_profit_trade_selection_watch_payload(
    settings: Settings,
    *,
    active: bool | None = True,
    keyword: str | None = None,
    status: str | None = None,
    sort: str = "roi_desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Return the dedicated all-market selection research pool."""

    db = Database(settings.db_path)
    try:
        db.initialize()
        payload = db.list_profit_trade_selection_watch(
            active=active,
            keyword=keyword,
            status=status,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        summary_rows = db.list_profit_trade_selection_watch(
            active=True,
            sort="roi_desc",
            page=1,
            page_size=PROFIT_TRADE_SELECTION_WATCH_MAX_ITEMS,
        )["items"]
        for item in payload["items"]:
            item.update(_selection_watch_recommendation(item))
        for item in summary_rows:
            item.update(_selection_watch_recommendation(item))
        next_due_at = db.next_profit_trade_selection_watch_due_at()
        active_count = db.count_active_profit_trade_selection_watch()
    finally:
        db.close()
    return sanitize_public_payload(
        {
            "generatedAt": utc_now_iso(),
            "researchOnly": True,
            "canExecute": False,
            "maxActiveItems": PROFIT_TRADE_SELECTION_WATCH_MAX_ITEMS,
            "scanIntervalSeconds": PROFIT_TRADE_SELECTION_WATCH_INTERVAL_SECONDS,
            "maxItemsPerCycle": None,
            "scansAllActiveItems": True,
            "activeCount": active_count,
            "nextDueAt": next_due_at,
            "summary": _selection_roi_statistics(list(summary_rows)),
            **payload,
        }
    )


def build_profit_trade_selection_history_payload(
    settings: Settings,
    market_hash_name: str,
    *,
    from_time: str | None = None,
    to_time: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    db = Database(settings.db_path)
    try:
        db.initialize()
        payload = db.list_profit_trade_selection_history(
            market_hash_name,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
        )
        history_statistics = db.profit_trade_selection_history_statistics(
            market_hash_name,
            from_time=from_time,
            to_time=to_time,
        )
    finally:
        db.close()
    return sanitize_public_payload(
        {
            "generatedAt": utc_now_iso(),
            "researchOnly": True,
            "canExecute": False,
            "marketHashName": str(market_hash_name or "").strip(),
            "stats": {
                "highestRoi": history_statistics.get("highestRoi"),
                "averageRoi": history_statistics.get("averageRoi"),
                "roiBasis": history_statistics.get("roiBasis"),
                "roiBasisMin": history_statistics.get("roiBasisMin"),
                "roiBasisMax": history_statistics.get("roiBasisMax"),
                "validObservationCount": history_statistics.get(
                    "validObservationCount", 0
                ),
            },
            "summary": history_statistics,
            **payload,
        }
    )


def update_profit_trade_selection_watch(
    settings: Settings,
    *,
    action: str,
    market_hash_name: str,
) -> dict[str, Any]:
    """Add, remove, or reactivate an exact local-catalog research selection."""

    normalized_action = str(action or "").strip().lower()
    normalized_name = str(market_hash_name or "").strip()
    if normalized_action not in {"add", "remove", "reactivate"}:
        raise ValueError("action must be add, remove, or reactivate")
    if not normalized_name:
        raise ValueError("marketHashName is required")
    db = Database(settings.db_path)
    try:
        db.initialize()
        if normalized_action in {"add", "reactivate"}:
            catalog_row = db.get_item(normalized_name)
            if catalog_row is None:
                raise ValueError(
                    "marketHashName must be selected from the local catalog; arbitrary item names are not allowed"
                )
            name_cn = str(catalog_row["name_cn"] or normalized_name).strip() or normalized_name
            if normalized_action == "add":
                row, outcome = db.add_profit_trade_selection_watch(
                    normalized_name,
                    name_cn=name_cn,
                )
            else:
                row = db.reactivate_profit_trade_selection_watch(
                    normalized_name,
                    name_cn=name_cn,
                )
                outcome = "reactivated"
        else:
            row = db.remove_profit_trade_selection_watch(normalized_name)
            outcome = "removed"
        item = Database._profit_trade_selection_row_to_dict(row)
    finally:
        db.close()
    return sanitize_public_payload(
        {
            "ok": True,
            "action": outcome,
            "researchOnly": True,
            "canExecute": False,
            "item": item,
        }
    )


def send_profit_trade_daily_report(settings: Settings) -> bool:
    if not settings.serverchan_sendkey:
        raise RuntimeError("缺少 SERVERCHAN_SENDKEY / SCTKEY")
    client = ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url)
    body = render_profit_trade_daily_report(settings)
    client.send("搬砖做T日报", body)
    return True

































