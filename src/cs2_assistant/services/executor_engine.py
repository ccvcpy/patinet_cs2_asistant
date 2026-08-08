from __future__ import annotations

import json
import math
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

from cs2_assistant.accounts import Account, AccountStore
from cs2_assistant.accounts.steam_auth import try_steam_auto_relogin
from cs2_assistant.catalog import is_csgo_api_weapon_case
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
from cs2_assistant.services.executor_buy import RebuyResult, execute_rebuy, is_retryable_c5_network_error
from cs2_assistant.services.c5_ip_circuit import (
    bind_c5_ip_circuit_telemetry,
    build_c5_ip_request_guard,
)
from cs2_assistant.services.market import calculate_listing_ratio, calculate_transfer_real_ratio
from cs2_assistant.services.pricing import (
    PricingDecision,
    choose_orderbook_price,
    fetch_listing_price,
    summarize_orderbook_prices,
)
from cs2_assistant.services.steam_request_scheduler import SteamRequestGuardRejected
from cs2_assistant.services.strategy import classify_strategies, load_strategy_config, scan_strategies
from cs2_assistant.services.t_yield_scan import fetch_all_c5_inventories, summarize_inventory_types
from cs2_assistant.utils import safe_float, safe_int, utc_now_iso


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_timestamp_iso(value: Any) -> str | None:
    """Return Steam/API timestamps as timezone-aware ISO 8601 strings.

    Steam market history commonly returns Unix seconds, while older local
    records may already contain ISO strings.  Persisting the raw integer is
    ambiguous to JavaScript (which interprets numbers as milliseconds) and
    also prevents the Python report path from parsing the official sale time.
    """

    if value in (None, ""):
        return None
    text = str(value).strip()
    numeric: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
    elif text.replace(".", "", 1).isdigit():
        try:
            numeric = float(text)
        except ValueError:
            numeric = None
    if numeric is not None:
        if abs(numeric) >= 1_000_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return text
    parsed = _parse_iso(text)
    if parsed is None:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _guadao_client_telemetry_callback(settings: Settings, **context: Any) -> Any:
    """Bind an explicit guadao source without making logging a dependency."""

    try:
        from cs2_assistant.services.guadao_logging import get_guadao_event_logger

        downstream = get_guadao_event_logger().bind_telemetry(**context)
    except Exception:
        downstream = None
    return bind_c5_ip_circuit_telemetry(
        settings,
        source="guadao",
        downstream=downstream,
    )


LISTING_CONFIRMATION_PENDING_STATUSES = {
    "pending",
    "manual_required",
    "failed",
    "not_found",
    "market_pending_visible",
    "market_pending_remove_failed",
    "confirm_sent_waiting_active_listing",
    "listing_missing_unverified",
}
CASE_NONBLOCKING_LISTING_PENDING_STATUSES = {
    "confirm_sent_waiting_active_listing",
    "listing_missing_unverified",
}
REBUY_NO_MATCHING_TIMEOUT_SECONDS = 3 * 60 * 60
REBUY_ORDER_AUDIT_LOOKBACK_DAYS = 7
C5_DELIVERY_DEADLINE_SECONDS = 12 * 60 * 60
C5_DELIVERY_STATUS_KEY = "c5FinalStatus"
C5_DELIVERY_SUCCESS = "c5_success"
C5_DELIVERY_FAILED = "c5_failed"
C5_SUBMISSION_UNCONFIRMED = "c5_submission_unconfirmed"
C5_SUBMISSION_ABSENCE_CONFIRMATIONS = 3
C5_SUBMISSION_MATCH_WINDOW_SECONDS = 2 * 60
C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET = 100
C5_SUBMISSION_RECONCILE_MAX_PAGE_BUDGET = 1000
C5_SUBMISSION_NOT_CREATED_MAX_CHAIN = 3
REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY = "autoReplacementEligible"
STEAM_LISTING_RETRY_DELAY_SECONDS = 3.0
STEAM_LISTING_SUCCESS_DELAY_SECONDS = 0.0
STEAM_LISTING_MAX_ATTEMPTS = 10
STEAM_LISTING_TRANSIENT_COOLDOWN_SECONDS = 30 * 60
STEAM_LISTING_ACCOUNT_ATTEMPT_INTERVAL_SECONDS = 1.0
STEAM_LISTING_ACCOUNT_BACKOFF_SECONDS = 3 * 60
GUADAO_SCAN_ORDERBOOK_ADMISSION_SECONDS = 90.0
GUADAO_STALE_LISTED_CANCEL_AFTER_SECONDS = 48 * 60 * 60
GUADAO_STALE_LISTED_DEFERRED_RETRY_SECONDS = 10 * 60
STEAM_SALE_RECEIPT_FAST_LOOKUP_MAX_PAGES = 2
STEAM_SALE_RECEIPT_DEEP_LOOKUP_MAX_PAGES = 30
STEAM_SALE_RECEIPT_DEEP_LOOKUP_INITIAL_DELAY_SECONDS = 30 * 60
STEAM_SALE_RECEIPT_DEEP_LOOKUP_INTERVAL_SECONDS = 6 * 60 * 60
CASE_CAPACITY_OBSERVATION_PAYLOAD_KEY = "caseListingCapacityObservation"
EXECUTION_PROCESS_SESSION_ID = uuid.uuid4().hex


class _NewGuadaoActionBlocked(RuntimeError):
    """Internal control-flow signal used before a new real Steam listing."""


@dataclass(frozen=True)
class _SteamSaleReceiptLookupOutcome:
    """Keep Steam-history absence distinct from an unreadable history route."""

    receipts: dict[int, dict[str, Any] | None]
    deep_attempt_ids: set[int]
    deep_attempted_at: str | None
    lookup_succeeded: bool
    coverage_complete: bool
    error: str | None = None
    retry_at: str | None = None
    pages_scanned: int = 0


def _format_decimal(value: Any, *, digits: int = 3) -> str:
    numeric_value = safe_float(value)
    if numeric_value is None:
        return "?"
    return f"{numeric_value:.{digits}f}".rstrip("0").rstrip(".")


def _format_pct(value: Any, *, digits: int = 2) -> str:
    numeric_value = safe_float(value)
    if numeric_value is None:
        return "?"
    return f"{numeric_value * 100:.{digits}f}%"


def _steam_seller_net_from_gross(value: Any, steam_net_factor: float) -> float | None:
    gross = safe_float(value)
    if gross is None or gross <= 0:
        return None
    cents = (
        Decimal(str(gross))
        * Decimal(str(float(steam_net_factor)))
        * Decimal("100")
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(cents / Decimal("100"))


def _steam_account_log_label(note: dict[str, Any]) -> str | None:
    account_name = str(note.get("steamAccountName") or "").strip()
    if account_name:
        return account_name
    steam_id = str(note.get("steamId64") or "").strip()
    return steam_id or None


def _message_indicates_pending_confirmation(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    pending_markers = (
        "等待确认",
        "待确认",
        "请确认",
        "waiting confirmation",
        "waiting for confirmation",
        "awaiting confirmation",
        "confirm or cancel",
        "confirm or remove",
        "already listed",
    )
    return any(marker in normalized for marker in pending_markers)


def _is_transient_listing_error(exc: Exception) -> bool:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        # Steam sellitem's generic retryable failure is a structured
        # success=false JSON response. The message is localized, so do not
        # depend on a specific language.
        message = payload.get("message")
        return (
            payload.get("success") is False
            and isinstance(message, str)
            and not _message_indicates_pending_confirmation(message)
        )
    return False


def _is_pending_confirmation_sellitem_error(exc: Exception) -> bool:
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict) or payload.get("success") is not False:
        return False
    return _message_indicates_pending_confirmation(str(payload.get("message") or ""))


def _is_market_auth_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "400 client error" in message or "401 client error" in message


def _is_market_transport_error(exc: Exception) -> bool:
    message = str(exc).lower()
    tokens = (
        "read timed out",
        "connect timeout",
        "connecttimeout",
        "ssleof",
        "unexpected eof while reading",
        "connection aborted",
        "connection reset",
        "remote disconnected",
        "remotedisconnected",
        "connectionerror",
    )
    return any(token in message for token in tokens)


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


def _extract_c5_order_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    # C5 /buy/detail expects the asset-level order id. In quick-buy payloads
    # that value is named orderAssetId, while orderId can point to a parent
    # trade order that detail reports as "订单不存在".
    for key in ("orderAssetId", "order_asset_id", "assetOrderId", "asset_order_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    for key in ("order", "orderInfo", "trade", "tradeInfo", "data"):
        nested = payload.get(key)
        order_id = _extract_c5_order_id(nested)
        if order_id:
            return order_id
    for key in ("orders", "orderList", "list"):
        nested_list = payload.get(key)
        if not isinstance(nested_list, list):
            continue
        for item in nested_list:
            order_id = _extract_c5_order_id(item)
            if order_id:
                return order_id
    return None


def _extract_c5_trade_order_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("orderId", "order_id", "id", "tradeOrderId", "trade_order_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _c5_submission_credentials(payload: Any) -> tuple[str | None, str | None, int | None]:
    return (
        _extract_c5_order_id(payload),
        _extract_c5_trade_order_id(payload),
        safe_int(payload.get("payStatus")) if isinstance(payload, dict) else None,
    )


def _has_confirmed_c5_submission(payload: Any) -> bool:
    asset_order_id, trade_order_id, _pay_status = _c5_submission_credentials(payload)
    # payStatus describes payment/delivery progress, not whether C5 created the
    # order. Two real identifiers are sufficient existence evidence; the
    # detail endpoint owns the terminal-state decision.
    return bool(asset_order_id and trade_order_id)


def _has_confirmed_c5_order_note(note: dict[str, Any]) -> bool:
    has_asset_id = bool(str(note.get("c5OrderId") or "").strip())
    has_trade_id = bool(str(note.get("c5TradeOrderId") or "").strip())
    # A safe unique buyer/status match may expose only the asset lookup id
    # until buyer/detail becomes readable. Preserve that recognized order
    # instead of migrating it back to submission absence reconciliation.
    return bool(
        (has_asset_id and has_trade_id)
        or (note.get("c5OrderRecognized") and has_asset_id)
    )


def _c5_buyer_status_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("list", "records", "rows", "orderList"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        return _c5_buyer_status_rows(data)
    return []


def _c5_buyer_status_has_list(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if any(isinstance(payload.get(key), list) for key in ("list", "records", "rows", "orderList")):
        return True
    return _c5_buyer_status_has_list(payload.get("data"))


def _c5_buyer_status_pages(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    pages = safe_int(payload.get("pages"))
    if pages is not None:
        return pages
    return _c5_buyer_status_pages(payload.get("data"))


def _c5_buyer_row_asset_order_id(row: dict[str, Any]) -> str | None:
    # buyer/status commonly exposes its asset-level identifier as ``orderId``.
    # quick-buy uses ``orderAssetId`` for the same lookup identifier.
    return _extract_c5_order_id(row) or (
        str(row.get("orderId")) if row.get("orderId") not in (None, "") else None
    )


def _c5_buyer_row_trade_order_id(row: dict[str, Any]) -> str | None:
    value = (
        row.get("tradeOrderId")
        or row.get("trade_order_id")
        or row.get("parentOrderId")
        or row.get("parent_order_id")
    )
    if value not in (None, ""):
        return str(value)
    # When both explicit quick-buy names are present, orderId is the trade id.
    if _extract_c5_order_id(row) and row.get("orderId") not in (None, ""):
        return str(row.get("orderId"))
    return None


def _is_c5_insufficient_balance_rebuy_result(result: RebuyResult) -> bool:
    payload = getattr(result, "payload", None)
    if isinstance(payload, dict):
        if safe_int(payload.get("errorCode")) == 70001:
            return True
        error_message = str(payload.get("errorMsg") or payload.get("message") or "")
        if "余额不足" in error_message or "insufficient balance" in error_message.lower():
            return True
    reason = str(getattr(result, "reason", "") or "")
    return "余额不足" in reason or "insufficient balance" in reason.lower()


def _parse_c5_error_payload(exc: Exception) -> dict[str, Any] | None:
    message = str(exc).strip()
    if not message.startswith("{"):
        return None
    try:
        payload = json.loads(message)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _c5_order_lookup_ids(note: dict[str, Any]) -> list[str]:
    ids: list[str] = []

    def add(value: Any) -> None:
        if value in (None, ""):
            return
        text = str(value).strip()
        if text and text not in ids:
            ids.append(text)

    add(note.get("c5OrderId"))
    payload = note.get("c5OrderPayload")
    if isinstance(payload, dict):
        add(_extract_c5_order_id(payload))
        add(payload.get("orderId"))
    add(note.get("c5TradeOrderId"))
    return ids


def _c5_order_detail_market_hash_name(payload: dict[str, Any]) -> str:
    open_item = payload.get("openItemInfo")
    if isinstance(open_item, dict):
        value = open_item.get("marketHashName") or open_item.get("name")
        if value:
            return str(value)
    return str(payload.get("marketHashName") or payload.get("name") or "").strip()


def _c5_delivery_final_status(detail: dict[str, Any]) -> str | None:
    status = safe_int(detail.get("status"))
    status_name = str(detail.get("statusName") or "").strip().lower()
    failed_code = str(detail.get("failedCode") or "").strip()
    failed_desc = str(detail.get("failedDesc") or "").strip()
    if status == 11 or status_name in {"failed", "fail", "failure", "失败", "已失败"} or failed_code or failed_desc:
        return C5_DELIVERY_FAILED
    if status == 10 or status_name in {
        "success",
        "succeeded",
        "complete",
        "completed",
        "finished",
        "done",
        "delivered",
        "received",
        "已完成",
        "已收货",
        "发货成功",
        "成功",
    }:
        return C5_DELIVERY_SUCCESS
    return None


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


@dataclass(slots=True)
class GuadaoAssetTarget:
    asset_id: str
    steam_id64: str | None
    account: Account | None


@dataclass(slots=True)
class GuadaoListingPlan:
    candidate: StrategyCandidate
    decision: ListingDecision
    target: GuadaoAssetTarget
    client: SteamMarketClient
    steam_id64: str
    account: Account | None
    recent_sold_count: int


@dataclass(slots=True)
class GuadaoAccountInventoryInfo:
    candidate: StrategyCandidate
    total_available: int
    configured_available: int
    unconfigured_available: int
    account_counts: list[tuple[str, int]]
    missing_steam_account_count: int


@dataclass(slots=True)
class ListingDeferState:
    op_id: int | None
    deferred_until: datetime
    defer_count: int
    reason: str


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        config: StrategyConfig | None = None,
        *,
        account: Account | str | None = None,
        dry_run_override: bool | None = None,
        force_refresh_override: bool | None = None,
        new_action_guard: Callable[[], bool] | None = None,
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
        c5_telemetry_context = {
            "account_id": self.account.id if self.account else None,
            "steam_id64": self.account.steam_id64 if self.account else None,
        }
        self.c5_client = C5GameClient(
            self._c5_api_key,
            settings.c5_base_url,
            telemetry_callback=_guadao_client_telemetry_callback(
                settings,
                **c5_telemetry_context,
            ),
            telemetry_context={"source": "guadao", **c5_telemetry_context},
            request_guard=build_c5_ip_request_guard(settings),
        )
        self.serverchan = (
            ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url)
            if settings.serverchan_sendkey
            else None
        )

        self.steam_client = None
        if (self.config.execution_enabled or self.config.auto_list_enabled) and self._steam_cookies:
            self.steam_client = SteamMarketClient(
                cookies=self._steam_cookies,
                steam_id64=self.account.steam_id64 if self.account else None,
                identity_secret=self._steam_identity_secret,
                device_id=self._steam_device_id,
                account_id=self.account.id if self.account else None,
                base_url=settings.steam_market_base_url,
                request_source="guadao",
            )
        self._steam_clients: dict[str, SteamMarketClient] = {}
        self._cache_steam_client(self.steam_client, self.account)

        self._last_inventory_payload: dict[str, Any] = {}
        self._inventory_items_by_asset_id: dict[str, dict[str, Any]] = {}
        self._pending_confirmation_count = 0
        self._market_pending_cleanup_failed_count = 0
        self._stop_requested = False
        self._stop_reason: str | None = None
        self._new_action_guard = new_action_guard
        self._case_open_guadao_limit_notified = False
        self._process_session_id = EXECUTION_PROCESS_SESSION_ID
        self._rebuy_wait_started_at: dict[int, datetime] = {}
        self._recent_rebuy_delivery_failures_checked = False
        self._listing_account_next_attempt_at: dict[str, float] = {}
        self._listing_account_backoff_until: dict[str, datetime] = {}
        self._steam_market_validated_accounts: set[str] = set()
        self._startup_account_cookies_refreshed = False

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

    def _emit_guadao_local_event(
        self,
        *,
        operation: str,
        message: str,
        level: str = "INFO",
        market_hash_name: str | None = None,
        operation_id: int | None = None,
        asset_id: str | None = None,
        note: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Write a fail-open state event while preserving existing CLI output."""

        try:
            from cs2_assistant.services.guadao_logging import get_guadao_event_logger

            source_note = dict(note or {})
            normalized_operation_id = safe_int(operation_id)
            if normalized_operation_id is not None and normalized_operation_id <= 0:
                normalized_operation_id = None
            account_id = source_note.get("steamAccountId")
            account_name = source_note.get("steamAccountName")
            steam_id64 = source_note.get("steamId64")
            if not account_id and getattr(self, "account", None) is not None:
                account_id = self.account.id
                account_name = account_name or self.account.name
                steam_id64 = steam_id64 or self.account.steam_id64
            safe_context = {
                "operationId": normalized_operation_id,
                "accountName": account_name,
                **dict(context or {}),
            }
            get_guadao_event_logger().emit(
                level=level,
                provider="local",
                component="executor_engine",
                operation=operation,
                message=message,
                trade_id=normalized_operation_id,
                trade_no=f"GD-{normalized_operation_id}" if normalized_operation_id is not None else None,
                market_hash_name=market_hash_name,
                asset_id=asset_id,
                account_id=account_id,
                steam_id64=steam_id64,
                safe_context=safe_context,
            )
        except Exception:
            # Logging must never change a trading transition or retry decision.
            return

    def _listing_account_key(self, client: SteamMarketClient | None) -> str | None:
        if client is None:
            return None
        steam_id64 = str(getattr(client, "steam_id64", "") or "").strip()
        if steam_id64:
            return steam_id64
        account_id = str(getattr(client, "account_id", "") or "").strip()
        if account_id:
            return f"account:{account_id}"
        return None

    def _active_listing_account_backoff(self, client: SteamMarketClient | None) -> datetime | None:
        if not hasattr(self, "_listing_account_backoff_until"):
            self._listing_account_backoff_until = {}
        key = self._listing_account_key(client)
        if not key:
            return None
        until = self._listing_account_backoff_until.get(key)
        if until is None:
            return None
        now = _now_utc()
        if until <= now:
            self._listing_account_backoff_until.pop(key, None)
            return None
        return until

    def _set_listing_account_backoff(
        self,
        client: SteamMarketClient | None,
        *,
        seconds: float = STEAM_LISTING_ACCOUNT_BACKOFF_SECONDS,
    ) -> datetime | None:
        if not hasattr(self, "_listing_account_backoff_until"):
            self._listing_account_backoff_until = {}
        if not hasattr(self, "_listing_account_next_attempt_at"):
            self._listing_account_next_attempt_at = {}
        key = self._listing_account_key(client)
        if not key:
            return None
        until = _now_utc() + timedelta(seconds=max(1.0, float(seconds)))
        self._listing_account_backoff_until[key] = until
        next_attempt = time.monotonic() + max(1.0, float(seconds))
        existing_next_attempt = self._listing_account_next_attempt_at.get(key, 0.0)
        self._listing_account_next_attempt_at[key] = max(existing_next_attempt, next_attempt)
        return until

    def _wait_for_listing_account_slot(self, client: SteamMarketClient | None) -> None:
        if not hasattr(self, "_listing_account_next_attempt_at"):
            self._listing_account_next_attempt_at = {}
        key = self._listing_account_key(client)
        if not key:
            return
        next_allowed_at = self._listing_account_next_attempt_at.get(key, 0.0)
        now_monotonic = time.monotonic()
        if next_allowed_at > now_monotonic:
            time.sleep(next_allowed_at - now_monotonic)
        self._listing_account_next_attempt_at[key] = time.monotonic() + STEAM_LISTING_ACCOUNT_ATTEMPT_INTERVAL_SECONDS

    def _rebuild_primary_steam_client(self) -> None:
        store = getattr(self, "account_store", None)
        if store and hasattr(store, "get_current"):
            try:
                current = store.get_current()
            except Exception:
                current = None
            if current is not None:
                self.account = current

        if self.account:
            self._steam_cookies = self.account.cookies
            self._steam_identity_secret = self.account.identity_secret
            self._steam_device_id = self.account.device_id
            self._steam_trade_url = self.account.trade_url
        else:
            self._steam_cookies = self.settings.steam_cookies
            self._steam_identity_secret = self.settings.steam_identity_secret
            self._steam_device_id = self.settings.steam_device_id

        self.steam_client = None
        self._steam_clients = {}
        self._steam_market_validated_accounts = set()
        if not (self.config.execution_enabled or self.config.auto_list_enabled):
            return
        if not self._steam_cookies:
            return
        try:
            self.steam_client = SteamMarketClient(
                cookies=self._steam_cookies,
                steam_id64=self.account.steam_id64 if self.account else None,
                identity_secret=self._steam_identity_secret,
                device_id=self._steam_device_id,
                account_id=self.account.id if self.account else None,
                base_url=self.settings.steam_market_base_url,
                request_source="guadao",
            )
        except SteamMarketError as exc:
            print(f"[警告] 当前账号 Steam client 重建失败，将在实际使用时再尝试自动登录: {exc}")
            self.steam_client = None
        self._cache_steam_client(self.steam_client, self.account)

    def _refresh_all_account_cookies_on_startup(self) -> None:
        if getattr(self, "_startup_account_cookies_refreshed", False):
            return
        accounts = self._all_accounts()
        if not accounts:
            self._startup_account_cookies_refreshed = True
            return

        refreshed = 0
        failed = 0
        skipped = 0
        for account in accounts:
            if not account.username or not account.password:
                skipped += 1
                continue
            try:
                ok, status, updated = try_steam_auto_relogin(
                    self.account_store,
                    account_id=account.id,
                    force_login=True,
                )
            except Exception as exc:
                ok = False
                status = str(exc)
                updated = None
            if ok and updated is not None and updated.cookies:
                refreshed += 1
                continue
            failed += 1
            print(f"[账号] 启动刷新失败: {account.name} | {status}")

        self._rebuild_primary_steam_client()
        self._startup_account_cookies_refreshed = True
        print(
            f"[账号] 启动已刷新 Steam cookies | 成功 {refreshed} | 失败 {failed} | "
            f"跳过 {skipped}"
        )

    def run(self, *, once: bool = False) -> None:
        self._refresh_all_account_cookies_on_startup()
        while True:
            try:
                self.run_once(wait_for_cycle=False)
            except C5GameError as exc:
                if once or not is_retryable_c5_network_error(exc):
                    raise
                print(f"[警告] C5 网络临时断开，本轮跳过，下一轮继续: {exc}")
            if self._stop_requested:
                if self._stop_reason:
                    print(f"[停止] {self._stop_reason}")
                return
            if once:
                return
            # 兼容旧 CLI。正式常驻运行由 8765 后端的持久化任务 worker
            # 持有；这里不再进入挂刀内部 while/sleep 等待状态闭环。
            schedule = self.config.effective_guadao_task_schedule()
            time.sleep(max(1.0, float(schedule["scanIntervalSeconds"])))

    def run_once(self, *, wait_for_cycle: bool = True) -> None:
        # wait_for_cycle 仅保留调用兼容；run-once 永远只推进一个有限轮次。
        # 旧的阻塞等待会造成卖出检测和补仓在两个分支重复执行。
        _ = wait_for_cycle
        self._pending_confirmation_count = 0
        self._market_pending_cleanup_failed_count = 0
        self._steam_market_validated_accounts = set()
        self._sync_assets()
        if not getattr(self, "_recent_rebuy_delivery_failures_checked", False):
            self._recent_rebuy_delivery_failures_checked = True
            self._check_recent_rebuy_delivery_failures()
        pool_names = self.db.get_pool_market_hash_names()
        if not pool_names:
            print("底仓为空，且当前 C5 库存未同步到可执行品种，跳过执行。")
            return
        weapon_case_market_hash_names = {
            market_hash_name for market_hash_name in pool_names if self._is_weapon_case(market_hash_name)
        }
        scan_pool_names = self._pool_names_for_strategy_scan(pool_names)

        self._refresh_transfer_holdings()
        report = scan_strategies(
            self.settings,
            self.config,
            allow_cached_fallback=True,
            cache_max_age_minutes=180,
            pool_market_hash_names=scan_pool_names,
            inventory_payload=self._last_inventory_payload,
            weapon_case_market_hash_names=weapon_case_market_hash_names,
        )
        self._refresh_scan_listing_prices_from_steam(report)
        self._print_scan_summary(report)
        self._print_guadao_account_inventory_summary(report)
        self._guadao_skipped_by_account = []

        listed, sold, rebought = self._run_guadao_cycle(report)
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
        if self._market_pending_cleanup_failed_count > 0:
            print(
                f"[提醒] {self._market_pending_cleanup_failed_count} 件 Steam 网页待确认挂单无法自动撤下，"
                "请在 Steam 市场等待确认列表手动撤下；此状态运行 steam confirm 通常无效"
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
        if self.config.max_transfer_buy_per_cycle <= 0:
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

    def _account_label_map(self) -> tuple[dict[str, str], list[str]]:
        steam_id_to_name: dict[str, str] = {}
        missing_steam_id_accounts: list[str] = []
        for account in self._all_accounts():
            steam_id = str(account.steam_id64 or "").strip()
            if steam_id:
                steam_id_to_name[steam_id] = account.name
            else:
                missing_steam_id_accounts.append(account.name)
        return steam_id_to_name, missing_steam_id_accounts

    def _guadao_account_inventory_infos(self, report: Any) -> list[GuadaoAccountInventoryInfo]:
        candidates = [
            candidate
            for candidate in list(getattr(report, "guadao_candidates", []) or [])
            if self._guadao_scope_allows_market_hash_name(candidate.market_hash_name)
        ]
        if not candidates:
            return []

        steam_id_to_name, missing_steam_id_accounts = self._account_label_map()
        infos: list[GuadaoAccountInventoryInfo] = []
        for candidate in candidates:
            assets = self.db.list_assets(
                market_hash_name=candidate.market_hash_name,
                tradable=True,
                status="available",
                exclude_reserved=True,
            )
            counts_by_steam_id: dict[str, int] = {}
            unknown_count = 0
            for asset in assets:
                steam_id = str(asset["steam_id"] or "").strip()
                if not steam_id:
                    unknown_count += 1
                    continue
                counts_by_steam_id[steam_id] = counts_by_steam_id.get(steam_id, 0) + 1

            account_counts: list[tuple[str, int]] = []
            configured_available = 0
            unconfigured_available = unknown_count
            for steam_id, count in sorted(counts_by_steam_id.items()):
                account_name = steam_id_to_name.get(steam_id)
                if account_name:
                    configured_available += count
                    account_counts.append((account_name, count))
                else:
                    unconfigured_available += count

            account_counts.sort(key=lambda item: (-item[1], item[0]))
            infos.append(
                GuadaoAccountInventoryInfo(
                    candidate=candidate,
                    total_available=len(assets),
                    configured_available=configured_available,
                    unconfigured_available=unconfigured_available,
                    account_counts=account_counts,
                    missing_steam_account_count=len(missing_steam_id_accounts),
                )
            )
        return infos

    def _format_account_counts(self, account_counts: list[tuple[str, int]], *, limit: int = 4) -> str:
        if not account_counts:
            return "四个账号均为 0" if len(self._all_accounts()) == 4 else "已配置账号均为 0"
        parts = [f"{name} {count}件" for name, count in account_counts[:limit]]
        if len(account_counts) > limit:
            parts.append("等")
        return "、".join(parts)

    def _print_guadao_account_inventory_summary(self, report: Any) -> None:
        infos = self._guadao_account_inventory_infos(report)
        if not infos:
            return

        accounts = self._all_accounts()
        configured_account_count = len(accounts)
        missing_steam_id_accounts = [account.name for account in accounts if not str(account.steam_id64 or "").strip()]
        executable_infos = [info for info in infos if info.configured_available > 0]
        threshold_pct = float(self.config.guadao_max_listing_ratio) * 100.0
        print(
            f"[账号库存] 挂刀候选 {len(infos)} 个 | "
            f"已配置 Steam 账号 {configured_account_count} 个 | "
            f"本地有可上架资产的候选 {len(executable_infos)} 个 | "
            f"挂刀阈值 {threshold_pct:.2f}%"
        )

        if missing_steam_id_accounts:
            sample = "、".join(missing_steam_id_accounts[:3])
            suffix = " 等" if len(missing_steam_id_accounts) > 3 else ""
            print(
                f"[账号库存] {len(missing_steam_id_accounts)} 个账号缺少 steam_id64，"
                f"无法匹配库存归属：{sample}{suffix}"
            )

        for info in infos[:10]:
            candidate = info.candidate
            ratio_pct = float(candidate.listing_ratio_pct)
            account_summary = self._format_account_counts(info.account_counts)
            if info.configured_available > 0:
                prefix = "可上架"
            else:
                prefix = "不可上架"
            print(
                f"[账号库存] {prefix} {candidate.market_hash_name} | "
                f"预计比例 {ratio_pct:.2f}% | "
                f"C5聚合可交易 {candidate.tradable_count}件 | "
                f"本地可上架 {info.configured_available}件：{account_summary}"
            )
        if len(infos) > 10:
            print(f"[账号库存] 其余 {len(infos) - 10} 个挂刀候选已省略。")

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
                exclude_reserved=True,
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

    def _all_accounts(self) -> list[Account]:
        store = getattr(self, "account_store", None)
        if not store:
            return []
        if not hasattr(store, "list_accounts"):
            return []
        try:
            return list(store.list_accounts())
        except Exception as exc:
            print(f"[警告] 读取本地 Steam 账号配置失败: {exc}")
            return []

    def _account_by_steam_id64(self, steam_id64: str | None) -> Account | None:
        steam_id = str(steam_id64 or "").strip()
        if not steam_id:
            return None
        for account in self._all_accounts():
            if str(account.steam_id64 or "").strip() == steam_id:
                return account
        return None

    def _account_by_id(self, account_id: str | None) -> Account | None:
        lookup = str(account_id or "").strip()
        if not lookup:
            return None
        store = getattr(self, "account_store", None)
        if not store:
            return None
        try:
            return store.get_account(lookup)
        except Exception as exc:
            print(f"[警告] 读取本地 Steam 账号配置失败: {exc}")
            return None

    def _new_listing_account_is_allowed(self, account: Account | None) -> bool:
        """Exclude a runtime-degraded account from *new* guadao listings.

        The runtime tables are optional for legacy CLI/test callers.  With no
        persisted Cookie health rows, existing behavior is preserved.  Once
        the unified runtime has established account health, only ``valid``
        accounts may contribute assets to a new listing plan.  Existing state
        synchronization and C5 closure paths do not call this helper.
        """

        if account is None:
            return True
        loader = getattr(self.db, "list_steam_cookie_health", None)
        if not callable(loader):
            return True
        try:
            rows = list(loader())
        except Exception:
            # A health-store read failure must not invent a false "valid"
            # result once the runtime health table is in use.
            return False
        if not rows:
            return True
        row = next((item for item in rows if str(item["account_id"]) == account.id), None)
        return row is not None and str(row["status"] or "").lower() == "valid"

    def _steam_client_cache(self) -> dict[str, SteamMarketClient]:
        cache = getattr(self, "_steam_clients", None)
        if cache is None:
            cache = {}
            self._steam_clients = cache
            self._cache_steam_client(getattr(self, "steam_client", None), getattr(self, "account", None))
        return cache

    def _cache_steam_client(self, client: SteamMarketClient | None, account: Account | None = None) -> None:
        if client is None:
            return
        cache = getattr(self, "_steam_clients", None)
        if cache is None:
            cache = {}
            self._steam_clients = cache
        steam_id = str(getattr(client, "steam_id64", "") or "").strip()
        if steam_id:
            cache[f"steam:{steam_id}"] = client
        if account is not None:
            cache[f"account:{account.id}"] = client

    def _steam_market_validation_cache(self) -> set[str]:
        cache = getattr(self, "_steam_market_validated_accounts", None)
        if cache is None:
            cache = set()
            self._steam_market_validated_accounts = cache
        return cache

    def _steam_market_validation_key(
        self,
        client: SteamMarketClient | None,
        account: Account | None,
    ) -> str | None:
        if account is not None and account.id:
            return f"account:{account.id}"
        steam_id = str(getattr(client, "steam_id64", "") or "").strip()
        if steam_id:
            return f"steam:{steam_id}"
        return None

    def _ensure_market_client_ready(
        self,
        client: SteamMarketClient | None,
        account: Account | None,
    ) -> SteamMarketClient | None:
        if client is None:
            return None
        validation_key = self._steam_market_validation_key(client, account)
        validated = self._steam_market_validation_cache()
        if validation_key and validation_key in validated:
            return client

        probe = getattr(client, "my_listings", None)
        if not callable(probe):
            if validation_key:
                validated.add(validation_key)
            return client

        try:
            payload = probe(count=1)
            if isinstance(payload, dict):
                success = payload.get("success")
                if success not in (1, True):
                    raise SteamMarketError(json.dumps(payload, ensure_ascii=False), payload=payload)
        except Exception as exc:
            account_name = account.name if account else str(getattr(client, "steam_id64", "") or "-")
            if _is_market_auth_error(exc):
                print(f"[跳过] Steam账号 {account_name} Market 会话无效，自动刷新后仍不可用: {exc}")
                return None
            if _is_market_transport_error(exc):
                print(f"[账号] Steam Market 校验遇到网络波动，继续使用现有会话: {account_name} | {exc}")
            else:
                print(f"[账号] Steam Market 校验返回异常，继续尝试执行: {account_name} | {exc}")
        if validation_key:
            validated.add(validation_key)
        return client

    def _cached_steam_client(
        self,
        account: Account | None,
        steam_id64: str | None,
    ) -> SteamMarketClient | None:
        cache = self._steam_client_cache()
        keys: list[str] = []
        if account is not None:
            keys.append(f"account:{account.id}")
        expected_steam_id = str(steam_id64 or (account.steam_id64 if account else "") or "").strip()
        if expected_steam_id:
            keys.append(f"steam:{expected_steam_id}")
        for key in keys:
            client = cache.get(key)
            if client is None:
                continue
            client_steam_id = str(getattr(client, "steam_id64", "") or "").strip()
            if expected_steam_id and client_steam_id != expected_steam_id:
                continue
            return client
        return None

    @staticmethod
    def _steam_client_relogin_policy_matches(
        client: SteamMarketClient | None,
        allow_relogin: bool,
    ) -> bool:
        """Keep cached clients from widening a no-relogin maintenance read.

        Older injected/fake clients do not expose the private policy flag; in
        that compatibility case there is no internal relogin path to widen and
        the cache remains usable.  Production ``SteamMarketClient`` instances
        always expose the flag, so a stale-listing read cannot reuse a client
        created by a normal sync path with relogin enabled.
        """

        configured = getattr(client, "_allow_account_relogin", None)
        if configured is None:
            return True
        return bool(configured) == bool(allow_relogin)

    def _steam_client_matches(self, steam_id64: str | None, account: Account | None = None) -> bool:
        if not self.steam_client:
            return False
        current_steam_id = str(getattr(self.steam_client, "steam_id64", "") or "").strip()
        expected_steam_id = str(steam_id64 or "").strip()
        if expected_steam_id and current_steam_id != expected_steam_id:
            return False
        if account is not None and self.account is not None and self.account.id != account.id:
            return False
        return True

    def _steam_client_for_account(
        self,
        account: Account | None,
        steam_id64: str | None = None,
        *,
        validate_session: bool = True,
        allow_relogin: bool = True,
    ) -> SteamMarketClient | None:
        expected_steam_id = str(steam_id64 or (account.steam_id64 if account else "") or "").strip() or None
        cached = self._cached_steam_client(account, expected_steam_id)
        if cached is not None and self._steam_client_relogin_policy_matches(
            cached,
            allow_relogin,
        ):
            return self._ensure_market_client_ready(cached, account) if validate_session else cached

        if account is None:
            if (
                self._steam_client_matches(expected_steam_id)
                and self._steam_client_relogin_policy_matches(
                    self.steam_client,
                    allow_relogin,
                )
            ):
                self._cache_steam_client(self.steam_client, self.account)
                return self._ensure_market_client_ready(self.steam_client, self.account) if validate_session else self.steam_client
            print(f"[跳过] steam={expected_steam_id or '-'} 未匹配到 config/accounts.json 中的 Steam 账号。")
            return None
        if not getattr(self, "account_store", None):
            print(f"[跳过] Steam账号 {account.name} 无账号存储上下文，无法创建 Steam client。")
            return None

        refreshed_account = account
        if not refreshed_account.cookies:
            if not allow_relogin:
                return None
            ok, status, updated = try_steam_auto_relogin(
                self.account_store,
                account_id=refreshed_account.id,
                force_login=True,
            )
            if not ok or updated is None or not updated.cookies:
                print(f"[跳过] Steam账号 {refreshed_account.name} 无可用 cookie，自动 relogin 失败: {status}")
                return None
            refreshed_account = updated

        try:
            client = SteamMarketClient(
                cookies=refreshed_account.cookies,
                steam_id64=refreshed_account.steam_id64 or expected_steam_id,
                identity_secret=refreshed_account.identity_secret,
                device_id=refreshed_account.device_id,
                account_id=refreshed_account.id,
                base_url=self.settings.steam_market_base_url,
                request_source="guadao",
                allow_account_relogin=allow_relogin,
            )
        except SteamMarketError as exc:
            if not allow_relogin:
                return None
            ok, status, updated = try_steam_auto_relogin(
                self.account_store,
                account_id=refreshed_account.id,
                force_login=True,
            )
            if not ok or updated is None or not updated.cookies:
                print(f"[跳过] Steam账号 {refreshed_account.name} 初始化失败且 relogin 失败: {exc} | {status}")
                return None
            try:
                client = SteamMarketClient(
                    cookies=updated.cookies,
                    steam_id64=updated.steam_id64 or expected_steam_id,
                    identity_secret=updated.identity_secret,
                    device_id=updated.device_id,
                    account_id=updated.id,
                    base_url=self.settings.steam_market_base_url,
                    request_source="guadao",
                    allow_account_relogin=allow_relogin,
                )
                refreshed_account = updated
            except SteamMarketError as relogin_exc:
                print(f"[跳过] Steam账号 {refreshed_account.name} relogin 后仍无法初始化: {relogin_exc}")
                return None

        if validate_session:
            client = self._ensure_market_client_ready(client, refreshed_account)
        if client is None:
            return None
        # A no-relogin maintenance client is intentionally not cached.  The
        # normal account-sync client may need to refresh a later 400/401, and a
        # one-off safety read must not permanently disable that behavior.
        if allow_relogin:
            self._cache_steam_client(client, refreshed_account)
        print(
            f"[账号] 已准备 Steam client: {refreshed_account.name} | "
            f"steam={getattr(client, 'steam_id64', expected_steam_id) or '-'}"
        )
        return client

    def _prepare_steam_client_for_account(self, account: Account | None, steam_id64: str | None = None) -> bool:
        return self._steam_client_for_account(account, steam_id64) is not None

    def _steam_id64_for_client(self, client: SteamMarketClient | None) -> str | None:
        steam_id = str(getattr(client, "steam_id64", "") or "").strip()
        return steam_id or None

    def _operation_matches_steam_account(self, op: Any, steam_id64: str | None) -> bool:
        op_steam_id = self._operation_steam_id64(op)
        if not op_steam_id:
            return True
        expected_steam_id = str(steam_id64 or "").strip()
        return bool(expected_steam_id) and expected_steam_id == op_steam_id

    def _operation_matches_client(self, op: Any, client: SteamMarketClient | None) -> bool:
        return self._operation_matches_steam_account(op, self._steam_id64_for_client(client))

    def _account_note_fields(self, account: Account | None, steam_id64: str | None) -> dict[str, Any]:
        return {
            "steamAccountId": account.id if account else None,
            "steamAccountName": account.name if account else None,
            "steamId64": str(steam_id64 or "").strip() or None,
        }

    def _find_guadao_asset_target(
        self,
        candidate: StrategyCandidate,
        *,
        steam_id64: str | None = None,
        exclude_asset_ids: set[str] | None = None,
    ) -> GuadaoAssetTarget | None:
        blocked_asset_ids = set(exclude_asset_ids or set())
        while True:
            asset_row = self.db.pick_tradable_asset(
                candidate.market_hash_name,
                steam_id=steam_id64,
                exclude_asset_ids=blocked_asset_ids,
            )
            if asset_row is None:
                return None
            asset_id = str(asset_row["asset_id"])
            asset_steam_id = str(asset_row["steam_id"] or "").strip() or None
            account = self._account_by_steam_id64(asset_steam_id)
            if account is None and self._steam_client_matches(asset_steam_id):
                account = self.account
            if self._new_listing_account_is_allowed(account):
                return GuadaoAssetTarget(
                    asset_id=asset_id,
                    steam_id64=asset_steam_id,
                    account=account,
                )
            blocked_asset_ids.add(asset_id)

    def _operation_steam_id64(self, op: Any) -> str | None:
        note = _read_note(op["note"])
        value = note.get("steamId64") or note.get("steamId")
        if value:
            return str(value).strip() or None
        if op["asset_id"]:
            asset = self.db.get_asset(str(op["asset_id"]))
            if asset is not None:
                return str(asset["steam_id"] or "").strip() or None
        return None

    def _operation_matches_current_steam_account(self, op: Any) -> bool:
        return self._operation_matches_client(op, getattr(self, "steam_client", None))

    def _prepare_steam_client_for_open_guadao_operation(self) -> bool:
        for op in self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=200):
            steam_id = self._operation_steam_id64(op)
            if self._operation_matches_current_steam_account(op):
                return True
            account = self._account_by_id(_read_note(op["note"]).get("steamAccountId")) or self._account_by_steam_id64(steam_id)
            if self._prepare_steam_client_for_account(account, steam_id):
                return True
        for op in self.db.list_pool_operations_by_type(OP_SELL_STEAM, status=POOL_STATUS_LISTING_PENDING, limit=200):
            steam_id = self._operation_steam_id64(op)
            if self._operation_matches_current_steam_account(op):
                return True
            account = self._account_by_id(_read_note(op["note"]).get("steamAccountId")) or self._account_by_steam_id64(steam_id)
            if self._prepare_steam_client_for_account(account, steam_id):
                return True
        return bool(self.steam_client)

    def _open_guadao_steam_targets(self) -> list[tuple[str | None, Account | None]]:
        targets: list[tuple[str | None, Account | None]] = []
        seen: set[str] = set()
        for status in ("listed", POOL_STATUS_LISTING_PENDING):
            for op in self.db.list_pool_operations_by_type(OP_SELL_STEAM, status=status, limit=200):
                note = _read_note(op["note"])
                steam_id = self._operation_steam_id64(op)
                account = self._account_by_id(note.get("steamAccountId")) or self._account_by_steam_id64(steam_id)
                key = steam_id or (account.id if account else "")
                if not key:
                    key = "__current__"
                if key in seen:
                    continue
                seen.add(key)
                targets.append((steam_id, account))
        return targets

    def _latest_listing_operation(self, asset_id: str, *, statuses: tuple[str, ...]) -> Any | None:
        if not statuses:
            return None
        placeholders = ", ".join("?" for _ in statuses)
        return self.db.conn.execute(
            f"""
            SELECT id, note
            FROM pool_operations
            WHERE operation_type = ?
              AND asset_id = ?
              AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            (OP_SELL_STEAM, asset_id, *statuses),
        ).fetchone()

    def _latest_listing_defer_state(self, asset_id: str) -> ListingDeferState | None:
        row = self._latest_listing_operation(asset_id, statuses=("deferred",))
        if row is None:
            return None
        note = _read_note(row["note"])
        deferred_until = _parse_iso(str(note.get("deferredUntil") or "").strip())
        if deferred_until is None:
            return None
        if deferred_until.tzinfo is None:
            deferred_until = deferred_until.replace(tzinfo=timezone.utc)
        return ListingDeferState(
            op_id=safe_int(row["id"]),
            deferred_until=deferred_until,
            defer_count=max(1, safe_int(note.get("deferCount")) or 1),
            reason=str(note.get("deferReason") or "transient_sellitem_failure"),
        )

    def _active_listing_defer_state(self, asset_id: str, *, now: datetime | None = None) -> ListingDeferState | None:
        state = self._latest_listing_defer_state(asset_id)
        if state is None:
            return None
        now = now or _now_utc()
        if state.deferred_until <= now:
            return None
        return state

    def _record_listing_defer(
        self,
        *,
        candidate: StrategyCandidate,
        asset_id: str,
        price: float,
        account: Account | None,
        steam_id64: str | None,
        error: Exception,
        reason: str,
        cooldown_seconds: float = STEAM_LISTING_TRANSIENT_COOLDOWN_SECONDS,
    ) -> ListingDeferState:
        previous = self._latest_listing_defer_state(asset_id)
        defer_count = (previous.defer_count if previous is not None else 0) + 1
        now = _now_utc()
        deferred_until = now + timedelta(seconds=max(1.0, float(cooldown_seconds)))
        note = _build_note(
            {
                "assetId": asset_id,
                "marketHashName": candidate.market_hash_name,
                "steamListPrice": price,
                "deferReason": reason,
                "deferMessage": str(error),
                "deferCount": defer_count,
                "deferredAt": now.isoformat(),
                "deferredUntil": deferred_until.isoformat(),
                **self._guadao_open_ratio_note_from_price(candidate, price),
                **self._account_note_fields(account, steam_id64),
            }
        )
        if previous is not None and previous.op_id is not None:
            self.db.update_pool_operation(previous.op_id, status="deferred", note=note)
            op_id = previous.op_id
        else:
            op_id = self.db.add_pool_operation(
                market_hash_name=candidate.market_hash_name,
                strategy=candidate.primary_strategy,
                operation_type=OP_SELL_STEAM,
                expected_price=price,
                asset_id=asset_id,
                note=note,
            )
            self.db.update_pool_operation(op_id, status="deferred", note=note)
        return ListingDeferState(
            op_id=op_id,
            deferred_until=deferred_until,
            defer_count=defer_count,
            reason=reason,
        )

    def _record_listing_transient_defer(
        self,
        *,
        candidate: StrategyCandidate,
        asset_id: str,
        price: float,
        account: Account | None,
        steam_id64: str | None,
        error: Exception,
    ) -> ListingDeferState:
        return self._record_listing_defer(
            candidate=candidate,
            asset_id=asset_id,
            price=price,
            account=account,
            steam_id64=steam_id64,
            error=error,
            reason="transient_sellitem_failure",
        )

    def _record_listing_pending_confirmation_from_sellitem(
        self,
        *,
        candidate: StrategyCandidate,
        asset_id: str,
        price: float,
        account: Account | None,
        steam_id64: str | None,
        message: str,
        client: SteamMarketClient | None = None,
    ) -> tuple[int, str]:
        pending_count_before = self._pending_confirmation_count
        confirmation_note, status_after = self._handle_listing_confirmation(
            market_hash_name=candidate.market_hash_name,
            asset_id=asset_id,
            listing_id="",
            client=client,
            account=account,
        )
        confirmation_note["confirmationSource"] = "sellitem_pending_confirmation"
        confirmation_note["sellitemPendingMessage"] = message
        if status_after == POOL_STATUS_LISTED:
            op_status = "listed"
            asset_status = "listed"
        elif status_after == POOL_STATUS_HOLDING and confirmation_note.get("confirmationStatus") == "market_pending_removed":
            op_status = "canceled"
            asset_status = "available"
        else:
            op_status = POOL_STATUS_LISTING_PENDING
            asset_status = "listing_pending"
        note_dict = {
            "assetId": asset_id,
            "marketHashName": candidate.market_hash_name,
            "listingId": None,
            "rebuyPrice": candidate.rebuy_price,
            "steamListPrice": price,
            "steamSellerNetPrice": _steam_seller_net_from_gross(price, self.config.steam_net_factor),
            "strategy": candidate.primary_strategy,
            "needsConfirmation": True,
            "listingPendingAt": utc_now_iso(),
            **self._guadao_open_ratio_note_from_price(candidate, price),
            **self._account_note_fields(account, steam_id64),
            **confirmation_note,
        }
        note = _build_note(note_dict)
        previous = self._latest_listing_operation(
            asset_id,
            statuses=(POOL_STATUS_LISTING_PENDING, "deferred"),
        )
        if previous is not None:
            op_id = safe_int(previous["id"]) or 0
            if op_id:
                self.db.update_pool_operation(op_id, status=op_status, note=note)
            else:
                op_id = 0
        else:
            op_id = self.db.add_pool_operation(
                market_hash_name=candidate.market_hash_name,
                strategy=candidate.primary_strategy,
                operation_type=OP_SELL_STEAM,
                expected_price=price,
                asset_id=asset_id,
                note=note,
            )
            self.db.update_pool_operation(op_id, status=op_status, note=note)
        self.db.set_asset_status(asset_id, asset_status)
        if op_status == "canceled":
            if not self._has_other_open_guadao_operation(candidate.market_hash_name, exclude_op_id=op_id):
                self.db.set_pool_status(candidate.market_hash_name, POOL_STATUS_HOLDING)
        else:
            self.db.set_pool_status(candidate.market_hash_name, status_after)
        if op_status in {"listed", POOL_STATUS_LISTING_PENDING}:
            expected_net = _steam_seller_net_from_gross(price, self.config.steam_net_factor)
            self._emit_guadao_local_event(
                operation=(
                    "steam_listing_active"
                    if op_status == "listed"
                    else "steam_listing_pending_confirmation"
                ),
                message=(
                    "Steam 挂刀已确认活跃"
                    if op_status == "listed"
                    else "Steam 挂刀已提交，等待确认或活跃状态"
                ),
                market_hash_name=candidate.market_hash_name,
                operation_id=op_id,
                asset_id=asset_id,
                note=note_dict,
                context={
                    "state": op_status,
                    "steamListPrice": price,
                    "steamExpectedNetAmount": expected_net,
                    "listingRatio": note_dict.get("listingRatioAtOpen"),
                    "listingId": note_dict.get("listingId"),
                },
            )
        if status_after == POOL_STATUS_LISTING_PENDING and client is not None and op_id:
            if self._mark_pending_sell_operation_sold_if_receipted(
                op_id=op_id,
                note=note_dict,
                client=client,
                asset_id=asset_id,
            ):
                self._pending_confirmation_count = pending_count_before
                return op_id, POOL_STATUS_PENDING_REBUY
        return op_id, status_after

    def _mark_pending_sell_operation_sold_if_receipted(
        self,
        *,
        op_id: int,
        note: dict[str, Any],
        client: SteamMarketClient | None,
        asset_id: str,
        max_pages: int = STEAM_SALE_RECEIPT_FAST_LOOKUP_MAX_PAGES,
    ) -> bool:
        if not client or not op_id:
            return False
        listing_id = str(note.get("listingId") or "").strip()
        sale_receipt = self._lookup_steam_sale_receipt_for_listing_or_asset(
            client,
            listing_id=listing_id,
            asset_id=asset_id,
            max_pages=max_pages,
        )
        if sale_receipt is None:
            return False
        existing_rebuy_sources, existing_rebuy_sell_ops = self._load_existing_rebuy_source_keys()
        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?",
            (op_id,),
        ).fetchone()
        if op is None:
            return False
        self._mark_steam_listing_sold(
            op,
            note,
            sale_receipt=sale_receipt,
            existing_rebuy_sources=existing_rebuy_sources,
            existing_rebuy_sell_ops=existing_rebuy_sell_ops,
        )
        return True

    def _refresh_scan_listing_prices_from_steam(self, report: Any) -> int:
        if not self.steam_client or not self.config.auto_list_enabled:
            return 0

        refreshed = 0
        for candidate in list(getattr(report, "all_evaluated", []) or []):
            if not self._guadao_scope_allows_market_hash_name(candidate.market_hash_name):
                continue
            pricing = fetch_listing_price(
                self.steam_client,
                app_id=self.settings.app_id,
                market_hash_name=candidate.market_hash_name,
                wall_min_count=self._listing_wall_min_count_for_market_hash_name(candidate.market_hash_name),
                price_offset=self._listing_price_offset_for_market_hash_name(candidate.market_hash_name),
                min_price=0.01,
                country=self.config.steam_country,
                language=self.config.steam_language,
                currency=self.config.steam_currency,
                force_refresh=self.config.force_refresh_before_execution,
                cache_ttl=self.config.steam_price_cache_ttl,
            )
            if pricing is None:
                continue

            decision = self._decision_from_list_price(candidate, pricing.list_price, pricing=pricing)
            if decision is None:
                continue

            candidate.steam_sell_price = decision.list_price
            candidate.steam_price_source = "steam_orderbook"
            candidate.steam_after_tax_price = decision.list_price * self.config.steam_net_factor
            candidate.listing_ratio = decision.listing_ratio
            candidate.transfer_real_ratio = decision.transfer_real_ratio
            candidate.recommended_strategies = classify_strategies(
                decision.listing_ratio,
                decision.transfer_real_ratio,
                self.config,
                is_weapon_case=self._is_weapon_case(candidate.market_hash_name),
            )
            refreshed += 1

        if refreshed:
            self._rebuild_strategy_report_lists(report)
        return refreshed

    def _rebuild_strategy_report_lists(self, report: Any) -> None:
        all_evaluated = list(getattr(report, "all_evaluated", []) or [])
        guadao_candidates = [
            candidate for candidate in all_evaluated if STRATEGY_GUADAO in candidate.recommended_strategies
        ]
        transfer_candidates = [
            candidate for candidate in all_evaluated if STRATEGY_TRANSFER in candidate.recommended_strategies
        ]
        hold_items = [candidate for candidate in all_evaluated if not candidate.recommended_strategies]

        guadao_candidates.sort(key=lambda candidate: candidate.listing_ratio)
        transfer_candidates.sort(key=lambda candidate: candidate.transfer_real_ratio, reverse=True)
        all_evaluated.sort(key=lambda candidate: candidate.listing_ratio)

        report.guadao_candidates = guadao_candidates
        report.transfer_candidates = transfer_candidates
        report.hold_items = hold_items
        report.all_evaluated = all_evaluated

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
        pool_status_map = self.db.get_pool_status_map()
        open_statuses = self._get_open_guadao_statuses()
        if open_statuses and self._has_open_guadao_cycle(pool_status_map):
            summary = ", ".join(
                f"{status}={count}" for status, count in sorted(open_statuses.items())
            )
            reasons.append(f"挂刀执行存在阻塞状态（{summary}），本轮仅做等待和状态检查。")

        inventory_type_names = self._current_inventory_type_names()
        missing_inventory_names = sorted(
            [
                name
                for name in pool_names
                if name not in inventory_type_names
                and pool_status_map.get(name, POOL_STATUS_HOLDING) == POOL_STATUS_HOLDING
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
        account_inventory_summary = self._guadao_account_inventory_no_action_reason(report)
        if account_inventory_summary:
            reasons.append(account_inventory_summary)
        lowest_listing_summary = self._lowest_listing_ratio_reason(report)
        if lowest_listing_summary:
            reasons.append(lowest_listing_summary)
        lowest_local_listing_summary = self._lowest_local_available_listing_ratio_reason(report)
        if lowest_local_listing_summary and lowest_local_listing_summary not in reasons:
            reasons.append(lowest_local_listing_summary)
        if evaluated and not guadao_candidates and not transfer_candidates:
            reasons.append(f"已评估 {len(evaluated)} 个品种，但都未满足 list/transfer 阈值。")

        if guadao_candidates and all(int(candidate.tradable_count) <= 0 for candidate in guadao_candidates):
            reasons.append("存在挂刀候选，但当前没有可交易库存，无法上架。")

        if guadao_candidates and self.config.max_list_per_cycle <= 0:
            reasons.append("本轮 `max-list=0`，已禁用新的 Steam 上架。")

        if transfer_candidates and self.config.max_transfer_buy_per_cycle <= 0:
            reasons.append("本轮 `max-transfer-buy=0`，已禁用 transfer 买入动作。")

        return reasons

    def _guadao_account_inventory_no_action_reason(self, report: Any) -> str | None:
        infos = self._guadao_account_inventory_infos(report)
        if not infos:
            return None
        executable_infos = [info for info in infos if info.configured_available > 0]
        account_count = len(self._all_accounts())
        if not executable_infos:
            aggregate_available = sum(info.total_available for info in infos)
            c5_tradable = sum(max(0, int(info.candidate.tradable_count)) for info in infos)
            account_text = f"{account_count} 个已配置 Steam 账号" if account_count else "本地已同步账号"
            if aggregate_available > 0:
                return (
                    f"挂刀候选 {len(infos)} 个，但 {account_text} 里没有匹配到可直接执行的资产；"
                    f"C5 聚合可交易合计 {c5_tradable} 件。"
                )
            return (
                f"挂刀候选 {len(infos)} 个，但 {account_text} 的本地可上架资产都是 0；"
                f"C5 聚合可交易合计 {c5_tradable} 件，不代表当前已配置账号能上架。"
            )

        best = min(executable_infos, key=lambda info: float(info.candidate.listing_ratio))
        best_candidate = best.candidate
        threshold_pct = float(self.config.guadao_max_listing_ratio) * 100.0
        best_ratio_pct = float(best_candidate.listing_ratio_pct)
        account_summary = self._format_account_counts(best.account_counts)
        if best_candidate.listing_ratio <= self.config.guadao_max_listing_ratio:
            return (
                f"当前账号可上架的最低品类是 {best_candidate.market_hash_name}，"
                f"比例 {best_ratio_pct:.2f}%，不高于阈值 {threshold_pct:.2f}%，"
                f"本地可上架 {best.configured_available} 件：{account_summary}；"
                "本轮未上架请看上方是否有状态闭环、账号冷却、Steam 取价或上架失败日志。"
            )
        return (
            f"当前账号可上架的最低品类是 {best_candidate.market_hash_name}，"
            f"比例 {best_ratio_pct:.2f}%，高于阈值 {threshold_pct:.2f}%，"
            f"本地可上架 {best.configured_available} 件：{account_summary}。"
        )

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

    def _lowest_local_available_listing_ratio_reason(self, report: Any) -> str | None:
        local_candidates: list[Any] = []
        for candidate in list(getattr(report, "all_evaluated", []) or []):
            if not self._guadao_scope_allows_market_hash_name(candidate.market_hash_name):
                continue
            assets = self.db.list_assets(
                market_hash_name=candidate.market_hash_name,
                tradable=True,
                status="available",
                exclude_reserved=True,
            )
            if assets:
                local_candidates.append(candidate)

        if not local_candidates:
            return None

        best_candidate = min(local_candidates, key=lambda candidate: float(candidate.listing_ratio))
        threshold_pct = float(self.config.guadao_max_listing_ratio) * 100.0
        best_ratio_pct = float(best_candidate.listing_ratio_pct)
        if best_candidate.listing_ratio <= self.config.guadao_max_listing_ratio:
            return None

        return (
            f"本地可执行资产里最低品类是 {best_candidate.market_hash_name}，"
            f"预计挂刀比例为 {best_ratio_pct:.2f}%，高于配置挂刀阈值 {threshold_pct:.2f}%，"
            "所以本轮未上架。"
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

    def _is_trade_url_for_steam_id(self, trade_url: str | None, steam_id64: str | None) -> bool:
        expected_steam_id = str(steam_id64 or "").strip()
        trade_url_steam_id = _steam_id64_from_trade_url(trade_url)
        if not expected_steam_id or not trade_url_steam_id:
            return True
        return expected_steam_id == trade_url_steam_id

    def _run_guadao_cycle(self, report: Any) -> tuple[int, int, int]:
        status_map = self.db.get_pool_status_map()
        listed = 0
        sold = 0
        rebought = 0

        if self._has_open_guadao_cycle(status_map):
            print("[等待] 检测到挂刀硬阻断状态，或广义箱子风险占用槽已满，本轮暂停新上架。")
        else:
            active_count = self._open_case_guadao_count()
            unverified_count = self._nonblocking_case_listing_pending_count()
            occupied_count = active_count + unverified_count
            if occupied_count > 0:
                print(
                    f"[继续] 广义箱子风险占用槽 {occupied_count}/{self._case_max_open_guadao_count()} "
                    f"（确认在售 {active_count}，待确认/终态复查 {unverified_count}），"
                    "未达上限，本轮继续开启新挂刀。"
                )
            listed = self._execute_guadao_listings(report, status_map)

        sold_delta, rebought_delta = self._advance_guadao_cycle()
        sold += sold_delta
        rebought += rebought_delta
        self._release_full_case_listing_capacity()

        return listed, sold, rebought

    def _advance_guadao_cycle(self) -> tuple[int, int]:
        sold = 0
        targets = self._open_guadao_steam_targets()
        if not targets:
            targets = [(str(getattr(self.steam_client, "steam_id64", "") or "") or None, self.account)]
        for steam_id, account in targets:
            client = self._steam_client_for_account(account, steam_id)
            if client is None:
                continue
            self._refresh_pending_listing_confirmations(client=client)
            self._backfill_listing_ids(client=client)
            sold += self._refresh_listings(client=client)
        rebought = self._execute_rebuys()
        return sold, rebought

    def _minimum_action_confirmation_seconds(self) -> float:
        schedule = self.config.effective_guadao_task_schedule()
        values = schedule.get("actionConfirmationDelaysSeconds") or [10.0]
        normalized = [float(value) for value in values if float(value) >= 0]
        return max(2.0, min(normalized or [10.0]))

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
        failed_rebuy_ops = [op for op in failed_rebuy_ops if self._failed_rebuy_counts_as_open(op)]
        if listed_sell_ops:
            counts["sell_on_steam.listed"] = len(listed_sell_ops)
        if pending_rebuy_ops:
            counts["rebuy_on_c5.pending"] = len(pending_rebuy_ops)
        if failed_rebuy_ops:
            counts["rebuy_on_c5.failed"] = len(failed_rebuy_ops)
        case_open_count = self._open_case_guadao_count()
        if case_open_count:
            counts["case_open_guadao.active_listings"] = case_open_count
        unverified_count = self._listing_missing_unverified_case_guadao_count()
        if unverified_count:
            counts["case_open_guadao.listing_missing_unverified"] = unverified_count
        confirm_sent_count = self._confirm_sent_waiting_case_guadao_count()
        if confirm_sent_count:
            counts["case_open_guadao.confirm_sent_waiting_active"] = confirm_sent_count
        nonblocking_pending_count = unverified_count + confirm_sent_count
        if nonblocking_pending_count:
            counts["case_open_guadao.nonblocking_listing_pending"] = (
                nonblocking_pending_count
            )
        occupied_count = case_open_count + nonblocking_pending_count
        if occupied_count:
            counts["case_open_guadao.occupied_slots"] = occupied_count
        return counts

    def _has_open_guadao_cycle(self, status_map: dict[str, str] | None = None) -> bool:
        current_status_map = status_map or self.db.get_pool_status_map()
        for market_hash_name, status in current_status_map.items():
            if status not in {
                POOL_STATUS_LISTING_PENDING,
                POOL_STATUS_LISTED,
                POOL_STATUS_PENDING_REBUY,
                POOL_STATUS_REBUY_FAILED,
            }:
                continue
            if not self._is_weapon_case(market_hash_name):
                return True

        if self._has_non_case_open_guadao_operation():
            return True

        if any(
            status == POOL_STATUS_REBUY_FAILED and self._is_weapon_case(market_hash_name)
            for market_hash_name, status in current_status_map.items()
        ):
            return True
        if self._has_blocking_case_listing_pending_operation(current_status_map):
            return True

        occupied_count = self._occupied_case_guadao_slot_count()
        if self._case_open_guadao_limit_reached(occupied_count):
            self._notify_case_open_guadao_limit(occupied_count)
            return True
        return False

    def _case_max_open_guadao_count(self) -> int:
        return max(0, int(self.config.case_max_open_guadao_count))

    def _open_case_guadao_count(self) -> int:
        count = 0
        limit = max(500, self._case_max_open_guadao_count() + 10)
        for op in self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=limit):
            if not self._is_weapon_case(op["market_hash_name"]):
                continue
            quantity = safe_int(op["quantity"]) or 1
            count += max(1, quantity)
        return count

    def _listing_missing_unverified_case_guadao_count(self) -> int:
        return self._case_listing_pending_count_for_confirmation_statuses(
            {"listing_missing_unverified"}
        )

    def _confirm_sent_waiting_case_guadao_count(self) -> int:
        return self._case_listing_pending_count_for_confirmation_statuses(
            {"confirm_sent_waiting_active_listing"}
        )

    def _nonblocking_case_listing_pending_count(self) -> int:
        return self._case_listing_pending_count_for_confirmation_statuses(
            CASE_NONBLOCKING_LISTING_PENDING_STATUSES
        )

    def _case_listing_pending_count_for_confirmation_statuses(
        self,
        confirmation_statuses: set[str],
    ) -> int:
        count = 0
        limit = max(500, self._case_max_open_guadao_count() + 10)
        for op in self.db.list_pool_operations_by_type(
            OP_SELL_STEAM,
            status=POOL_STATUS_LISTING_PENDING,
            limit=limit,
        ):
            if not self._is_weapon_case(op["market_hash_name"]):
                continue
            note = _read_note(op["note"])
            if (
                str(note.get("confirmationStatus") or "").strip()
                not in confirmation_statuses
            ):
                continue
            quantity = safe_int(op["quantity"]) or 1
            count += max(1, quantity)
        return count

    def _occupied_case_guadao_slot_count(self) -> int:
        """Return slots that must be reserved before opening another crates listing.

        A listing whose remote terminal state is missing still consumes capacity risk,
        but it is not proof of an active Steam listing and therefore must not be used
        by the three-hour full-capacity release timer.
        """

        return (
            self._open_case_guadao_count()
            + self._nonblocking_case_listing_pending_count()
        )

    def _has_blocking_case_listing_pending_operation(
        self,
        status_map: dict[str, str],
    ) -> bool:
        """Keep unknown case listing states blocking except the bounded slot exception."""

        limit = max(500, self._case_max_open_guadao_count() + 10)
        pending_names: set[str] = set()
        for op in self.db.list_pool_operations_by_type(
            OP_SELL_STEAM,
            status=POOL_STATUS_LISTING_PENDING,
            limit=limit,
        ):
            market_hash_name = str(op["market_hash_name"] or "").strip()
            if not market_hash_name or not self._is_weapon_case(market_hash_name):
                continue
            pending_names.add(market_hash_name)
            note = _read_note(op["note"])
            if (
                str(note.get("confirmationStatus") or "").strip()
                not in CASE_NONBLOCKING_LISTING_PENDING_STATUSES
            ):
                return True

        # A pool-level pending state without its operation evidence is inconsistent.
        # It must stay blocked rather than being silently treated as a reserved slot.
        return any(
            status == POOL_STATUS_LISTING_PENDING
            and self._is_weapon_case(market_hash_name)
            and market_hash_name not in pending_names
            for market_hash_name, status in status_map.items()
        )

    def _has_non_case_open_guadao_operation(self) -> bool:
        for operation_type, status in (
            (OP_SELL_STEAM, "listed"),
            (OP_REBUY_C5, "pending"),
            (OP_REBUY_C5, "failed"),
        ):
            for op in self.db.list_pool_operations_by_type(operation_type, status=status, limit=500):
                if operation_type == OP_REBUY_C5 and status == "failed" and not self._failed_rebuy_counts_as_open(op):
                    continue
                if not self._is_weapon_case(op["market_hash_name"]):
                    return True
        return False

    def _case_open_guadao_limit_reached(self, count: int | None = None) -> bool:
        count = self._occupied_case_guadao_slot_count() if count is None else count
        reached = count > 0 and count >= self._case_max_open_guadao_count()
        if not reached:
            self._case_open_guadao_limit_notified = False
        return reached

    def _notify_case_open_guadao_limit(self, count: int) -> None:
        limit = self._case_max_open_guadao_count()
        message = f"广义箱子风险占用槽已达到 {count}/{limit} 个，已暂停新上架；程序会继续每轮扫描卖出和补仓。"
        if getattr(self, "_case_open_guadao_limit_notified", False):
            return
        self._case_open_guadao_limit_notified = True
        print(f"[提醒] {message}")
        serverchan = getattr(self, "serverchan", None)
        if not serverchan:
            return
        try:
            serverchan.send(
                "[挂刀暂停] 广义箱子风险占用槽已满",
                (
                    f"广义箱子风险占用槽: {count}/{limit}\n"
                    "状态: 已暂停新上架，程序仍会继续扫描卖出和补仓\n"
                    "处理: 已发送确认但尚未见活跃挂单、以及 listing_missing_unverified "
                    "都只保留风险槽并继续后台复查；只有 Steam 远端确认在售也满载，"
                    f"并连续 {self.config.case_full_release_after_hours:g} 小时后，才随机撤销 "
                    f"{self.config.case_full_release_fraction * 100:g}% 的远端活跃挂单"
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
        if inventory_source != "cache":
            self.db.delete_assets_absent_from_live_inventory(set(self._inventory_items_by_asset_id))
        self.db.sync_pool_from_inventory(
            summarize_inventory_types(items),
            zero_missing_holding=inventory_source != "cache",
        )
        self._reconcile_transfer_buys()

    def _decide_listing(
        self,
        candidate: StrategyCandidate,
        *,
        client: SteamMarketClient | None = None,
    ) -> ListingDecision | None:
        active_client = client or self.steam_client
        if not active_client:
            return None
        if (
            not self.config.dry_run
            and str(candidate.steam_price_source or "") != "steam_orderbook"
        ):
            print(
                f"[上架跳过] {candidate.market_hash_name} | "
                "扫描阶段没有取得 Steam orderbook 官方卖盘价"
            )
            return None
        scan_price = safe_float(candidate.steam_sell_price)
        if scan_price is None or scan_price <= 0:
            return None
        # The scan already parsed the official orderbook with this item's
        # actual wall/offset rule. Reuse that snapshot here. The irreversible
        # listing boundary still performs exactly one force-refresh below.
        pricing = PricingDecision(
            list_price=float(scan_price),
            wall_price=None,
            reason="scan_orderbook_snapshot",
        )
        return self._decision_from_list_price(candidate, pricing.list_price, pricing=pricing)

    def _guadao_scan_orderbook_price(
        self,
        market_hash_name: str,
        payload: dict[str, Any],
    ) -> PricingDecision | None:
        return choose_orderbook_price(
            payload,
            wall_min_count=self._listing_wall_min_count_for_market_hash_name(
                market_hash_name
            ),
            price_offset=self._listing_price_offset_for_market_hash_name(
                market_hash_name
            ),
            min_price=0.01,
        )

    def _is_weapon_case(self, market_hash_name: str) -> bool:
        item = self.db.get_item(market_hash_name)
        if item is not None:
            raw_json = _read_note(item["raw_json"])
            if isinstance(raw_json.get("csgoApi"), dict):
                return is_csgo_api_weapon_case(raw_json)
            if _looks_like_weapon_case_name(str(item["market_hash_name"])):
                return True
            if _looks_like_weapon_case_name(str(item["name_cn"])):
                return True
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

    def _decide_listing_compat(
        self,
        candidate: StrategyCandidate,
        *,
        client: SteamMarketClient | None,
    ) -> ListingDecision | None:
        try:
            return self._decide_listing(candidate, client=client)
        except TypeError as exc:
            if "client" not in str(exc):
                raise
            # Some tests monkeypatch _decide_listing with the historical
            # one-argument call shape.
            return self._decide_listing(candidate)

    def _finalize_listing_decision(
        self,
        candidate: StrategyCandidate,
        decision: ListingDecision,
        *,
        client: SteamMarketClient,
    ) -> ListingDecision | None:
        if not self.config.force_refresh_before_execution:
            return decision

        price_offset = self._listing_price_offset_for_candidate(candidate)
        final_pricing = fetch_listing_price(
            client,
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
                return None
            print(
                f"[上架定价] {candidate.market_hash_name} | "
                "force_refresh 实时取价失败，dry-run 沿用扫描价/缓存价继续判断"
            )
            final_pricing = decision.pricing

        return self._decision_from_list_price(
            candidate,
            final_pricing.list_price if final_pricing is not None else decision.list_price,
            pricing=final_pricing,
        )

    def _guadao_open_ratio_note(
        self,
        candidate: StrategyCandidate,
        decision: ListingDecision | None,
    ) -> dict[str, Any]:
        listing_ratio = safe_float(decision.listing_ratio if decision is not None else None)
        if listing_ratio is None:
            listing_ratio = safe_float(candidate.listing_ratio)
        special_rule = self.config.guadao_special_ratio_rule_for(candidate.market_hash_name)
        hard_max_ratio = self.config.guadao_max_listing_ratio_for(candidate.market_hash_name)
        rule_fields = {
            "guadaoRatioRuleSource": "special_case" if special_rule else "global",
            "guadaoRatioRuleId": special_rule.get("ruleId") if special_rule else None,
            "guadaoRatioRuleVersion": special_rule.get("version") if special_rule else None,
        }
        if listing_ratio is None or listing_ratio <= 0:
            return {
                "guadaoMaxListingRatioAtOpen": hard_max_ratio,
                "steamNetFactorAtOpen": self.config.steam_net_factor,
                **rule_fields,
            }
        max_rebuy_ratio = listing_ratio
        if hard_max_ratio is not None and hard_max_ratio > 0:
            max_rebuy_ratio = min(max_rebuy_ratio, hard_max_ratio)
        return {
            "listingRatioAtOpen": listing_ratio,
            "maxRebuyRatioAtOpen": max_rebuy_ratio,
            "guadaoMaxListingRatioAtOpen": hard_max_ratio,
            "steamNetFactorAtOpen": self.config.steam_net_factor,
            **rule_fields,
        }

    def _guadao_open_ratio_note_from_price(
        self,
        candidate: StrategyCandidate,
        price: float,
    ) -> dict[str, Any]:
        return self._guadao_open_ratio_note(
            candidate,
            self._decision_from_list_price(candidate, price, pricing=None),
        )

    def _rebuy_max_listing_ratio_for_note(self, note: dict[str, Any]) -> float:
        frozen_ratio = safe_float(note.get("maxRebuyRatioAtOpen"))
        if frozen_ratio is None:
            frozen_ratio = safe_float(note.get("listingRatioAtOpen"))
        if frozen_ratio is not None and frozen_ratio > 0:
            # A user-approved per-operation refreeze explicitly replaces the
            # original hard ceiling for this sold-but-not-yet-rebought flow.
            # The old values remain in ``manualRebuyRefreezeHistory``; the
            # global strategy and unrelated operations are unchanged.
            if note.get("manualRebuyRefrozenAt"):
                return frozen_ratio
            hard_max_at_open = safe_float(note.get("guadaoMaxListingRatioAtOpen"))
            if hard_max_at_open is not None and hard_max_at_open > 0:
                return min(frozen_ratio, hard_max_at_open)
            return frozen_ratio
        return float(self.config.guadao_max_listing_ratio)

    def _recent_guadao_sold_count(
        self,
        market_hash_name: str,
        *,
        lookback_minutes: int = 60,
    ) -> int:
        cutoff = _now_utc() - timedelta(minutes=max(1, lookback_minutes))
        count = 0
        for op in self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="sold", limit=1000):
            if str(op["market_hash_name"]) != market_hash_name:
                continue
            note = _read_note(op["note"])
            sold_at = _parse_iso(str(note.get("steamSoldAt") or "")) or _parse_iso(op["completed_at"])
            if sold_at is None:
                continue
            if sold_at.tzinfo is None:
                sold_at = sold_at.replace(tzinfo=timezone.utc)
            if sold_at >= cutoff:
                count += 1
        return count

    def _build_guadao_listing_plans(
        self,
        candidates: list[StrategyCandidate],
        *,
        status_map: dict[str, str],
        picked_asset_ids: set[str],
    ) -> list[GuadaoListingPlan]:
        plans: list[GuadaoListingPlan] = []
        for candidate in candidates:
            if getattr(self, "_stop_requested", False):
                break
            if self._is_weapon_case(candidate.market_hash_name):
                occupied_count = self._occupied_case_guadao_slot_count()
                if self._case_open_guadao_limit_reached(occupied_count):
                    self._notify_case_open_guadao_limit(occupied_count)
                    break
            if not self._can_execute_guadao(status_map.get(candidate.market_hash_name)):
                continue
            if candidate.tradable_count <= 0:
                continue

            target = self._find_guadao_asset_target(
                candidate,
                exclude_asset_ids=picked_asset_ids,
            )
            while target is not None:
                client = self._steam_client_for_account(target.account, target.steam_id64)
                if client is not None:
                    break
                picked_asset_ids.add(target.asset_id)
                target = self._find_guadao_asset_target(
                    candidate,
                    exclude_asset_ids=picked_asset_ids,
                )
            if target is None:
                continue
            client = self._steam_client_for_account(target.account, target.steam_id64)
            if client is None:
                continue

            decision = self._decide_listing_compat(candidate, client=client)
            if decision is None:
                continue
            decision = self._finalize_listing_decision(candidate, decision, client=client)
            if decision is None:
                continue
            if decision.listing_ratio > self.config.guadao_max_listing_ratio_for(
                candidate.market_hash_name
            ):
                continue

            steam_id64 = str(getattr(client, "steam_id64", "") or target.steam_id64 or "").strip()
            if not steam_id64:
                continue
            plans.append(
                GuadaoListingPlan(
                    candidate=candidate,
                    decision=decision,
                    target=target,
                    client=client,
                    steam_id64=steam_id64,
                    account=target.account or self.account,
                    recent_sold_count=self._recent_guadao_sold_count(candidate.market_hash_name),
                )
            )

        plans.sort(
            key=lambda plan: (
                float(plan.decision.listing_ratio),
                -plan.recent_sold_count,
                plan.candidate.market_hash_name,
            )
        )
        return plans

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
        if not self.config.auto_list_enabled:
            return 0
        if self._has_open_guadao_cycle(status_map):
            return 0

        list_count = 0
        picked_asset_ids: set[str] = set()
        selected_steam_id64: str | None = None
        selected_account: Account | None = None
        defer_logged_asset_ids: set[str] = set()
        # 本轮内 (market_hash_name, reason) 维度的去重，避免同饰品在同一轮里重复推送跳过通知
        self._notified_listing_skips_cycle: set[tuple[str, str]] = set()
        candidates = [
            candidate
            for candidate in report.guadao_candidates
            if candidate.primary_strategy == STRATEGY_GUADAO
            and self._guadao_scope_allows_market_hash_name(candidate.market_hash_name)
        ]
        plans = self._build_guadao_listing_plans(
            candidates,
            status_map=status_map,
            picked_asset_ids=picked_asset_ids,
        )

        for plan in plans:
            candidate = plan.candidate
            decision = plan.decision
            target = plan.target
            client = plan.client
            if getattr(self, "_stop_requested", False):
                break
            if list_count >= self.config.max_list_per_cycle:
                break
            if self._is_weapon_case(candidate.market_hash_name):
                occupied_count = self._occupied_case_guadao_slot_count()
                if self._case_open_guadao_limit_reached(occupied_count):
                    self._notify_case_open_guadao_limit(occupied_count)
                    break
            if not self._can_execute_guadao(status_map.get(candidate.market_hash_name)):
                continue
            if candidate.tradable_count <= 0:
                continue

            if selected_steam_id64 is None:
                selected_steam_id64 = plan.steam_id64
                selected_account = plan.account
            elif plan.steam_id64 != selected_steam_id64:
                target = self._find_guadao_asset_target(
                    candidate,
                    steam_id64=selected_steam_id64,
                    exclude_asset_ids=picked_asset_ids,
                )
                if target is None:
                    continue
                client = self._steam_client_for_account(target.account, target.steam_id64)
                if client is None:
                    continue
                selected_account = target.account or selected_account or self.account

            if not selected_steam_id64:
                continue
            account_backoff_until = self._active_listing_account_backoff(client)
            if account_backoff_until is not None:
                local_retry_at = account_backoff_until.astimezone(timezone(timedelta(hours=8)))
                account_name = selected_account.name if selected_account else selected_steam_id64
                print(
                    f"[账号节流] {candidate.market_hash_name} | "
                    f"账号={account_name} | "
                    f"Steam 上架接口正在冷却，等待到 {local_retry_at.strftime('%Y-%m-%d %H:%M:%S')} 后再试"
                )
                continue
            wait_before_next_listing = False
            while list_count < self.config.max_list_per_cycle:
                account_backoff_until = self._active_listing_account_backoff(client)
                if account_backoff_until is not None:
                    local_retry_at = account_backoff_until.astimezone(timezone(timedelta(hours=8)))
                    account_name = selected_account.name if selected_account else selected_steam_id64
                    print(
                        f"[账号节流] {candidate.market_hash_name} | "
                        f"账号={account_name} | "
                        f"Steam 上架接口正在冷却，等待到 {local_retry_at.strftime('%Y-%m-%d %H:%M:%S')} 后再试"
                    )
                    break
                if self._is_weapon_case(candidate.market_hash_name):
                    occupied_count = self._occupied_case_guadao_slot_count()
                    if self._case_open_guadao_limit_reached(occupied_count):
                        self._notify_case_open_guadao_limit(occupied_count)
                        break
                if wait_before_next_listing:
                    time.sleep(STEAM_LISTING_SUCCESS_DELAY_SECONDS)
                    wait_before_next_listing = False
                asset_row = self.db.pick_tradable_asset(
                    candidate.market_hash_name,
                    steam_id=selected_steam_id64,
                    exclude_asset_ids=picked_asset_ids,
                )
                if asset_row is None:
                    break

                asset_id = asset_row["asset_id"]
                defer_state = self._active_listing_defer_state(str(asset_id))
                if defer_state is not None:
                    picked_asset_ids.add(str(asset_id))
                    defer_row = self._latest_listing_operation(str(asset_id), statuses=("deferred",))
                    defer_note = _read_note(defer_row["note"]) if defer_row is not None else {}
                    defer_message = str(defer_note.get("deferMessage") or "").strip()
                    if _message_indicates_pending_confirmation(defer_message):
                        _, status_after = self._record_listing_pending_confirmation_from_sellitem(
                            candidate=candidate,
                            asset_id=str(asset_id),
                            price=decision.list_price,
                            account=selected_account,
                            steam_id64=selected_steam_id64,
                            message=defer_message,
                            client=client,
                        )
                        status_map[candidate.market_hash_name] = status_after
                        if status_after == POOL_STATUS_LISTED:
                            print(
                                f"[上架确认] {candidate.market_hash_name} | "
                                f"账号={selected_account.name if selected_account else selected_steam_id64} | "
                                f"asset={asset_id} | "
                                f"Steam挂价 CNY {decision.list_price:.2f} | "
                                "状态: Steam 已返回待令牌确认，已自动确认并验证为活跃挂单"
                            )
                        elif status_after == POOL_STATUS_HOLDING:
                            pass
                        else:
                            print(
                                f"[上架待确认] {candidate.market_hash_name} | "
                                f"账号={selected_account.name if selected_account else selected_steam_id64} | "
                                f"asset={asset_id} | "
                                f"Steam挂价 CNY {decision.list_price:.2f} | "
                                "状态: Steam 已返回待令牌确认，已尝试自动确认，仍需继续追踪"
                            )
                        if status_after != POOL_STATUS_HOLDING:
                            list_count += 1
                            wait_before_next_listing = True
                        continue
                    if str(asset_id) not in defer_logged_asset_ids:
                        local_retry_at = defer_state.deferred_until.astimezone(timezone(timedelta(hours=8)))
                        print(
                            f"[上架冷却] {candidate.market_hash_name} | "
                            f"asset={asset_id} | "
                            f"该资产此前被 Steam 临时拒绝 {defer_state.defer_count} 次，"
                            f"冷却至 {local_retry_at.strftime('%Y-%m-%d %H:%M:%S')} 再重试"
                        )
                        defer_logged_asset_ids.add(str(asset_id))
                    continue
                picked_asset_ids.add(asset_id)
                if self.config.dry_run:
                    print(
                        f"[dry-run] 上架 {candidate.market_hash_name} asset={asset_id} "
                        f"price={decision.list_price:.2f}"
                    )
                    list_count += 1
                    continue

                try:
                    payload = self._sell_item_with_retry(
                        client=client,
                        asset_id=asset_id,
                        price=decision.list_price,
                    )
                except _NewGuadaoActionBlocked:
                    self._stop_requested = True
                    self._stop_reason = "new_action_guard_blocked"
                    print(
                        f"[停止新上架] {candidate.market_hash_name} | asset={asset_id} | "
                        "执行器已关闭或新动作门禁不可用；未发送 Steam sellitem"
                    )
                    break
                except SteamMarketError as exc:
                    if _is_pending_confirmation_sellitem_error(exc):
                        _, status_after = self._record_listing_pending_confirmation_from_sellitem(
                            candidate=candidate,
                            asset_id=str(asset_id),
                            price=decision.list_price,
                            account=selected_account,
                            steam_id64=selected_steam_id64,
                            message=str(exc),
                            client=client,
                        )
                        status_map[candidate.market_hash_name] = status_after
                        if status_after == POOL_STATUS_LISTED:
                            print(
                                f"[上架确认] {candidate.market_hash_name} | "
                                f"账号={selected_account.name if selected_account else selected_steam_id64} | "
                                f"asset={asset_id} | "
                                f"Steam挂价 CNY {decision.list_price:.2f} | "
                                "状态: Steam 已返回待令牌确认，已自动确认并验证为活跃挂单"
                            )
                        elif status_after == POOL_STATUS_HOLDING:
                            pass
                        else:
                            print(
                                f"[上架待确认] {candidate.market_hash_name} | "
                                f"账号={selected_account.name if selected_account else selected_steam_id64} | "
                                f"asset={asset_id} | "
                                f"Steam挂价 CNY {decision.list_price:.2f} | "
                                "状态: Steam 已返回待令牌确认，已尝试自动确认，仍需继续追踪"
                            )
                        if status_after != POOL_STATUS_HOLDING:
                            list_count += 1
                            wait_before_next_listing = True
                        continue
                    if _is_transient_listing_error(exc):
                        backoff_until = self._set_listing_account_backoff(client)
                        defer_state = self._record_listing_transient_defer(
                            candidate=candidate,
                            asset_id=str(asset_id),
                            price=decision.list_price,
                            account=selected_account,
                            steam_id64=selected_steam_id64,
                            error=exc,
                        )
                        local_asset_retry_at = defer_state.deferred_until.astimezone(timezone(timedelta(hours=8)))
                        local_account_retry_at = (
                            backoff_until.astimezone(timezone(timedelta(hours=8)))
                            if backoff_until is not None
                            else local_asset_retry_at
                        )
                        print(
                            f"[上架延后] {candidate.market_hash_name} | "
                            f"asset={asset_id} | "
                            f"Steam挂价 CNY {decision.list_price:.2f} | "
                            f"账号将冷却到 {local_account_retry_at.strftime('%Y-%m-%d %H:%M:%S')}，"
                            f"该资产将冷却到 {local_asset_retry_at.strftime('%Y-%m-%d %H:%M:%S')} 后再试，"
                            "本轮继续尝试其他资产 | "
                            f"原因: {exc}"
                        )
                        continue
                    defer_state = self._record_listing_defer(
                        candidate=candidate,
                        asset_id=str(asset_id),
                        price=decision.list_price,
                        account=selected_account,
                        steam_id64=selected_steam_id64,
                        error=exc,
                        reason="sellitem_failure",
                    )
                    local_asset_retry_at = defer_state.deferred_until.astimezone(timezone(timedelta(hours=8)))
                    print(
                        f"[上架失败冷却] {candidate.market_hash_name} | "
                        f"asset={asset_id} | "
                        f"Steam挂价 CNY {decision.list_price:.2f} | "
                        f"该资产冷却到 {local_asset_retry_at.strftime('%Y-%m-%d %H:%M:%S')} 后可重试，"
                        "不会永久停留在 listing_failed | "
                        f"原因: {exc}"
                    )
                    continue
                listing_id = str(payload.get("listingid") or "")
                confirmation_note: dict[str, Any] = {
                    "needsConfirmation": True,
                    "confirmationStatus": "pending",
                }
                status_after = POOL_STATUS_LISTED
                pending_count_before = self._pending_confirmation_count
                confirmation_note, status_after = self._handle_listing_confirmation(
                    market_hash_name=candidate.market_hash_name,
                    asset_id=asset_id,
                    listing_id=listing_id,
                    client=client,
                    account=selected_account,
                )

                if status_after == POOL_STATUS_LISTED:
                    op_status = "listed"
                    asset_status = "listed"
                elif (
                    status_after == POOL_STATUS_HOLDING
                    and confirmation_note.get("confirmationStatus") == "market_pending_removed"
                ):
                    op_status = "canceled"
                    asset_status = "available"
                else:
                    op_status = POOL_STATUS_LISTING_PENDING
                    asset_status = "listing_pending"

                note_dict = {
                    "listingId": listing_id,
                    "rebuyPrice": candidate.rebuy_price,
                    "steamListPrice": decision.list_price,
                    "steamSellerNetPrice": _steam_seller_net_from_gross(
                        decision.list_price,
                        self.config.steam_net_factor,
                    ),
                    "strategy": candidate.primary_strategy,
                    **self._guadao_open_ratio_note(candidate, decision),
                    **self._account_note_fields(selected_account, selected_steam_id64),
                    **confirmation_note,
                }
                note = _build_note(note_dict)
                op_id = self.db.add_pool_operation(
                    market_hash_name=candidate.market_hash_name,
                    strategy=candidate.primary_strategy,
                    operation_type=OP_SELL_STEAM,
                    expected_price=decision.list_price,
                    asset_id=asset_id,
                    note=note,
                )
                self.db.update_pool_operation(op_id, status=op_status)
                self.db.set_asset_status(asset_id, asset_status)
                if op_status == "canceled":
                    if not self._has_other_open_guadao_operation(candidate.market_hash_name, exclude_op_id=op_id):
                        self.db.set_pool_status(candidate.market_hash_name, POOL_STATUS_HOLDING)
                    status_map[candidate.market_hash_name] = POOL_STATUS_HOLDING
                else:
                    self.db.set_pool_status(candidate.market_hash_name, status_after)
                    status_map[candidate.market_hash_name] = status_after
                if op_status in {"listed", POOL_STATUS_LISTING_PENDING}:
                    self._emit_guadao_local_event(
                        operation=(
                            "steam_listing_active"
                            if op_status == "listed"
                            else "steam_listing_pending_confirmation"
                        ),
                        message=(
                            "Steam 挂刀已确认活跃"
                            if op_status == "listed"
                            else "Steam 挂刀已提交，等待确认或活跃状态"
                        ),
                        market_hash_name=candidate.market_hash_name,
                        operation_id=op_id,
                        asset_id=str(asset_id),
                        note=note_dict,
                        context={
                            "state": op_status,
                            "steamListPrice": decision.list_price,
                            "steamExpectedNetAmount": (
                                decision.list_price * self.config.steam_net_factor
                            ),
                            "listingRatio": decision.listing_ratio,
                            "listingId": listing_id or None,
                        },
                    )
                immediate_sold = False
                if op_status == POOL_STATUS_LISTING_PENDING and client is not None:
                    immediate_sold = self._mark_pending_sell_operation_sold_if_receipted(
                        op_id=op_id,
                        note=note_dict,
                        client=client,
                        asset_id=asset_id,
                    )
                    if immediate_sold:
                        self._pending_confirmation_count = pending_count_before
                        status_after = POOL_STATUS_PENDING_REBUY
                        status_map[candidate.market_hash_name] = POOL_STATUS_PENDING_REBUY
                if immediate_sold:
                    pass
                elif status_after == POOL_STATUS_LISTED:
                    print(
                        f"[上架] {candidate.market_hash_name} | "
                        f"账号={selected_account.name if selected_account else selected_steam_id64} | "
                        f"asset={asset_id} | "
                        f"预计挂刀比例 {decision.listing_ratio * 100:.2f}% | "
                        f"Steam挂价 CNY {decision.list_price:.2f} | "
                        f"预计到手 CNY {decision.list_price * self.config.steam_net_factor:.2f}"
                    )
                elif status_after == POOL_STATUS_HOLDING:
                    pass
                else:
                    print(
                        f"[上架待确认] {candidate.market_hash_name} | "
                        f"账号={selected_account.name if selected_account else selected_steam_id64} | "
                        f"asset={asset_id} | "
                        f"预计挂刀比例 {decision.listing_ratio * 100:.2f}% | "
                        f"Steam挂价 CNY {decision.list_price:.2f} | "
                        f"预计到手 CNY {decision.list_price * self.config.steam_net_factor:.2f} | "
                        f"状态: Steam Guard 未确认，未计为真实活跃挂单"
                    )
                if status_after != POOL_STATUS_HOLDING:
                    list_count += 1
                    wait_before_next_listing = True

        return list_count

    def _sell_item_with_retry(
        self,
        *,
        asset_id: str,
        price: float,
        client: SteamMarketClient | None = None,
    ) -> dict[str, Any]:
        active_client = client or self.steam_client
        if not active_client:
            raise SteamMarketError("missing Steam client")
        last_exc: SteamMarketError | None = None
        for attempt in range(STEAM_LISTING_MAX_ATTEMPTS):
            try:
                self._wait_for_listing_account_slot(active_client)
                guard = getattr(self, "_new_action_guard", None)
                if guard is not None:
                    try:
                        action_allowed = bool(guard())
                    except Exception:
                        action_allowed = False
                    if not action_allowed:
                        raise _NewGuadaoActionBlocked("new guadao action is no longer allowed")
                return active_client.sell_item(
                    app_id=self.settings.app_id,
                    context_id=self.config.steam_context_id,
                    asset_id=asset_id,
                    price=price,
                    quantity=1,
                    steam_net_factor=self.config.steam_net_factor,
                    **({"execution_guard": guard} if guard is not None else {}),
                )
            except SteamRequestGuardRejected as exc:
                raise _NewGuadaoActionBlocked(str(exc)) from exc
            except SteamMarketError as exc:
                if not _is_transient_listing_error(exc):
                    raise
                last_exc = exc
                self._set_listing_account_backoff(active_client)
                break
        if last_exc is not None:
            raise last_exc
        raise SteamMarketError("Steam sellitem failed without response")

    def _refresh_pending_listing_confirmations(
        self,
        *,
        client: SteamMarketClient | None = None,
        active_listings: list[Any] | None = None,
        operation_ids: set[int] | None = None,
        sale_receipt_results: dict[int, dict[str, Any] | None] | None = None,
        sale_receipt_deep_attempt_ids: set[int] | None = None,
        sale_receipt_deep_attempted_at: str | None = None,
    ) -> int:
        active_client = client or self.steam_client
        if not active_client:
            return 0
        if active_listings is None:
            try:
                active = active_client.list_active_listings()
            except Exception as exc:
                print(f"[警告] 获取 Steam 挂单列表失败: {exc}")
                return 0
        else:
            active = active_listings
        active_listing_ids, active_asset_ids = self._active_listing_identity_sets(active)
        updated = 0
        existing_rebuy_sources: set[str] | None = None
        existing_rebuy_sell_ops: set[str] | None = None
        candidate_ops = self.db.list_pool_operations_by_type_and_statuses(
            OP_SELL_STEAM,
            statuses=[POOL_STATUS_LISTING_PENDING, "listed", "manual_required"],
            limit=300,
        )
        if operation_ids is not None:
            selected_ids = {int(value) for value in operation_ids}
            candidate_ops = [op for op in candidate_ops if int(op["id"]) in selected_ids]

        if sale_receipt_results is None:
            receipt_lookup = self._lookup_steam_sale_receipts_for_operations(
                active_client,
                candidate_ops,
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
            )
            sale_receipt_results = receipt_lookup.receipts
            sale_receipt_deep_attempt_ids = receipt_lookup.deep_attempt_ids
            sale_receipt_deep_attempted_at = receipt_lookup.deep_attempted_at
        deep_attempt_ids = set(sale_receipt_deep_attempt_ids or set())

        # Confirm every due operation first, then refresh mylistings exactly
        # once for the account.  The old per-operation refresh multiplied the
        # same expensive Steam route by the number of pending listings.
        confirmation_results: dict[int, tuple[int | None, Exception | None]] = {}
        confirmer = getattr(active_client, "confirm_listing_assets", None)
        for op in candidate_ops:
            if not self._operation_matches_client(op, active_client):
                continue
            note = _read_note(op["note"])
            listing_id = str(note.get("listingId") or "").strip()
            asset_id = str(op["asset_id"] or "").strip()
            if self._listing_is_active(
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
                listing_id=listing_id,
                asset_id=asset_id,
            ):
                continue
            operation_id = int(op["id"])
            if sale_receipt_results.get(operation_id) is not None:
                # Official history is already terminal evidence.  Do not send
                # an unnecessary Steam Guard confirmation request afterward.
                continue
            if str(op["status"] or "") == "manual_required":
                continue
            needs_confirmation_retry = (
                op["status"] == POOL_STATUS_LISTING_PENDING
                or note.get("confirmationStatus") in LISTING_CONFIRMATION_PENDING_STATUSES
                or not note.get("activeVerifiedAt")
            )
            if not needs_confirmation_retry:
                continue
            if not callable(confirmer):
                confirmation_results[operation_id] = (
                    None,
                    SteamMarketError(
                        "Steam client does not support scoped listing confirmation"
                    ),
                )
                continue
            try:
                count = confirmer(
                    asset_ids=[asset_id],
                    listing_ids=[listing_id] if listing_id else None,
                )
                confirmation_results[operation_id] = (count, None)
            except Exception as exc:
                confirmation_results[operation_id] = (None, exc)

        if confirmation_results:
            try:
                active = active_client.list_active_listings()
                active_listing_ids, active_asset_ids = self._active_listing_identity_sets(active)
            except Exception:
                # Keep the original snapshot.  A failed post-confirmation
                # refresh is inconclusive and must not invent a transition.
                pass

        for op in candidate_ops:
            if not self._operation_matches_client(op, active_client):
                continue
            note = _read_note(op["note"])
            is_stale_manual_recheck = (
                str(op["status"] or "") == "manual_required"
                and str(note.get("staleListedCleanupStatus") or "") == "manual_required"
            )
            if str(op["status"] or "") == "manual_required" and not is_stale_manual_recheck:
                continue
            operation_id = int(op["id"])
            if operation_id in deep_attempt_ids:
                self._record_sale_receipt_deep_attempt(
                    note,
                    attempted_at=sale_receipt_deep_attempted_at,
                )
            listing_id = str(note.get("listingId") or "").strip()
            asset_id = str(op["asset_id"] or "").strip()
            if not self._listing_is_active(
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
                listing_id=listing_id,
                asset_id=asset_id,
            ):
                sale_receipt = sale_receipt_results.get(operation_id)
                if sale_receipt is not None:
                    if existing_rebuy_sources is None or existing_rebuy_sell_ops is None:
                        existing_rebuy_sources, existing_rebuy_sell_ops = self._load_existing_rebuy_source_keys()
                    self._mark_steam_listing_sold(
                        op,
                        note,
                        sale_receipt=sale_receipt,
                        existing_rebuy_sources=existing_rebuy_sources,
                        existing_rebuy_sell_ops=existing_rebuy_sell_ops,
                    )
                    updated += 1
                    continue
                if is_stale_manual_recheck:
                    note["staleListedLastRecheckedAt"] = utc_now_iso()
                    self.db.update_pool_operation(op["id"], note=_build_note(note))
                    continue
                needs_confirmation_retry = (
                    op["status"] == POOL_STATUS_LISTING_PENDING
                    or note.get("confirmationStatus") in LISTING_CONFIRMATION_PENDING_STATUSES
                    or not note.get("activeVerifiedAt")
                )
                if needs_confirmation_retry:
                    confirmation_retry_count, confirmation_retry_error = confirmation_results.get(
                        operation_id,
                        (
                            None,
                            SteamMarketError(
                                "Steam listing confirmation was not attempted"
                            ),
                        ),
                    )
                    if self._listing_is_active(
                        active_listing_ids=active_listing_ids,
                        active_asset_ids=active_asset_ids,
                        listing_id=listing_id,
                        asset_id=asset_id,
                    ):
                        if not listing_id:
                            recovered_listing_id = self._active_listing_id_for_asset(active, asset_id)
                            if recovered_listing_id:
                                note["listingId"] = recovered_listing_id
                        note["confirmationStatus"] = "confirmed_late"
                        note["confirmationRecoveredAt"] = utc_now_iso()
                        note["confirmationRetryCount"] = confirmation_retry_count
                        self._mark_steam_listing_active(op, note)
                        updated += 1
                        continue
                    retry_note = {
                        **note,
                        "confirmationRetryAt": utc_now_iso(),
                        "listingPendingAt": note.get("listingPendingAt") or utc_now_iso(),
                    }
                    if confirmation_retry_error is not None:
                        retry_note["confirmationRetryStatus"] = "failed"
                        retry_note["confirmationRetryMessage"] = str(confirmation_retry_error)
                    else:
                        retry_note["confirmationRetryStatus"] = (
                            "confirmed_waiting_active_listing"
                            if (confirmation_retry_count or 0) > 0
                            else "not_found"
                        )
                        retry_note["confirmationRetryCount"] = confirmation_retry_count
                    pending_market_listing = self._pending_market_confirmation_listing(
                        active_client,
                        listing_id=listing_id,
                        asset_id=asset_id,
                    )
                    if pending_market_listing is not None:
                        pending_listing_id = str(getattr(pending_market_listing, "listing_id", "") or "").strip()
                        retry_note["listingId"] = pending_listing_id or retry_note.get("listingId")
                        retry_note["marketPendingListingId"] = pending_listing_id
                        retry_note["confirmationRetryStatus"] = "market_pending_visible"
                        retry_note["confirmationMessage"] = (
                            "Steam market mylistings shows this listing waiting for confirmation, "
                            "but mobileconf returned no confirmation"
                        )
                        removed, remove_error = self._remove_market_pending_confirmation_listing(
                            active_client,
                            pending_market_listing,
                        )
                        if removed:
                            retry_note["marketPendingRemovedAt"] = utc_now_iso()
                            self._release_removed_market_pending_listing(op, retry_note)
                            continue
                        retry_note["confirmationStatus"] = "market_pending_remove_failed"
                        retry_note["confirmationRetryMessage"] = (
                            f"automatic remove failed: {remove_error}"
                        )
                        self._market_pending_cleanup_failed_count += 1
                    if (
                        str(note.get("confirmationStatus") or "")
                        == "confirm_sent_waiting_active_listing"
                        and confirmation_retry_error is None
                        and (confirmation_retry_count or 0) <= 0
                        and pending_market_listing is None
                        and (
                            operation_id in deep_attempt_ids
                            or bool(note.get("saleEvidenceDeepLastAttemptAt"))
                        )
                    ):
                        retry_note["listingMissingRecoveryFrom"] = (
                            "confirm_sent_waiting_active_listing"
                        )
                        retry_note["listingMissingRecoveryAt"] = utc_now_iso()
                        self._mark_steam_listing_pending(
                            op,
                            retry_note,
                            reason="listing_missing_unverified",
                        )
                        continue
                    self.db.update_pool_operation(
                        op["id"],
                        status=POOL_STATUS_LISTING_PENDING,
                        note=_build_note(retry_note),
                    )
                    self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_LISTING_PENDING)
                    if asset_id:
                        self.db.set_asset_status(asset_id, "listing_pending")
                    continue
                if op["status"] == "listed" and (
                    note.get("confirmationStatus") in LISTING_CONFIRMATION_PENDING_STATUSES
                    or not note.get("activeVerifiedAt")
                ):
                    self._mark_steam_listing_pending(op, note, reason="listing_missing_unverified")
                continue
            note["confirmationStatus"] = "confirmed_late"
            note["confirmationRecoveredAt"] = utc_now_iso()
            confirmation_retry = confirmation_results.get(operation_id)
            if confirmation_retry is not None:
                note["confirmationRetryCount"] = confirmation_retry[0]
            if not listing_id:
                recovered_listing_id = self._active_listing_id_for_asset(active, asset_id)
                if recovered_listing_id:
                    note["listingId"] = recovered_listing_id
            self._mark_steam_listing_active(op, note)
            updated += 1
        return updated

    def _active_listing_identity_sets(self, active: list[Any]) -> tuple[set[str], set[str]]:
        listing_ids = {
            str(getattr(lst, "listing_id", "") or "").strip()
            for lst in active
            if str(getattr(lst, "listing_id", "") or "").strip()
        }
        asset_ids = {
            str(getattr(lst, "asset_id", "") or "").strip()
            for lst in active
            if str(getattr(lst, "asset_id", "") or "").strip()
        }
        return listing_ids, asset_ids

    def _active_listing_id_for_asset(self, active: list[Any], asset_id: str) -> str | None:
        expected_asset_id = str(asset_id or "").strip()
        if not expected_asset_id:
            return None
        for listing in active:
            listing_asset_id = str(getattr(listing, "asset_id", "") or "").strip()
            if listing_asset_id != expected_asset_id:
                continue
            listing_id = str(getattr(listing, "listing_id", "") or "").strip()
            if listing_id:
                return listing_id
        return None

    def _pending_market_confirmation_listing(
        self,
        client: SteamMarketClient | None,
        *,
        listing_id: str,
        asset_id: str,
    ) -> Any | None:
        if not client:
            return None
        loader = getattr(client, "list_confirmation_pending_listings", None)
        if not callable(loader):
            return None
        expected_listing_id = str(listing_id or "").strip()
        expected_asset_id = str(asset_id or "").strip()
        if not expected_listing_id and not expected_asset_id:
            return None
        try:
            pending_listings = loader()
        except Exception as exc:
            print(f"[警告] 获取 Steam 待确认挂单列表失败: {exc}")
            return None
        for listing in pending_listings:
            pending_listing_id = str(getattr(listing, "listing_id", "") or "").strip()
            pending_asset_id = str(getattr(listing, "asset_id", "") or "").strip()
            if expected_listing_id and pending_listing_id == expected_listing_id:
                return listing
            if expected_asset_id and pending_asset_id == expected_asset_id:
                return listing
        return None

    def _remove_market_pending_confirmation_listing(
        self,
        client: SteamMarketClient | None,
        listing: Any,
    ) -> tuple[bool, str | None]:
        if not client:
            return False, "missing Steam client"
        listing_id = str(getattr(listing, "listing_id", "") or "").strip()
        if not listing_id:
            return False, "missing listing id"
        remover = getattr(client, "remove_listing", None)
        if not callable(remover):
            return False, "Steam client does not support remove_listing"
        try:
            removed = bool(remover(listing_id))
        except Exception as exc:
            return False, str(exc)
        if not removed:
            return False, "Steam remove_listing returned false"
        return True, None

    def _listing_is_active(
        self,
        *,
        active_listing_ids: set[str],
        active_asset_ids: set[str],
        listing_id: str,
        asset_id: str,
    ) -> bool:
        if listing_id and listing_id in active_listing_ids:
            return True
        if asset_id and asset_id in active_asset_ids:
            return True
        return False

    def _mark_steam_listing_active(self, op: Any, note: dict[str, Any]) -> None:
        note["activeVerifiedAt"] = note.get("activeVerifiedAt") or utc_now_iso()
        self.db.update_pool_operation(op["id"], status="listed", note=_build_note(note))
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_LISTED)
        asset_id = str(op["asset_id"] or "").strip()
        if asset_id:
            self.db.set_asset_status(asset_id, "listed")

    def _mark_steam_listing_pending(self, op: Any, note: dict[str, Any], *, reason: str) -> None:
        note["confirmationStatus"] = reason
        note["listingPendingAt"] = note.get("listingPendingAt") or utc_now_iso()
        if reason == "listing_missing_unverified":
            self._record_listing_missing_observation(note, first_observation=True)
        self.db.update_pool_operation(
            op["id"],
            status=POOL_STATUS_LISTING_PENDING,
            note=_build_note(note),
        )
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_LISTING_PENDING)
        asset_id = str(op["asset_id"] or "").strip()
        if asset_id:
            self.db.set_asset_status(asset_id, "listing_pending")

    def _has_other_open_guadao_operation(self, market_hash_name: str, *, exclude_op_id: int) -> bool:
        rows = self.db.conn.execute(
            """
            SELECT id
            FROM pool_operations
            WHERE market_hash_name = ?
              AND strategy = ?
              AND id <> ?
              AND (
                (operation_type = ? AND status IN ('listed', ?))
                OR (operation_type = ? AND status = 'pending')
              )
            LIMIT 1
            """,
            (
                market_hash_name,
                STRATEGY_GUADAO,
                exclude_op_id,
                OP_SELL_STEAM,
                POOL_STATUS_LISTING_PENDING,
                OP_REBUY_C5,
            ),
        ).fetchone()
        return rows is not None

    def _reconcile_guadao_pool_status_after_listing_release(
        self,
        market_hash_name: str,
    ) -> str:
        """Rebuild the item-level pool state from remaining guadao operations."""

        rows = self.db.conn.execute(
            """
            SELECT operation_type, status, note
            FROM pool_operations
            WHERE market_hash_name = ?
              AND strategy = ?
              AND (
                (operation_type = ? AND status IN (?, 'listed', 'manual_required'))
                OR (
                    operation_type = ?
                    AND status IN ('pending', 'delivery_pending', ?, 'failed', 'manual_required')
                )
              )
            ORDER BY id ASC
            """,
            (
                market_hash_name,
                STRATEGY_GUADAO,
                OP_SELL_STEAM,
                POOL_STATUS_LISTING_PENDING,
                OP_REBUY_C5,
                C5_SUBMISSION_UNCONFIRMED,
            ),
        ).fetchall()

        has_blocking_rebuy = any(
            row["operation_type"] == OP_REBUY_C5
            and (
                row["status"] == "manual_required"
                or (row["status"] == "failed" and self._failed_rebuy_counts_as_open(row))
            )
            for row in rows
        )
        if has_blocking_rebuy:
            pool_status = POOL_STATUS_REBUY_FAILED
        elif any(
            row["operation_type"] == OP_SELL_STEAM
            and row["status"] == POOL_STATUS_LISTING_PENDING
            for row in rows
        ):
            pool_status = POOL_STATUS_LISTING_PENDING
        elif any(
            row["operation_type"] == OP_SELL_STEAM
            and row["status"] in {"listed", "manual_required"}
            for row in rows
        ):
            pool_status = POOL_STATUS_LISTED
        elif any(
            row["operation_type"] == OP_REBUY_C5
            and row["status"] in {"pending", "delivery_pending", C5_SUBMISSION_UNCONFIRMED}
            for row in rows
        ):
            pool_status = POOL_STATUS_PENDING_REBUY
        else:
            pool_status = POOL_STATUS_HOLDING

        self.db.set_pool_status(market_hash_name, pool_status)
        return pool_status

    def _release_removed_market_pending_listing(self, op: Any, note: dict[str, Any]) -> None:
        asset_id = str(op["asset_id"] or "").strip()
        listing_id = str(note.get("marketPendingListingId") or note.get("listingId") or "").strip()
        note["needsConfirmation"] = False
        note["confirmationStatus"] = "market_pending_removed"
        note["marketPendingRemovedAt"] = note.get("marketPendingRemovedAt") or utc_now_iso()
        self.db.update_pool_operation(op["id"], status="canceled", note=_build_note(note))
        if asset_id:
            self.db.set_asset_status(asset_id, "available")
        self._reconcile_guadao_pool_status_after_listing_release(
            str(op["market_hash_name"]),
        )
        print(
            f"[挂单待确认清理] {op['market_hash_name']} | asset={asset_id or '-'} | "
            f"listing={listing_id or '-'} | "
            "Steam 网页待确认挂单已撤下，资产已释放，下轮可重新上架"
        )

    @staticmethod
    def _listing_missing_observation_count(note: dict[str, Any]) -> int:
        return max(0, safe_int(note.get("activeListingMissingObservationCount")) or 0)

    def _record_listing_missing_observation(
        self,
        note: dict[str, Any],
        *,
        first_observation: bool = False,
    ) -> int:
        """Persist distinct active-listing absence observations conservatively."""

        observed_at = utc_now_iso()
        count = self._listing_missing_observation_count(note)
        if first_observation:
            # A transition from listed -> listing_missing_unverified is itself
            # the first complete mylistings absence observation.
            count = max(1, count)
            note["activeListingMissingFirstObservedAt"] = (
                note.get("activeListingMissingFirstObservedAt") or observed_at
            )
        else:
            # Older rows predate the counter.  Their persisted
            # listing_missing_unverified state is proof of one previous
            # absence, so the current fresh snapshot becomes observation two.
            count = max(1, count) + 1
            note["activeListingMissingFirstObservedAt"] = (
                note.get("activeListingMissingFirstObservedAt")
                or note.get("listingPendingAt")
                or observed_at
            )
        note["activeListingMissingObservationCount"] = count
        note["activeListingMissingLastObservedAt"] = observed_at
        return count

    def _steam_inventory_return_check_due(
        self,
        note: dict[str, Any],
        *,
        operation_id: int,
        deep_attempt_ids: set[int],
    ) -> bool:
        """Avoid retrying official inventory on every routine 10-minute check."""

        if self._listing_missing_observation_count(note) < 2:
            return False
        if not note.get("steamInventoryReturnCheckAt"):
            return True
        # After a real inventory response (missing, incomplete, or failed),
        # wait for a new bounded deep history traversal before spending another
        # official inventory read.  A historical receipt may have appeared in
        # the meantime, and must win over any inventory fallback.
        return int(operation_id) in deep_attempt_ids

    @staticmethod
    def _listing_matches_identity(listing: Any, *, listing_id: str, asset_id: str) -> bool:
        candidate_listing_id = str(getattr(listing, "listing_id", "") or "").strip()
        candidate_asset_id = str(getattr(listing, "asset_id", "") or "").strip()
        return bool(
            (listing_id and candidate_listing_id == listing_id)
            or (asset_id and candidate_asset_id == asset_id)
        )

    def _prepare_official_inventory_return_checks(
        self,
        client: SteamMarketClient,
        operations: list[Any],
        *,
        active_listing_ids: set[str],
        active_asset_ids: set[str],
        sale_receipt_results: dict[int, dict[str, Any] | None],
        sale_receipt_lookup_succeeded: bool,
        sale_receipt_coverage_complete: bool,
        deep_attempt_ids: set[int],
    ) -> dict[int, dict[str, Any]]:
        """Prepare one account-batched official inventory recovery check.

        A missing market listing is ambiguous.  This helper runs only after a
        successful official market-history read has not matched a receipt, and
        after a second independent active-listing absence.  A partial history
        page is still insufficient to prove a sale, but an exact asset found
        in Steam's official inventory is independently decisive evidence that
        the asset was not sold.  Inventory absence never converts into a sale.
        """

        if not sale_receipt_lookup_succeeded:
            return {}

        candidates: list[tuple[Any, dict[str, Any], str, str]] = []
        for op in operations:
            note = _read_note(op["note"])
            if (
                str(op["status"] or "") != POOL_STATUS_LISTING_PENDING
                or str(note.get("confirmationStatus") or "")
                != "listing_missing_unverified"
            ):
                continue
            operation_id = int(op["id"])
            if sale_receipt_results.get(operation_id) is not None:
                continue
            listing_id = str(note.get("listingId") or "").strip()
            asset_id = str(op["asset_id"] or "").strip()
            if not asset_id or self._listing_is_active(
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
                listing_id=listing_id,
                asset_id=asset_id,
            ):
                continue
            projected_observations = max(1, self._listing_missing_observation_count(note)) + 1
            if projected_observations < 2 or not self._steam_inventory_return_check_due(
                {**note, "activeListingMissingObservationCount": projected_observations},
                operation_id=operation_id,
                deep_attempt_ids=deep_attempt_ids,
            ):
                continue
            candidates.append((op, note, listing_id, asset_id))
        if not candidates:
            return {}

        pending_loader = getattr(client, "list_confirmation_pending_listings", None)
        if not callable(pending_loader):
            return {}
        try:
            pending_listings = list(pending_loader())
        except Exception as exc:
            return {
                int(op["id"]): {
                    "status": "market_pending_lookup_failed",
                    "message": str(exc),
                }
                for op, _, _, _ in candidates
            }

        checks: dict[int, dict[str, Any]] = {}
        inventory_candidates: list[tuple[Any, dict[str, Any], str]] = []
        for op, _note, listing_id, asset_id in candidates:
            if any(
                self._listing_matches_identity(
                    listing,
                    listing_id=listing_id,
                    asset_id=asset_id,
                )
                for listing in pending_listings
            ):
                checks[int(op["id"])] = {"status": "market_pending_visible"}
                continue
            inventory_candidates.append((op, _note, asset_id))
        if not inventory_candidates:
            return checks

        finder = getattr(client, "find_inventory_asset_ids", None)
        if not callable(finder):
            return checks
        checked_at = utc_now_iso()
        try:
            result = finder([asset_id for _, _, asset_id in inventory_candidates])
            found_asset_ids = {
                str(value or "").strip()
                for value in getattr(result, "found_asset_ids", frozenset())
                if str(value or "").strip()
            }
            coverage_complete = bool(getattr(result, "coverage_complete", False))
            pages_scanned = max(0, safe_int(getattr(result, "pages_scanned", 0)) or 0)
        except Exception as exc:
            return {
                **checks,
                **{
                    int(op["id"]): {
                        "status": "request_failed",
                        "checkedAt": checked_at,
                        "message": str(exc),
                    }
                    for op, _, _ in inventory_candidates
                },
            }

        for op, _note, asset_id in inventory_candidates:
            checks[int(op["id"])] = {
                "status": (
                    "found_same_asset"
                    if asset_id in found_asset_ids
                    else "not_found_complete"
                    if coverage_complete
                    else "not_found_incomplete"
                ),
                "checkedAt": checked_at,
                "pagesScanned": pages_scanned,
                "coverageComplete": coverage_complete,
                "historyCoverageComplete": sale_receipt_coverage_complete,
                "assetId": asset_id,
            }
        return checks

    def _release_listing_missing_asset_returned(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        inventory_check: dict[str, Any],
    ) -> None:
        """Release an exact asset proven present in Steam's official inventory."""

        asset_id = str(op["asset_id"] or "").strip()
        note["needsConfirmation"] = False
        note["confirmationStatus"] = "listing_missing_unsold_asset_returned"
        note["terminalEvidence"] = "official_steam_inventory_same_asset"
        note["steamInventoryAssetReturnedAt"] = utc_now_iso()
        note["steamInventoryReturnCheckAt"] = inventory_check.get("checkedAt") or utc_now_iso()
        note["steamInventoryReturnCheckStatus"] = "found_same_asset"
        note["steamInventoryReturnCheckAssetId"] = asset_id
        note["steamInventoryReturnCheckAccountId"] = note.get("steamAccountId") or getattr(
            self.account,
            "id",
            None,
        )
        note["steamSaleEvidenceChecked"] = True
        note["steamSaleReceiptFound"] = False
        note["steamSaleEvidenceHistoryCoverageComplete"] = inventory_check.get(
            "historyCoverageComplete"
        )
        note["releasedForRelisting"] = True
        note["steamInventoryReturnPagesScanned"] = inventory_check.get("pagesScanned")
        note["steamInventoryReturnCoverageComplete"] = inventory_check.get("coverageComplete")
        self.db.update_pool_operation(op["id"], status="canceled", note=_build_note(note))
        if asset_id:
            self.db.set_asset_status(asset_id, "available")
        self.db.delete_scheduled_task(f"sale-evidence:{int(op['id'])}")
        self._reconcile_guadao_pool_status_after_listing_release(
            str(op["market_hash_name"]),
        )
        print(
            f"[挂单待确认恢复] {op['market_hash_name']} | asset={asset_id or '-'} | "
            "Steam 官方库存确认同一 asset 仍在，未判卖出，已释放供下轮重新定价"
        )

    def _case_full_release_after_seconds(self) -> float:
        try:
            hours = float(self.config.case_full_release_after_hours)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(hours) or hours <= 0:
            return 0.0
        return hours * 3600.0

    def _case_full_release_fraction(self) -> float:
        try:
            fraction = float(self.config.case_full_release_fraction)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(fraction) or fraction <= 0:
            return 0.0
        return min(1.0, fraction)

    def _listed_case_guadao_operations(self) -> list[Any]:
        query_limit = max(500, self._case_max_open_guadao_count() * 2)
        return [
            op
            for op in self.db.list_pool_operations_by_type(
                OP_SELL_STEAM,
                status="listed",
                limit=query_limit,
            )
            if self._is_weapon_case(op["market_hash_name"])
        ]

    def _case_capacity_observation(self) -> dict[str, Any]:
        row = self.db.get_executor_runtime_state("guadao")
        if row is None:
            return {}
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        state = payload.get(CASE_CAPACITY_OBSERVATION_PAYLOAD_KEY)
        return dict(state) if isinstance(state, dict) else {}

    def _save_case_capacity_observation(self, state: dict[str, Any]) -> None:
        row = self.db.get_executor_runtime_state("guadao")
        if row is None:
            return
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        payload[CASE_CAPACITY_OBSERVATION_PAYLOAD_KEY] = dict(state)
        self.db.upsert_executor_runtime_state(
            "guadao",
            enabled=bool(row["enabled"]),
            runtime_status=str(row["runtime_status"]),
            migration_hold=bool(row["migration_hold"]),
            gate_reason=row["gate_reason"],
            heartbeat_at=row["heartbeat_at"],
            payload=payload,
        )

    def _case_capacity_max_observation_gap_seconds(self) -> float:
        schedule = self.config.effective_guadao_task_schedule()
        scan_seconds = max(1.0, safe_float(schedule.get("scanIntervalSeconds")) or 300.0)
        sync_seconds = max(1.0, safe_float(schedule.get("steamSyncIntervalSeconds")) or 120.0)
        # A normal scan/sync may drift slightly while another due task owns the
        # worker. More than 2.5 normal intervals is no longer continuous proof.
        return max(60.0, max(scan_seconds, sync_seconds) * 2.5)

    def _observe_case_listing_capacity(
        self,
        *,
        occupied: int,
        capacity: int,
        snapshot_complete: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = (now or _now_utc()).astimezone(timezone.utc)
        previous = self._case_capacity_observation()
        state: dict[str, Any] = {
            "observedAt": current.isoformat(),
            "lastObservedAt": current.isoformat(),
            "processSessionId": self._process_session_id,
            "occupied": max(0, int(occupied)),
            "capacity": max(0, int(capacity)),
            "snapshotComplete": bool(snapshot_complete),
        }
        if not snapshot_complete:
            state.update(
                {
                    "isFull": None,
                    "fullSince": None,
                    "continuityResetReason": "snapshot_unavailable",
                }
            )
            self._save_case_capacity_observation(state)
            return state
        if capacity <= 0 or occupied < capacity:
            state.update(
                {
                    "isFull": False,
                    "fullSince": None,
                    "continuityResetReason": "capacity_not_full",
                }
            )
            self._save_case_capacity_observation(state)
            return state

        previous_last = _parse_iso(str(previous.get("lastObservedAt") or ""))
        if previous_last is not None and previous_last.tzinfo is None:
            previous_last = previous_last.replace(tzinfo=timezone.utc)
        same_session = previous.get("processSessionId") == self._process_session_id
        gap_seconds = (
            max(0.0, (current - previous_last.astimezone(timezone.utc)).total_seconds())
            if previous_last is not None
            else None
        )
        gap_ok = gap_seconds is not None and gap_seconds <= self._case_capacity_max_observation_gap_seconds()
        continue_full = bool(previous.get("isFull")) and same_session and gap_ok
        if continue_full:
            full_since = _parse_iso(str(previous.get("fullSince") or ""))
            if full_since is None:
                full_since = current
                reset_reason = "missing_full_since"
            else:
                if full_since.tzinfo is None:
                    full_since = full_since.replace(tzinfo=timezone.utc)
                full_since = full_since.astimezone(timezone.utc)
                reset_reason = None
        else:
            full_since = current
            if previous.get("processSessionId") and not same_session:
                reset_reason = "process_restart"
            elif bool(previous.get("isFull")) and not gap_ok:
                reset_reason = "observation_gap"
            elif previous.get("isFull") is False:
                reset_reason = "previously_not_full"
            elif previous:
                reset_reason = "continuity_unproven"
            else:
                reset_reason = "first_full_observation"
        state.update(
            {
                "isFull": True,
                "fullSince": full_since.isoformat(),
                "continuityResetReason": reset_reason,
                "observationGapSeconds": gap_seconds,
            }
        )
        self._save_case_capacity_observation(state)
        return state

    def _release_full_case_listing_capacity(self) -> int:
        listed_ops = self._listed_case_guadao_operations()
        capacity = self._case_max_open_guadao_count()
        occupied = sum(max(1, safe_int(op["quantity"]) or 1) for op in listed_ops)
        if capacity <= 0 or occupied < capacity:
            self._observe_case_listing_capacity(
                occupied=occupied,
                capacity=capacity,
                snapshot_complete=True,
            )
            return 0

        release_after_seconds = self._case_full_release_after_seconds()
        release_fraction = self._case_full_release_fraction()
        if release_after_seconds <= 0 or release_fraction <= 0:
            self._observe_case_listing_capacity(
                occupied=occupied,
                capacity=capacity,
                snapshot_complete=False,
            )
            return 0

        targets = self._open_guadao_steam_targets()
        if not targets:
            targets = [(str(getattr(self.steam_client, "steam_id64", "") or "") or None, self.account)]

        active_records: list[tuple[Any, dict[str, Any], SteamMarketClient, str]] = []
        seen_operation_ids: set[int] = set()
        snapshot_complete = True
        for steam_id, account in targets:
            client = self._steam_client_for_account(account, steam_id)
            if client is None:
                snapshot_complete = False
                continue
            try:
                active = client.list_active_listings()
            except Exception as exc:
                snapshot_complete = False
                print(
                    f"[警告] 满载随机释放读取 Steam 活跃挂单失败 | "
                    f"steam={steam_id or '-'} | 原因: {exc}"
                )
                continue
            active_listing_ids, active_asset_ids = self._active_listing_identity_sets(active)
            for op in listed_ops:
                operation_id = int(op["id"])
                if operation_id in seen_operation_ids or not self._operation_matches_client(op, client):
                    continue
                note = _read_note(op["note"])
                listing_id = str(note.get("listingId") or "").strip()
                asset_id = str(op["asset_id"] or "").strip()
                if not self._listing_is_active(
                    active_listing_ids=active_listing_ids,
                    active_asset_ids=active_asset_ids,
                    listing_id=listing_id,
                    asset_id=asset_id,
                ):
                    continue
                if not listing_id and asset_id:
                    listing_id = self._active_listing_id_for_asset(active, asset_id) or ""
                    if listing_id:
                        note["listingId"] = listing_id
                if not listing_id:
                    continue
                seen_operation_ids.add(operation_id)
                active_records.append((op, note, client, listing_id))

        remote_occupied = sum(
            max(1, safe_int(record[0]["quantity"]) or 1) for record in active_records
        )
        observation = self._observe_case_listing_capacity(
            occupied=remote_occupied,
            capacity=capacity,
            snapshot_complete=snapshot_complete,
        )
        if not snapshot_complete or not observation.get("isFull"):
            return 0
        full_since = _parse_iso(str(observation.get("fullSince") or ""))
        if full_since is None:
            return 0
        if full_since.tzinfo is None:
            full_since = full_since.replace(tzinfo=timezone.utc)
        now = _now_utc()
        full_seconds = max(0.0, (now - full_since.astimezone(timezone.utc)).total_seconds())
        if full_seconds < release_after_seconds:
            return 0
        active_records.sort(key=lambda record: int(record[0]["id"]))
        release_count = min(
            len(active_records),
            max(1, int(math.ceil(len(active_records) * release_fraction))),
        )
        selected_records = random.sample(active_records, release_count)
        released = 0
        released_quantity = 0
        for op, note, client, listing_id in selected_records:
            remover = getattr(client, "remove_listing", None)
            try:
                removed = bool(remover(listing_id)) if callable(remover) else False
                remove_error = None if removed else "Steam remove_listing returned false"
            except Exception as exc:
                removed = False
                remove_error = str(exc)
            if not removed:
                print(
                    f"[满载随机释放失败] {op['market_hash_name']} | "
                    f"asset={op['asset_id'] or '-'} | listing={listing_id} | 原因: {remove_error}"
                )
                continue

            note["sequenceReleaseReason"] = "case_listing_capacity_full_random"
            note["sequenceReleasedAt"] = utc_now_iso()
            note["sequenceReleaseListingId"] = listing_id
            note["caseListingCapacity"] = capacity
            note["caseListingOccupiedAtRelease"] = occupied
            note["caseListingFullSince"] = full_since.isoformat()
            note["caseListingFullHoursAtRelease"] = round(full_seconds / 3600.0, 4)
            note["caseFullReleaseFraction"] = release_fraction
            self.db.update_pool_operation(op["id"], status="canceled", note=_build_note(note))
            asset_id = str(op["asset_id"] or "").strip()
            if asset_id:
                self.db.set_asset_status(asset_id, "available")
            if not self._has_other_open_guadao_operation(
                op["market_hash_name"],
                exclude_op_id=int(op["id"]),
            ):
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
            released += 1
            released_quantity += max(1, safe_int(op["quantity"]) or 1)
            print(
                f"[满载随机释放] {op['market_hash_name']} | asset={asset_id or '-'} | "
                f"listing={listing_id} | 活跃挂单槽连续满载 {full_seconds / 3600.0:.2f} 小时 | "
                f"随机释放比例 {release_fraction * 100:g}% | Steam撤单成功，资产已恢复可上架"
            )
        if released:
            remaining_occupied = max(0, remote_occupied - released_quantity)
            self._observe_case_listing_capacity(
                occupied=remaining_occupied,
                capacity=capacity,
                snapshot_complete=True,
            )
        return released

    def _guadao_listed_age_seconds(self, op: Any, *, now: datetime) -> float | None:
        created_at = _parse_iso(op["created_at"])
        if created_at is None:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (now.astimezone(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds(),
        )

    def _is_stale_guadao_listed_operation(self, op: Any, *, now: datetime) -> bool:
        age_seconds = self._guadao_listed_age_seconds(op, now=now)
        return age_seconds is not None and age_seconds >= GUADAO_STALE_LISTED_CANCEL_AFTER_SECONDS

    def run_guadao_stale_listing_recheck_task(self) -> dict[str, Any]:
        """Independently recheck old Steam listings without running full sync.

        This maintenance path deliberately consumes only positive ``MyListings``
        evidence.  An absent listing is inconclusive here: sale history,
        inventory and confirmation reconciliation remain the responsibility of
        the regular account-sync state machine.
        """

        now = _now_utc()
        due_by_account: dict[str, dict[str, Any]] = {}
        due_count = 0
        for op in self.db.list_pool_operations_by_type(
            OP_SELL_STEAM,
            status="listed",
            limit=5000,
        ):
            if not self._is_stale_guadao_listed_operation(op, now=now):
                continue
            age_seconds = self._guadao_listed_age_seconds(op, now=now)
            if age_seconds is None:
                continue
            note = _read_note(op["note"])
            if not self._stale_listed_recheck_due(note, now=now):
                continue

            steam_id64 = self._operation_steam_id64(op)
            requested_account_id = str(note.get("steamAccountId") or "").strip() or None
            account = self._account_by_id(requested_account_id) if requested_account_id else None
            attribution_error: str | None = None
            if requested_account_id and account is None:
                attribution_error = "Steam account attribution is unavailable"
            elif account is None and steam_id64:
                account = self._account_by_steam_id64(steam_id64)
                if account is None and getattr(self, "account_store", None) is not None:
                    # A SteamID by itself is not enough to select a client:
                    # the current client may belong to another account.  The
                    # maintenance task must fail closed rather than guessing
                    # which account owns this old listing.
                    attribution_error = "Steam account attribution is unavailable"

            account_id = account.id if account is not None else None
            if account is not None:
                account_steam_id64 = str(account.steam_id64 or "").strip() or None
                if steam_id64 and account_steam_id64 and steam_id64 != account_steam_id64:
                    attribution_error = "Steam account attribution does not match operation SteamID"
                elif not steam_id64:
                    steam_id64 = account_steam_id64
                if not steam_id64:
                    attribution_error = "Steam account attribution is unavailable"
            elif not steam_id64:
                attribution_error = "Steam account attribution is unavailable"

            group_key = (
                f"attribution-error:{int(op['id'])}"
                if attribution_error
                else f"account:{account_id}"
                if account_id
                else f"steam:{steam_id64}"
                if steam_id64
                else f"unattributed:{int(op['id'])}"
            )
            group = due_by_account.setdefault(
                group_key,
                {
                    "account": account,
                    "accountId": account_id,
                    "steamId64": steam_id64,
                    "attributionError": attribution_error,
                    "operations": [],
                },
            )
            group["operations"].append((op, note, age_seconds))
            due_count += 1

        result: dict[str, Any] = {
            "ok": True,
            "runId": f"stale-{uuid.uuid4().hex[:16]}",
            "startedAt": now.astimezone(timezone.utc).isoformat(),
            "accounts": sum(
                1 for group in due_by_account.values() if not group.get("attributionError")
            ),
            "due": due_count,
            "checked": 0,
            "kept": 0,
            "removeAttempts": 0,
            "removed": 0,
            "removeFailed": 0,
            "unmatched": 0,
            "deferred": 0,
            "priceDeferred": 0,
            "attributionDeferred": 0,
            "removedOperations": [],
            "removeFailedOperations": [],
            "unmatchedOperations": [],
        }
        def defer(
            op: Any,
            note: dict[str, Any],
            age_seconds: float,
            reason: str,
            *,
            now_value: datetime = now,
        ) -> None:
            checked_at = now_value.astimezone(timezone.utc)
            note["staleListedAgeHours"] = round(max(0.0, age_seconds) / 3600.0, 4)
            note["staleListedAgeSource"] = "pool_operations.created_at"
            note["staleListedCheckedAt"] = checked_at.isoformat()
            note["staleListedNextCheckAt"] = (
                checked_at + timedelta(seconds=self._stale_listed_deferred_retry_after_seconds())
            ).isoformat()
            note["staleListedCleanupStatus"] = "check_deferred"
            note["staleListedCleanupReason"] = str(reason)[:500]
            self._merge_stale_listing_note_if_still_listed(op, note)

        for group in due_by_account.values():
            operations = list(group["operations"])
            account = group.get("account")
            account_id = str(group.get("accountId") or "").strip() or None
            steam_id64 = str(group.get("steamId64") or "").strip() or None
            attribution_error = str(group.get("attributionError") or "").strip() or None
            if attribution_error:
                for op, note, age_seconds in operations:
                    defer(op, note, age_seconds, attribution_error)
                result["deferred"] += len(operations)
                result["attributionDeferred"] += len(operations)
                continue

            # The runtime cookie gate normally prevents this branch.  The
            # explicit health check keeps this standalone entry point fail-safe
            # for direct callers and avoids an implicit relogin from
            # ``_steam_client_for_account``.
            if account_id:
                health_loader = getattr(self.db, "get_steam_cookie_health", None)
                try:
                    health = health_loader(account_id) if callable(health_loader) else None
                except Exception as exc:
                    for op, note, age_seconds in operations:
                        defer(op, note, age_seconds, f"Steam Cookie health unavailable: {exc}")
                    result["deferred"] += len(operations)
                    continue
                if callable(health_loader):
                    if health is None:
                        for op, note, age_seconds in operations:
                            defer(op, note, age_seconds, "Steam Cookie health is unavailable")
                        result["deferred"] += len(operations)
                        continue
                    if str(health["status"] or "") != "valid":
                        for op, note, age_seconds in operations:
                            defer(op, note, age_seconds, "Steam Cookie is not ready")
                        result["deferred"] += len(operations)
                        continue
                if account is not None and not str(account.cookies or "").strip():
                    for op, note, age_seconds in operations:
                        defer(op, note, age_seconds, "Steam Cookie is not ready")
                    result["deferred"] += len(operations)
                    continue

            try:
                client = self._steam_client_for_account(
                    account,
                    steam_id64,
                    validate_session=False,
                    allow_relogin=False,
                )
            except Exception as exc:
                client = None
                client_error = f"Steam client unavailable: {exc}"
            else:
                client_error = "Steam client unavailable"
            if client is None:
                for op, note, age_seconds in operations:
                    defer(op, note, age_seconds, client_error)
                result["deferred"] += len(operations)
                continue

            try:
                # This read is evidence for a possible destructive stale-listing
                # action.  Keep it in the scheduler's P0 safety lane so a large
                # P1 rebuy backlog cannot leave the maintenance task waiting in
                # the ordinary P2 account-sync queue.  A few isolated test or
                # plugin clients still expose the old no-argument method; keep
                # those clients usable without weakening the production path.
                active_loader = getattr(client, "list_active_listings")
                try:
                    active = list(active_loader(safety_terminal=True))
                except TypeError as exc:
                    if "safety_terminal" not in str(exc):
                        raise
                    active = list(active_loader())
            except Exception as exc:
                for op, note, age_seconds in operations:
                    defer(op, note, age_seconds, f"Steam active listings unavailable: {exc}")
                result["deferred"] += len(operations)
                continue

            active_listing_ids, active_asset_ids = self._active_listing_identity_sets(active)
            market_snapshot_cache: dict[
                str, tuple[float | None, float | None, str | None]
            ] = {}
            for op, note, age_seconds in operations:
                listing_id = str(note.get("listingId") or "").strip()
                asset_id = str(op["asset_id"] or "").strip()
                if not self._listing_is_active(
                    active_listing_ids=active_listing_ids,
                    active_asset_ids=active_asset_ids,
                    listing_id=listing_id,
                    asset_id=asset_id,
                ):
                    defer(
                        op,
                        note,
                        age_seconds,
                        "本轮 Steam 活跃挂单中未取得对应 listing 证据；等待完整卖出/状态复查",
                    )
                    result["unmatched"] += 1
                    result["unmatchedOperations"].append(
                        {
                            "operationId": int(op["id"]),
                            "marketHashName": str(op["market_hash_name"] or ""),
                            "assetId": str(op["asset_id"] or "") or None,
                            "listingId": listing_id or None,
                        }
                    )
                    continue

                result["checked"] += 1
                note["staleListedAgeHours"] = round(max(0.0, age_seconds) / 3600.0, 4)
                note["staleListedAgeSource"] = "pool_operations.created_at"
                keep_decision = self._keep_stale_active_listing_if_still_competitive(
                    op,
                    note,
                    client=client,
                    now=now,
                    market_snapshot_cache=market_snapshot_cache,
                )
                if keep_decision is True:
                    if str(note.get("staleListedCleanupStatus") or "") == "check_deferred":
                        result["deferred"] += 1
                        result["priceDeferred"] += 1
                    else:
                        result["kept"] += 1
                    continue
                if keep_decision is None:
                    # The normal sync won the local race while evidence was
                    # being read.  Do not classify the newer terminal state as
                    # a price failure or trigger a false ServerChan warning.
                    result["deferred"] += 1
                    continue

                removed = self._remove_stale_active_guadao_listing(
                    op,
                    note,
                    client=client,
                    active=active,
                    active_listing_ids=active_listing_ids,
                )
                if removed is None:
                    result["deferred"] += 1
                elif removed:
                    result["removeAttempts"] += 1
                    result["removed"] += 1
                    result["removedOperations"].append(
                        {
                            "operationId": int(op["id"]),
                            "marketHashName": str(op["market_hash_name"] or ""),
                            "assetId": asset_id or None,
                            "listingId": str(note.get("listingId") or "") or None,
                            "reason": str(note.get("staleListedRemoveReason") or ""),
                        }
                    )
                else:
                    result["removeAttempts"] += 1
                    result["removeFailed"] += 1
                    result["removeFailedOperations"].append(
                        {
                            "operationId": int(op["id"]),
                            "marketHashName": str(op["market_hash_name"] or ""),
                            "assetId": asset_id or None,
                            "listingId": str(note.get("listingId") or "") or None,
                            "reason": str(note.get("staleListedCleanupReason") or ""),
                        }
                    )

        summary = (
            f"账号 {result['accounts']} 个 | 到期 {result['due']} 笔 | 实查 {result['checked']} 笔 | "
            f"最低价保护保留 {result['kept']} 笔 | 撤单成功 {result['removed']} 笔 | "
            f"撤单失败 {result['removeFailed']} 笔 | 活跃挂单未匹配 {result['unmatched']} 笔 | "
            f"价格读取延期 {result['priceDeferred']} 笔"
            f" | 撤单尝试 {result['removeAttempts']} 笔"
        )
        result["summary"] = summary
        print(f"[老挂单检查完成] {summary}")
        self._emit_guadao_local_event(
            operation="stale_listing_recheck",
            message="老挂单检查完成",
            level="INFO",
            context=result,
        )
        return result

    def _stale_listed_recheck_after_seconds(self) -> float:
        try:
            hours = float(self.config.stale_listed_recheck_hours)
        except (TypeError, ValueError):
            return 24.0 * 3600.0
        if not math.isfinite(hours) or hours <= 0:
            return 24.0 * 3600.0
        return hours * 3600.0

    def _stale_listed_deferred_retry_after_seconds(self) -> float:
        return min(
            self._stale_listed_recheck_after_seconds(),
            float(GUADAO_STALE_LISTED_DEFERRED_RETRY_SECONDS),
        )

    def _stale_listed_ratio_tolerance(self) -> float:
        try:
            pct = float(self.config.stale_listed_max_ratio_tolerance_pct)
        except (TypeError, ValueError):
            return 0.015
        if not math.isfinite(pct) or pct <= 0:
            return 0.0
        return pct / 100.0

    def _stale_listed_recheck_due(self, note: dict[str, Any], *, now: datetime) -> bool:
        next_check_at = _parse_iso(str(note.get("staleListedNextCheckAt") or ""))
        if str(note.get("staleListedCleanupStatus") or "") == "check_deferred":
            checked_at = _parse_iso(str(note.get("staleListedCheckedAt") or ""))
            if checked_at is not None:
                if checked_at.tzinfo is None:
                    checked_at = checked_at.replace(tzinfo=timezone.utc)
                deferred_check_at = checked_at.astimezone(timezone.utc) + timedelta(
                    seconds=self._stale_listed_deferred_retry_after_seconds()
                )
                if next_check_at is None or deferred_check_at < next_check_at:
                    next_check_at = deferred_check_at
        if next_check_at is None:
            return True
        if next_check_at.tzinfo is None:
            next_check_at = next_check_at.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc) >= next_check_at.astimezone(timezone.utc)

    def _current_c5_market_price(self, market_hash_name: str) -> tuple[float | None, str | None]:
        """Read the current category market price, not an account inventory asset price."""
        try:
            payload = self.c5_client.price_batch(
                [market_hash_name],
                app_id=self.settings.app_id,
            )
        except Exception as exc:
            return None, f"C5 price_batch unavailable: {exc}"
        if not isinstance(payload, dict):
            return None, "current C5 market price is unavailable"
        item = payload.get(market_hash_name)
        price = safe_float(item.get("price")) if isinstance(item, dict) else None
        # A stale-listing check is destructive.  ``float('nan')`` and
        # infinities are not usable market evidence: comparisons with NaN
        # would otherwise evaluate as false and fall through to cancellation.
        if price is None or price <= 0:
            return None, "current C5 market price is unavailable"
        if not math.isfinite(price):
            return None, "current C5 market price is unavailable (non-finite)"
        return price, None

    def _stale_listed_market_snapshot(
        self,
        market_hash_name: str,
        *,
        client: SteamMarketClient,
        cache: dict[str, tuple[float | None, float | None, str | None]],
    ) -> tuple[float | None, float | None, str | None]:
        cached = cache.get(market_hash_name)
        if cached is not None:
            return cached

        try:
            orderbook_loader = getattr(client, "order_book")
            try:
                # This is destructive-action evidence for the independent
                # maintenance lane. Keep it in P0 so a large P1 rebuy queue
                # cannot make the price check expire before it is read.
                payload = orderbook_loader(
                    app_id=self.settings.app_id,
                    market_hash_name=market_hash_name,
                    safety_terminal=True,
                )
            except TypeError as exc:
                # A few legacy injected test/plugin clients still expose the
                # old two-argument method. Production SteamMarketClient has
                # the safety_terminal parameter; retaining this compatibility
                # fallback does not weaken the production request path.
                if "safety_terminal" not in str(exc):
                    raise
                payload = orderbook_loader(
                    app_id=self.settings.app_id,
                    market_hash_name=market_hash_name,
                )
        except Exception as exc:
            result = (None, None, f"Steam orderbook unavailable: {exc}")
            cache[market_hash_name] = result
            return result

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        actual_currency = safe_int(data.get("eCurrency"))
        # A missing currency marker is incomplete evidence, just like a
        # mismatched currency.  The stale-listing task is destructive and
        # must never infer CNY prices from an unlabelled Steam response.
        if actual_currency is None:
            result = (
                None,
                None,
                f"Steam orderbook currency unavailable: expected={self.config.steam_currency}",
            )
            cache[market_hash_name] = result
            return result
        if actual_currency != int(self.config.steam_currency):
            result = (
                None,
                None,
                f"Steam orderbook currency mismatch: expected={self.config.steam_currency} actual={actual_currency}",
            )
            cache[market_hash_name] = result
            return result

        # 老挂单只认 Steam compact 卖家墙第一档，不读取买家盘口或第三方价格。
        compact_sell_orders = data.get("rgCompactSellOrders")
        summary = summarize_orderbook_prices(
            {"rgCompactSellOrders": compact_sell_orders},
            wall_min_count=1,
            price_offset=0.0,
        )
        floor_price = safe_float(summary.seller_floor_price)
        error: str | None = None
        if floor_price is None or floor_price <= 0:
            error = "Steam compact sell orderbook has no floor price"
            floor_price = None
            c5_price = None
        elif not math.isfinite(floor_price):
            error = "Steam compact sell orderbook has no floor price (non-finite)"
            floor_price = None
            c5_price = None
        else:
            c5_price, error = self._current_c5_market_price(market_hash_name)
        result = (floor_price, c5_price, error)
        cache[market_hash_name] = result
        return result

    def _merge_stale_listing_note_if_still_listed(
        self,
        op: Any,
        note: dict[str, Any],
    ) -> bool:
        """Merge maintenance fields without overwriting a concurrent terminal note.

        A stale-listing evidence walk can overlap the regular Steam sync.  The
        sync may record a sale or confirmation while this task is waiting on
        Steam/C5.  Only merge fields owned by this maintenance task, and only
        while the authoritative operation row is still ``listed``.
        """

        conn = self.db.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status, note FROM pool_operations WHERE id = ?",
                (int(op["id"]),),
            ).fetchone()
            if current is None or str(current["status"] or "") != "listed":
                conn.rollback()
                return False

            latest_note = _read_note(current["note"])
            for key, value in note.items():
                if key.startswith("staleListed"):
                    latest_note[key] = value
                elif key == "listingId" and not str(latest_note.get(key) or "").strip():
                    # Backfill a missing ID, but never replace a newer ID that
                    # another reconciliation path may already have recorded.
                    latest_note[key] = value
            cursor = conn.execute(
                """
                UPDATE pool_operations
                SET note = ?
                WHERE id = ? AND status = 'listed'
                """,
                (_build_note(latest_note), int(op["id"])),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

    def _keep_stale_active_listing_if_still_competitive(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        client: SteamMarketClient,
        now: datetime,
        market_snapshot_cache: dict[str, tuple[float | None, float | None, str | None]],
    ) -> bool | None:
        if not self._stale_listed_recheck_due(note, now=now):
            return True

        market_hash_name = str(op["market_hash_name"])
        floor_price, c5_price, snapshot_error = self._stale_listed_market_snapshot(
            market_hash_name,
            client=client,
            cache=market_snapshot_cache,
        )
        checked_at = now.astimezone(timezone.utc)
        note["staleListedCheckedAt"] = checked_at.isoformat()
        note["staleListedCurrentFloorPrice"] = floor_price
        note["staleListedCurrentC5Price"] = c5_price

        raw_list_price = note.get("steamListPrice")
        if raw_list_price is None:
            raw_list_price = op["expected_price"]
        list_price = safe_float(raw_list_price)
        next_check_at = checked_at + timedelta(
            seconds=self._stale_listed_recheck_after_seconds()
        )
        note["staleListedNextCheckAt"] = next_check_at.isoformat()

        raw_steam_net_factor = note.get("steamNetFactorAtOpen")
        steam_net_factor = (
            safe_float(raw_steam_net_factor)
            if raw_steam_net_factor is not None
            else safe_float(self.config.steam_net_factor)
        )
        raw_hard_max_ratio = note.get("guadaoMaxListingRatioAtOpen")
        hard_max_ratio = (
            safe_float(raw_hard_max_ratio)
            if raw_hard_max_ratio is not None
            else safe_float(self.config.guadao_max_listing_ratio)
        )
        tolerance = safe_float(self._stale_listed_ratio_tolerance())
        allowed_ratio = (
            hard_max_ratio + tolerance
            if hard_max_ratio is not None
            and tolerance is not None
            and math.isfinite(hard_max_ratio)
            and math.isfinite(tolerance)
            and hard_max_ratio > 0
            and tolerance >= 0
            else None
        )
        steam_after_tax = (
            list_price * steam_net_factor
            if list_price is not None
            and steam_net_factor is not None
            and math.isfinite(list_price)
            and math.isfinite(steam_net_factor)
            and list_price > 0
            and steam_net_factor > 0
            else None
        )
        current_ratio = (
            c5_price / steam_after_tax
            if c5_price is not None
            and steam_after_tax is not None
            and math.isfinite(c5_price)
            and math.isfinite(steam_after_tax)
            and steam_after_tax > 0
            else None
        )
        if current_ratio is not None and not math.isfinite(current_ratio):
            current_ratio = None
        note["staleListedCurrentRatio"] = current_ratio
        note["staleListedAllowedMaxRatio"] = allowed_ratio
        note["staleListedRatioTolerancePct"] = self.config.stale_listed_max_ratio_tolerance_pct

        if snapshot_error:
            next_check_at = checked_at + timedelta(
                seconds=self._stale_listed_deferred_retry_after_seconds()
            )
            note["staleListedNextCheckAt"] = next_check_at.isoformat()
            note["staleListedCleanupStatus"] = "check_deferred"
            note["staleListedCleanupReason"] = snapshot_error
            if not self._merge_stale_listing_note_if_still_listed(op, note):
                return None
            print(
                f"[挂刀老挂单复查延后] {market_hash_name} | "
                f"无法安全读取当前最低价/补仓价，保留挂单，下次复查 {next_check_at.isoformat()} | "
                f"原因: {snapshot_error}"
            )
            return True
        if list_price is not None and floor_price is not None:
            # 本单价格低于盘口第一档时同样位于最前面，不能因为盘口短时未包含本单而误撤。
            is_at_market_floor = list_price <= floor_price + 0.005
            note["staleListedAtMarketFloor"] = is_at_market_floor
        if (
            list_price is None
            or not math.isfinite(list_price)
            or floor_price is None
            or not math.isfinite(floor_price)
            or c5_price is None
            or not math.isfinite(c5_price)
            or steam_net_factor is None
            or not math.isfinite(steam_net_factor)
            or allowed_ratio is None
            or not math.isfinite(allowed_ratio)
            or current_ratio is None
            or not math.isfinite(current_ratio)
        ):
            next_check_at = checked_at + timedelta(
                seconds=self._stale_listed_deferred_retry_after_seconds()
            )
            note["staleListedNextCheckAt"] = next_check_at.isoformat()
            note["staleListedCleanupStatus"] = "check_deferred"
            note["staleListedCleanupReason"] = "listing price, ratio, or protection factor is unavailable or non-finite"
            if not self._merge_stale_listing_note_if_still_listed(op, note):
                return None
            return True
        is_at_market_floor = bool(note.get("staleListedAtMarketFloor"))
        if is_at_market_floor and current_ratio <= allowed_ratio:
            note["staleListedCleanupStatus"] = "kept_at_market_floor"
            note["staleListedCleanupReason"] = "still at market floor and ratio remains acceptable"
            if not self._merge_stale_listing_note_if_still_listed(op, note):
                return None
            print(
                f"[挂刀老挂单继续等待] {market_hash_name} | 挂价 {list_price:.2f} | "
                f"当前最低价 {floor_price:.2f} | C5价 {c5_price:.2f} | "
                f"挂刀比例 {_format_pct(current_ratio)} <= 允许上限 {_format_pct(allowed_ratio)} | "
                f"下次复查 {next_check_at.isoformat()}"
            )
            return True

        if not is_at_market_floor:
            note["staleListedRemoveReason"] = (
                "listed more than 48 hours and no longer at market floor"
            )
        else:
            note["staleListedRemoveReason"] = "stale listing ratio exceeds tolerated maximum"
        return False

    def _finalize_stale_listing_removal_if_still_listed(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        asset_id: str,
        asset_restore_status: str,
    ) -> tuple[bool, str | None]:
        """Commit a successful remote cancel only if the operation is still listed.

        The Steam request can wait in a shared queue while the normal account
        sync advances the same operation.  Use one SQLite transaction to read
        the latest row, merge only this task's stale-listing fields, and update
        both the operation and asset conditionally.  A concurrent terminal
        state therefore wins and its note/evidence is never overwritten.
        """

        conn = self.db.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status, note FROM pool_operations WHERE id = ?",
                (int(op["id"]),),
            ).fetchone()
            if current is None:
                conn.rollback()
                return False, "missing"
            current_status = str(current["status"] or "")
            if current_status != "listed":
                conn.rollback()
                return False, current_status

            latest_note = _read_note(current["note"])
            requested_listing_id = str(note.get("listingId") or "").strip()
            latest_listing_id = str(latest_note.get("listingId") or "").strip()
            if (
                requested_listing_id
                and latest_listing_id
                and requested_listing_id != latest_listing_id
            ):
                conn.rollback()
                return False, "listing_id_changed"
            # Keep concurrent sale/confirmation fields from the latest note;
            # only merge fields owned by this stale-maintenance decision.
            for key, value in note.items():
                if key.startswith("staleListed") or key == "assetRestoredStatus":
                    latest_note[key] = value
                elif key == "listingId" and not latest_listing_id:
                    latest_note[key] = value
            latest_note["staleListedCleanupStatus"] = "removed"
            latest_note["staleListedRemovedAt"] = utc_now_iso()
            latest_note["staleListedRemoveReason"] = (
                note.get("staleListedRemoveReason") or "listed more than 48 hours"
            )
            latest_note["assetRestoredStatus"] = asset_restore_status

            cursor = conn.execute(
                """
                UPDATE pool_operations
                SET status = 'canceled', note = ?
                WHERE id = ? AND status = 'listed'
                """,
                (_build_note(latest_note), int(op["id"])),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                latest = conn.execute(
                    "SELECT status FROM pool_operations WHERE id = ?",
                    (int(op["id"]),),
                ).fetchone()
                return False, str(latest["status"] or "") if latest is not None else "missing"

            if asset_id:
                conn.execute(
                    """
                    UPDATE inventory_assets
                    SET status = ?, last_seen_at = ?
                    WHERE asset_id = ?
                    """,
                    (asset_restore_status, utc_now_iso(), asset_id),
                )
            conn.commit()
            return True, None
        except Exception:
            conn.rollback()
            raise


    def _remove_stale_active_guadao_listing(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        client: SteamMarketClient,
        active: list[Any],
        active_listing_ids: set[str],
    ) -> bool | None:
        asset_id = str(op["asset_id"] or "").strip()
        listing_id = str(note.get("listingId") or "").strip()

        def defer_identity_conflict(reason: str) -> None:
            checked_at = _now_utc()
            next_check_at = checked_at + timedelta(
                seconds=self._stale_listed_deferred_retry_after_seconds()
            )
            note["staleListedCleanupStatus"] = "check_deferred"
            note["staleListedCleanupReason"] = reason
            note["staleListedCheckedAt"] = checked_at.isoformat()
            note["staleListedNextCheckAt"] = next_check_at.isoformat()
            self._merge_stale_listing_note_if_still_listed(op, note)
            print(
                f"[挂刀老挂单撤单延后] {op['market_hash_name']} | asset={asset_id or '-'} | "
                f"{reason}；下次复查 {next_check_at.isoformat()}"
            )

        # A listing ID must not be treated as authoritative for a different
        # asset.  Steam listing IDs should be unique, but a stale/corrupted
        # local association is destructive-action ambiguity, not proof that
        # the other asset's listing should be removed.
        if listing_id and asset_id:
            for active_listing in active:
                active_listing_id = str(
                    getattr(active_listing, "listing_id", "") or ""
                ).strip()
                if active_listing_id != listing_id:
                    continue
                active_asset_id = str(
                    getattr(active_listing, "asset_id", "") or ""
                ).strip()
                if active_asset_id and active_asset_id != asset_id:
                    defer_identity_conflict(
                        "active listing asset id conflicts with local operation; refusing to cancel"
                    )
                    return None
                break

        # An asset can be relisted while the local operation note still holds
        # the previous listing ID.  Matching by asset is useful when the note
        # has no ID at all, but it is unsafe when a different ID is explicitly
        # recorded: cancelling the replacement listing would destroy the new
        # order and leave the old operation unresolved.  Defer until the normal
        # reconciliation path records the new ID.
        if listing_id and listing_id not in active_listing_ids and asset_id:
            active_asset_listing_id = self._active_listing_id_for_asset(active, asset_id)
            if active_asset_listing_id and active_asset_listing_id != listing_id:
                defer_identity_conflict(
                    "active listing id changed for asset; refusing to cancel replacement listing"
                )
                return None
        removable_listing_id = listing_id if listing_id and listing_id in active_listing_ids else None
        if removable_listing_id is None and asset_id:
            removable_listing_id = self._active_listing_id_for_asset(active, asset_id)
            if removable_listing_id:
                note["listingId"] = removable_listing_id
        if not removable_listing_id:
            checked_at = _now_utc()
            next_check_at = checked_at + timedelta(
                seconds=self._stale_listed_deferred_retry_after_seconds()
            )
            note["staleListedCleanupStatus"] = "check_deferred"
            note["staleListedCleanupReason"] = (
                "active listing matched asset but listing id is unavailable"
            )
            note["staleListedCheckedAt"] = checked_at.isoformat()
            note["staleListedNextCheckAt"] = next_check_at.isoformat()
            self._merge_stale_listing_note_if_still_listed(op, note)
            print(
                f"[stale listing recheck deferred] {op['market_hash_name']} | asset={asset_id or '-'} | "
                f"active listing has no removable listingId; retry {next_check_at.isoformat()}"
            )
            return None

        # The regular Steam sync and sale-evidence workers can advance this
        # operation while the maintenance task is reading market evidence.
        # Re-read the authoritative row immediately before the destructive
        # request; a stale snapshot must never cancel a listing that has
        # already left the 'listed' state.
        current = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (int(op["id"]),),
        ).fetchone()
        current_status = str(current["status"] or "") if current is not None else ""
        if current is None or current_status != "listed":
            print(
                f"[stale listing recheck skipped] {op['market_hash_name']} | "
                f"operation is no longer listed (current={current_status or 'missing'})"
            )
            return None

        # Re-check the live runtime gate immediately before the destructive
        # Steam request. A task may have been claimed while enabled and then
        # the user may disable the executor while its evidence walk is still
        # running. Fail closed: defer the operation and leave it listed.
        guard = getattr(self, "_new_action_guard", None)
        gate_blocked = bool(getattr(self.config, "dry_run", False))
        gate_reason = "dry-run mode before stale listing removal" if gate_blocked else None
        if not gate_blocked and guard is not None:
            try:
                gate_blocked = not bool(guard())
            except Exception:
                gate_blocked = True
            if gate_blocked:
                gate_reason = "executor disabled before stale listing removal"
        if gate_blocked:
            checked_at = _now_utc()
            next_check_at = checked_at + timedelta(
                seconds=self._stale_listed_deferred_retry_after_seconds()
            )
            note["staleListedCheckedAt"] = checked_at.isoformat()
            note["staleListedNextCheckAt"] = next_check_at.isoformat()
            note["staleListedCleanupStatus"] = "check_deferred"
            note["staleListedCleanupReason"] = gate_reason
            self._merge_stale_listing_note_if_still_listed(op, note)
            print(
                f"[stale listing recheck deferred] {op['market_hash_name']} | "
                f"{gate_reason}; Steam cancellation was not sent; retry {next_check_at.isoformat()}"
            )
            return None

        remover = getattr(client, "remove_listing", None)
        if not callable(remover):
            remove_error = "Steam client does not support remove_listing"
            removed = False
        else:
            try:
                execution_guard = guard if callable(guard) else None
                if execution_guard is None:
                    removed = bool(remover(removable_listing_id))
                else:
                    # The shared scheduler evaluates this guard again after
                    # queue admission, closing the check-then-enqueue race.
                    removed = bool(
                        remover(
                            removable_listing_id,
                            execution_guard=execution_guard,
                        )
                    )
                remove_error = None if removed else "Steam remove_listing returned false"
            except SteamRequestGuardRejected as exc:
                # The runtime may be disabled (or the C5 circuit may open)
                # while the request waits in Steam's queue. No remote action
                # ran in this case; classify it as a deferred check, not a
                # failed/removed listing, and retry on the normal short delay.
                checked_at = _now_utc()
                next_check_at = checked_at + timedelta(
                    seconds=self._stale_listed_deferred_retry_after_seconds()
                )
                note["staleListedCleanupStatus"] = "check_deferred"
                note["staleListedCleanupReason"] = (
                    "execution gate changed before Steam stale-listing removal: "
                    f"{exc}"
                )[:500]
                note["staleListedCheckedAt"] = checked_at.isoformat()
                note["staleListedNextCheckAt"] = next_check_at.isoformat()
                self._merge_stale_listing_note_if_still_listed(op, note)
                print(
                    f"[挂刀老挂单撤单延后] {op['market_hash_name']} | "
                    f"执行闸门在 Steam 请求执行前关闭，保留挂单；下次复查 {next_check_at.isoformat()}"
                )
                return None
            except Exception as exc:
                removed = False
                remove_error = str(exc)

        if not removed:
            checked_at = _now_utc()
            note["staleListedCleanupStatus"] = "remove_failed"
            note["staleListedCleanupReason"] = remove_error
            note["staleListedCheckedAt"] = checked_at.isoformat()
            note["staleListedNextCheckAt"] = (
                checked_at + timedelta(seconds=self._stale_listed_deferred_retry_after_seconds())
            ).isoformat()
            if not self._merge_stale_listing_note_if_still_listed(op, note):
                print(
                    f"[stale listing recheck skipped] {op['market_hash_name']} | "
                    "remote removal failed after the operation left listed; preserving newer state"
                )
                return None
            print(
                f"[挂刀老挂单撤单失败] {op['market_hash_name']} | asset={asset_id or '-'} | "
                f"listing={removable_listing_id} | 原因: {remove_error}"
            )
            return False

        asset_restore_status = "available"
        if asset_id:
            asset_row = self.db.get_asset(asset_id)
            # A successfully removed listing releases the local reservation,
            # but a non-tradable asset must remain locked until the next
            # inventory sync marks it tradable.  Marking every asset
            # ``available`` here would make a trade-locked item look eligible
            # for immediate relisting.
            if asset_row is None or not bool(asset_row["tradable"]):
                asset_restore_status = "locked"
        note["staleListedCleanupStatus"] = "removed"
        note["staleListedRemovedAt"] = utc_now_iso()
        note["staleListedRemoveReason"] = note.get("staleListedRemoveReason") or "listed more than 48 hours"
        note["assetRestoredStatus"] = asset_restore_status
        finalized, conflict_status = self._finalize_stale_listing_removal_if_still_listed(
            op,
            note,
            asset_id=asset_id,
            asset_restore_status=asset_restore_status,
        )
        if not finalized:
            # The remote cancel succeeded, but another state-machine worker
            # won the local race. Do not overwrite its terminal state or
            # restore its asset; the next normal reconciliation will observe
            # the remote result.
            print(
                f"[挂刀老挂单本地终态冲突] {op['market_hash_name']} | "
                f"Steam撤单已成功，但本地流水已变为 {conflict_status or 'missing'}；保留并发终态"
            )
            return None
        if not self._has_other_open_guadao_operation(
            op["market_hash_name"],
            exclude_op_id=int(op["id"]),
        ):
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
        print(
            f"[挂刀老挂单已撤单恢复] {op['market_hash_name']} | asset={asset_id or '-'} | "
            f"listing={removable_listing_id} | 已超过48小时且不再满足继续等待条件，"
            "Steam撤单成功后恢复本地资产"
        )
        return True

    def _mark_stale_listed_manual_required(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        reason: str,
    ) -> bool:
        """Move a stale listing to manual review only if it is still listed.

        The full sync can overlap the independent stale-listing task.  A
        newer ``sold``/``canceled`` state must win instead of being reverted to
        ``manual_required`` by this older snapshot.
        """

        asset_id = str(op["asset_id"] or "").strip()
        listing_id = str(note.get("listingId") or "").strip()
        checked_at = utc_now_iso()
        note["staleListedCleanupStatus"] = "manual_required"
        note["staleListedManualRequiredAt"] = checked_at
        note["staleListedManualRequiredReason"] = reason
        note["manualReviewReason"] = reason
        conn = self.db.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status, note FROM pool_operations WHERE id = ?",
                (int(op["id"]),),
            ).fetchone()
            if current is None or str(current["status"] or "") != "listed":
                conn.rollback()
                print(
                    f"[挂刀老挂单人工检查跳过] {op['market_hash_name']} | "
                    f"流水已变为 {str(current['status'] or '') if current is not None else 'missing'}，保留更新后的状态"
                )
                return False
            latest_note = _read_note(current["note"])
            for key, value in note.items():
                if key.startswith("staleListed") or key == "manualReviewReason":
                    latest_note[key] = value
            cursor = conn.execute(
                """
                UPDATE pool_operations
                SET status = 'manual_required', note = ?
                WHERE id = ? AND status = 'listed'
                """,
                (_build_note(latest_note), int(op["id"])),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_LISTED)
        print(
            f"[挂刀老挂单人工检查] {op['market_hash_name']} | asset={asset_id or '-'} | "
            f"listing={listing_id or '-'} | 已超过48小时，远端不在售且无Steam卖出回执；未恢复本地资产"
        )
        return True

    def _backfill_listing_ids(
        self,
        *,
        client: SteamMarketClient | None = None,
        active_listings: list[Any] | None = None,
        operation_ids: set[int] | None = None,
    ) -> int:
        """上架确认后，Steam 才分配 listing_id。
        此方法查询当前活跃挂单，按 asset_id 匹配，把真实 listing_id 回填到 DB。
        """
        active_client = client or self.steam_client
        if not active_client:
            return 0
        ops = self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=200)
        if operation_ids is not None:
            selected_ids = {int(value) for value in operation_ids}
            ops = [op for op in ops if int(op["id"]) in selected_ids]
        empty_ops = [
            op for op in ops
            if not _read_note(op["note"]).get("listingId")
            and op["asset_id"]
            and self._operation_matches_client(op, active_client)
        ]
        if not empty_ops:
            return 0
        if active_listings is None:
            try:
                active_listings = active_client.list_active_listings()
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
        client: SteamMarketClient | None = None,
        account: Account | None = None,
    ) -> tuple[dict[str, Any], str]:
        note: dict[str, Any] = {
            "needsConfirmation": True,
            "confirmationStatus": "pending",
        }
        active_client = client or self.steam_client
        allow_global_credentials = active_client is self.steam_client and (
            account is None or (self.account is not None and account.id == self.account.id)
        )
        identity_secret = getattr(active_client, "identity_secret", None) or (account.identity_secret if account else None)
        device_id = getattr(active_client, "device_id", None) or (account.device_id if account else None)
        if allow_global_credentials:
            identity_secret = identity_secret or self._effective_steam_identity_secret()
            device_id = device_id or self._effective_steam_device_id()
        if not identity_secret or not device_id:
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

        if active_client is not None:
            pending_market_listing = self._pending_market_confirmation_listing(
                active_client,
                listing_id=listing_id,
                asset_id=asset_id,
            )
            if pending_market_listing is not None:
                pending_listing_id = str(getattr(pending_market_listing, "listing_id", "") or "").strip()
                if pending_listing_id:
                    note["listingId"] = pending_listing_id
                    note["marketPendingListingId"] = pending_listing_id
                    listing_id = pending_listing_id

        try:
            if not active_client:
                raise SteamMarketError("missing Steam client")
            confirmer = getattr(active_client, "confirm_listing_assets", None)
            if not callable(confirmer):
                raise SteamMarketError("Steam client does not support scoped listing confirmation")
            confirmed_count = confirmer(
                asset_ids=[asset_id],
                listing_ids=[listing_id] if listing_id else None,
            )
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
            pending_market_listing = self._pending_market_confirmation_listing(
                active_client,
                listing_id=listing_id,
                asset_id=asset_id,
            )
            if pending_market_listing is not None:
                pending_listing_id = str(getattr(pending_market_listing, "listing_id", "") or "").strip()
                note["listingId"] = pending_listing_id or listing_id
                note["marketPendingListingId"] = pending_listing_id
                note["confirmationStatus"] = "market_pending_visible"
                note["confirmationMessage"] = (
                    "Steam market mylistings shows this listing waiting for confirmation, "
                    "but mobileconf returned no confirmation"
                )
                removed, remove_error = self._remove_market_pending_confirmation_listing(
                    active_client,
                    pending_market_listing,
                )
                if removed:
                    note["needsConfirmation"] = False
                    note["confirmationStatus"] = "market_pending_removed"
                    note["marketPendingRemovedAt"] = utc_now_iso()
                    print(
                        f"[挂单待确认清理] {market_hash_name} | asset={asset_id} | "
                        f"listing={pending_listing_id or '-'} | "
                        "Steam 网页存在待确认挂单但移动确认列表为空，已自动撤下并释放资产"
                    )
                    return note, POOL_STATUS_HOLDING
                note["confirmationStatus"] = "market_pending_remove_failed"
                note["confirmationMessage"] = (
                    f"{note['confirmationMessage']}; automatic remove failed: {remove_error}"
                )
                self._market_pending_cleanup_failed_count += 1
                print(
                    f"[提醒] Steam 网页待确认挂单无法自动撤下 | {market_hash_name} | "
                    f"asset={asset_id} | listing={pending_listing_id or '-'} | error={remove_error}"
                )
                return note, POOL_STATUS_LISTING_PENDING
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
        if listing_id:
            note["listingId"] = listing_id
        try:
            active_listings = active_client.list_active_listings()
            active_listing_ids, active_asset_ids = self._active_listing_identity_sets(active_listings)
        except Exception as exc:
            confirmation_sent_at = note.get("confirmationSentAt") or utc_now_iso()
            note["confirmationStatus"] = "confirm_sent_waiting_active_listing"
            note["confirmationSentAt"] = confirmation_sent_at
            note["listingPendingAt"] = note.get("listingPendingAt") or confirmation_sent_at
            note["confirmationMessage"] = f"confirmed but active listing check failed: {exc}"
            self._pending_confirmation_count += 1
            return note, POOL_STATUS_LISTING_PENDING
        if not self._listing_is_active(
            active_listing_ids=active_listing_ids,
            active_asset_ids=active_asset_ids,
            listing_id=listing_id,
            asset_id=asset_id,
        ):
            confirmation_sent_at = note.get("confirmationSentAt") or utc_now_iso()
            note["confirmationStatus"] = "confirm_sent_waiting_active_listing"
            note["confirmationSentAt"] = confirmation_sent_at
            note["listingPendingAt"] = note.get("listingPendingAt") or confirmation_sent_at
            note["confirmationMessage"] = "confirmed but listing is not visible in Steam active listings yet"
            self._pending_confirmation_count += 1
            return note, POOL_STATUS_LISTING_PENDING
        if not listing_id:
            recovered_listing_id = self._active_listing_id_for_asset(active_listings, asset_id)
            if recovered_listing_id:
                note["listingId"] = recovered_listing_id
        note["activeVerifiedAt"] = utc_now_iso()
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
            if buy_count >= self.config.max_transfer_buy_per_cycle:
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
            exclude_reserved=True,
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

    def _lookup_steam_sale_receipt(
        self,
        client: SteamMarketClient,
        listing_id: str,
        *,
        max_pages: int = 3,
    ) -> dict[str, Any] | None:
        finder = getattr(client, "find_sale_receipt", None)
        if not listing_id or not callable(finder):
            return None
        try:
            receipt = finder(listing_id, max_pages=max_pages)
        except TypeError:
            try:
                receipt = finder(listing_id)
            except Exception:
                return None
        except Exception:
            return None
        return receipt if isinstance(receipt, dict) else None

    def _lookup_steam_sale_receipt_by_asset(
        self,
        client: SteamMarketClient,
        asset_id: str,
        *,
        max_pages: int = 3,
    ) -> dict[str, Any] | None:
        finder = getattr(client, "find_sale_receipt_by_asset", None)
        if not asset_id or not callable(finder):
            return None
        try:
            receipt = finder(asset_id, max_pages=max_pages)
        except TypeError:
            try:
                receipt = finder(asset_id)
            except Exception:
                return None
        except Exception:
            return None
        return receipt if isinstance(receipt, dict) else None

    def _lookup_steam_sale_receipt_for_listing_or_asset(
        self,
        client: SteamMarketClient,
        *,
        listing_id: str,
        asset_id: str,
        max_pages: int = 3,
    ) -> dict[str, Any] | None:
        sale_receipt = self._lookup_steam_sale_receipt(client, listing_id, max_pages=max_pages)
        if sale_receipt is None and asset_id:
            sale_receipt = self._lookup_steam_sale_receipt_by_asset(
                client,
                asset_id,
                max_pages=max_pages,
            )
        return sale_receipt

    def _steam_sale_receipt_deep_lookup_due(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        now: datetime,
    ) -> bool:
        """Return whether an unresolved listing is due for a bounded deep walk."""

        confirmation_status = str(note.get("confirmationStatus") or "")
        if confirmation_status == "confirm_sent_waiting_active_listing":
            # Confirmation is sent before the operation row is inserted. Old
            # rows only gained listingPendingAt during a later retry, so the
            # operation creation time is the truthful conservative fallback.
            pending_since = (
                _parse_iso(str(note.get("confirmationSentAt") or ""))
                or _parse_iso(str(op["created_at"] or ""))
                or _parse_iso(str(note.get("listingPendingAt") or ""))
            )
        else:
            pending_since = (
                _parse_iso(str(note.get("listingPendingAt") or ""))
                or _parse_iso(str(note.get("staleListedManualRequiredAt") or ""))
            )
        if pending_since is None:
            return False
        if pending_since.tzinfo is None:
            pending_since = pending_since.replace(tzinfo=timezone.utc)
        if (
            now - pending_since.astimezone(timezone.utc)
        ).total_seconds() < STEAM_SALE_RECEIPT_DEEP_LOOKUP_INITIAL_DELAY_SECONDS:
            return False
        last_deep = _parse_iso(str(note.get("saleEvidenceDeepLastAttemptAt") or ""))
        if last_deep is None:
            return True
        if last_deep.tzinfo is None:
            last_deep = last_deep.replace(tzinfo=timezone.utc)
        return (
            now - last_deep.astimezone(timezone.utc)
        ).total_seconds() >= STEAM_SALE_RECEIPT_DEEP_LOOKUP_INTERVAL_SECONDS

    def _lookup_steam_sale_receipts_for_operations(
        self,
        client: SteamMarketClient,
        operations: list[Any],
        *,
        active_listing_ids: set[str],
        active_asset_ids: set[str],
        now: datetime | None = None,
        raise_on_error: bool = False,
    ) -> _SteamSaleReceiptLookupOutcome:
        """Read one account history snapshot for every unresolved due operation.

        Routine checks stay on the first two pages.  Once an unresolved
        listing has waited long enough, one shared bounded deep walk covers all
        due operations for the account.  The deep attempt timestamp is only
        returned after a successful history read so transient Steam errors do
        not postpone the next recovery attempt for six hours.
        """

        checked_at = now or _now_utc()
        missing: list[tuple[Any, dict[str, Any]]] = []
        deep_due_ids: set[int] = set()
        for op in operations:
            if not self._operation_matches_client(op, client):
                continue
            note = _read_note(op["note"])
            listing_id = str(note.get("listingId") or "").strip()
            asset_id = str(op["asset_id"] or "").strip()
            if self._listing_is_active(
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
                listing_id=listing_id,
                asset_id=asset_id,
            ):
                continue
            if not listing_id and not asset_id:
                continue
            missing.append((op, note))
            if self._steam_sale_receipt_deep_lookup_due(op, note, now=checked_at):
                deep_due_ids.add(int(op["id"]))
        if not missing:
            return _SteamSaleReceiptLookupOutcome({}, set(), None, True, True)

        max_pages = (
            STEAM_SALE_RECEIPT_DEEP_LOOKUP_MAX_PAGES
            if deep_due_ids
            else STEAM_SALE_RECEIPT_FAST_LOOKUP_MAX_PAGES
        )
        results: dict[int, dict[str, Any] | None] = {
            int(op["id"]): None for op, _ in missing
        }
        coverage_complete = False
        lookup_succeeded = False
        rich_batch_finder = getattr(client, "find_sale_receipts_for_targets_with_coverage", None)
        batch_finder = getattr(client, "find_sale_receipts_for_targets", None)
        try:
            targets = [
                {
                    "key": str(int(op["id"])),
                    "listingId": str(note.get("listingId") or "").strip(),
                    "assetId": str(op["asset_id"] or "").strip(),
                    "createdAt": str(op["created_at"] or "").strip(),
                }
                for op, note in missing
            ]
            if callable(rich_batch_finder):
                rich_result = rich_batch_finder(targets, max_pages=max_pages)
                batch_results = getattr(rich_result, "receipts", None)
                if not isinstance(batch_results, dict):
                    raise SteamMarketError("Steam sale history coverage result is invalid")
                coverage_complete = bool(getattr(rich_result, "coverage_complete", False))
                for operation_id in results:
                    receipt = batch_results.get(str(operation_id))
                    if isinstance(receipt, dict):
                        results[operation_id] = receipt
                lookup_succeeded = bool(
                    getattr(rich_result, "lookup_succeeded", True)
                )
                lookup_error = str(getattr(rich_result, "error", "") or "") or None
                retry_at = str(getattr(rich_result, "retry_at", "") or "") or None
                pages_scanned = max(
                    0,
                    int(getattr(rich_result, "pages_scanned", 0) or 0),
                )
            elif callable(batch_finder):
                batch_results = batch_finder(targets, max_pages=max_pages)
                if isinstance(batch_results, dict):
                    for operation_id in results:
                        receipt = batch_results.get(str(operation_id))
                        if isinstance(receipt, dict):
                            results[operation_id] = receipt
                    # Legacy injected clients do not say whether their page
                    # walk reached history's real end.  They remain usable
                    # for receipt-positive transitions, but cannot authorize
                    # an inventory-based asset release.
                    lookup_succeeded = True
                    lookup_error = None
                    retry_at = None
                    pages_scanned = max_pages
                else:
                    raise SteamMarketError("Steam sale history batch result is invalid")
            else:
                # Compatibility for tests and older injected clients.  The
                # production Steam client supports the account-level batch
                # finder above, so real requests are not multiplied per op.
                for op, note in missing:
                    results[int(op["id"])] = self._lookup_steam_sale_receipt_for_listing_or_asset(
                        client,
                        listing_id=str(note.get("listingId") or "").strip(),
                        asset_id=str(op["asset_id"] or "").strip(),
                        max_pages=max_pages,
                    )
                lookup_succeeded = True
                lookup_error = None
                retry_at = None
                pages_scanned = max_pages
        except Exception as exc:
            if raise_on_error:
                raise
            return _SteamSaleReceiptLookupOutcome(
                results,
                set(),
                None,
                False,
                False,
                str(exc),
                (
                    getattr(exc, "retry_at").isoformat()
                    if isinstance(getattr(exc, "retry_at", None), datetime)
                    else None
                ),
            )
        attempted_at = checked_at.astimezone(timezone.utc).isoformat()
        return _SteamSaleReceiptLookupOutcome(
            results,
            deep_due_ids,
            attempted_at if deep_due_ids else None,
            lookup_succeeded,
            coverage_complete,
            lookup_error,
            retry_at,
            pages_scanned,
        )

    def _record_sale_receipt_deep_attempt(
        self,
        note: dict[str, Any],
        *,
        attempted_at: str | None,
    ) -> None:
        if not attempted_at:
            return
        note["saleEvidenceDeepLastAttemptAt"] = attempted_at
        note["saleEvidenceDeepAttemptCount"] = max(
            0,
            safe_int(note.get("saleEvidenceDeepAttemptCount")) or 0,
        ) + 1

    def _load_existing_rebuy_source_keys(self) -> tuple[set[str], set[str]]:
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
        return existing_rebuy_sources, existing_rebuy_sell_ops

    def _mark_steam_listing_sold(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        sale_receipt: dict[str, Any] | None,
        existing_rebuy_sources: set[str],
        existing_rebuy_sell_ops: set[str],
    ) -> None:
        listing_id = str(note.get("listingId") or "").strip()
        asset_id = str(op["asset_id"] or "").strip()
        note_changed = False
        if not listing_id and sale_receipt:
            listing_id = str(sale_receipt.get("listingId") or "").strip()
            if listing_id:
                note["listingId"] = listing_id
                note_changed = True
        self.db.update_pool_operation(op["id"], status="sold")
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
        if asset_id:
            self.db.set_asset_status(asset_id, "sold")

        steam_list_price = note.get("steamListPrice")
        steam_net_price = safe_float(note.get("steamSellerNetPrice"))
        receipt_net_price = safe_float(sale_receipt.get("receivedAmount")) if sale_receipt else None
        if sale_receipt:
            receipt_purchase_id = sale_receipt.get("purchaseId")
            receipt_time_sold = sale_receipt.get("timeSold")
            receipt_currency_id = sale_receipt.get("receivedCurrencyId")
            if receipt_purchase_id not in (None, ""):
                note["steamPurchaseId"] = receipt_purchase_id
                note_changed = True
            if receipt_time_sold not in (None, ""):
                note["steamSoldAt"] = _normalize_timestamp_iso(receipt_time_sold)
                note_changed = True
            if receipt_currency_id not in (None, ""):
                note["steamHistoryCurrencyId"] = receipt_currency_id
                note_changed = True
        if receipt_net_price is not None and receipt_net_price > 0:
            steam_net_price = receipt_net_price
            note["steamSellerNetPrice"] = steam_net_price
            note["steamSellerNetPriceSource"] = "steam_history"
            note_changed = True
        elif steam_net_price is None:
            steam_net_price = _steam_seller_net_from_gross(
                steam_list_price,
                self.config.steam_net_factor,
            )
            if steam_net_price is not None:
                note["steamSellerNetPrice"] = steam_net_price
                note["steamSellerNetPriceSource"] = "steam_net_factor"
                note_changed = True
        if note_changed:
            self.db.update_pool_operation(op["id"], note=_build_note(note))
        sold_message = (
            f"[卖出] {op['market_hash_name']} | "
            f"账号={_steam_account_log_label(note) or '-'} | "
            f"asset={asset_id} | "
            f"Steam售价 CNY {_format_decimal(steam_list_price)}"
        )
        if steam_net_price is not None:
            sold_message += f" | 税后到手 CNY {_format_decimal(steam_net_price)}"
        print(sold_message)
        self._emit_guadao_local_event(
            operation="steam_listing_sold",
            message="Steam 挂刀已确认卖出",
            market_hash_name=str(op["market_hash_name"]),
            operation_id=int(op["id"]),
            asset_id=asset_id or None,
            note=note,
            context={
                "state": "sold",
                "listingId": listing_id or None,
                "steamSalePrice": steam_list_price,
                "steamNetAmount": steam_net_price,
                "steamNetAmountSource": note.get("steamSellerNetPriceSource"),
                "steamSoldAt": note.get("steamSoldAt"),
                "steamPurchaseId": note.get("steamPurchaseId"),
            },
        )

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
                            "steamSellerNetPrice": steam_net_price,
                            "steamAccountId": note.get("steamAccountId"),
                            "steamAccountName": note.get("steamAccountName"),
                            "steamId64": note.get("steamId64"),
                            "listingRatioAtOpen": note.get("listingRatioAtOpen"),
                            "maxRebuyRatioAtOpen": note.get("maxRebuyRatioAtOpen"),
                            "guadaoMaxListingRatioAtOpen": note.get("guadaoMaxListingRatioAtOpen"),
                            "steamNetFactorAtOpen": note.get("steamNetFactorAtOpen"),
                            "guadaoRatioRuleSource": note.get("guadaoRatioRuleSource"),
                            "guadaoRatioRuleId": note.get("guadaoRatioRuleId"),
                            "guadaoRatioRuleVersion": note.get("guadaoRatioRuleVersion"),
                        }
                    ),
                )
                if listing_id:
                    existing_rebuy_sources.add(listing_id)
                existing_rebuy_sell_ops.add(source_sell_op_id)

    def _refresh_listings(
        self,
        *,
        client: SteamMarketClient | None = None,
        active_listings: list[Any] | None = None,
        operation_ids: set[int] | None = None,
        sale_receipt_results: dict[int, dict[str, Any] | None] | None = None,
        sale_receipt_deep_attempt_ids: set[int] | None = None,
        sale_receipt_deep_attempted_at: str | None = None,
        sale_receipt_lookup_succeeded: bool | None = None,
        sale_receipt_coverage_complete: bool | None = None,
    ) -> int:
        active_client = client or self.steam_client
        if not active_client:
            return 0
        if active_listings is None:
            try:
                active = active_client.list_active_listings()
            except Exception as exc:
                print(
                    "[警告] 获取 Steam 挂单列表失败，暂按网络/Steam 超时处理，"
                    f"不会判定为已卖出: {exc}"
                )
                return 0
        else:
            active = active_listings
        active_listing_ids, active_asset_ids = self._active_listing_identity_sets(active)
        now = _now_utc()
        sold_count = 0
        pool_status_map = self.db.get_pool_status_map()
        existing_rebuy_sources, existing_rebuy_sell_ops = self._load_existing_rebuy_source_keys()
        stale_market_snapshot_cache: dict[str, tuple[float | None, float | None, str | None]] = {}

        sale_evidence_ops = []
        for op in self.db.list_pool_operations_by_type_and_statuses(
            OP_SELL_STEAM,
            statuses=["listed", POOL_STATUS_LISTING_PENDING, "manual_required"],
            limit=5000,
        ):
            note = _read_note(op["note"])
            raw_status = str(op["status"] or "")
            is_listing_missing_unverified = (
                raw_status == POOL_STATUS_LISTING_PENDING
                and str(note.get("confirmationStatus") or "") == "listing_missing_unverified"
            )
            is_stale_manual_recheck = (
                raw_status == "manual_required"
                and str(note.get("staleListedCleanupStatus") or "") == "manual_required"
            )
            if raw_status != "listed" and not (
                is_listing_missing_unverified or is_stale_manual_recheck
            ):
                continue
            if not self._operation_matches_client(op, active_client):
                continue
            if operation_ids is not None and int(op["id"]) not in operation_ids:
                continue
            sale_evidence_ops.append(op)
        if sale_receipt_results is None:
            receipt_lookup = self._lookup_steam_sale_receipts_for_operations(
                active_client,
                sale_evidence_ops,
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
                now=now,
            )
            sale_receipt_results = receipt_lookup.receipts
            sale_receipt_deep_attempt_ids = receipt_lookup.deep_attempt_ids
            sale_receipt_deep_attempted_at = receipt_lookup.deep_attempted_at
            sale_receipt_lookup_succeeded = receipt_lookup.lookup_succeeded
            sale_receipt_coverage_complete = receipt_lookup.coverage_complete
        deep_attempt_ids = set(sale_receipt_deep_attempt_ids or set())
        sale_receipt_results = dict(sale_receipt_results or {})
        inventory_return_checks = self._prepare_official_inventory_return_checks(
            active_client,
            sale_evidence_ops,
            active_listing_ids=active_listing_ids,
            active_asset_ids=active_asset_ids,
            sale_receipt_results=sale_receipt_results,
            sale_receipt_lookup_succeeded=bool(sale_receipt_lookup_succeeded),
            sale_receipt_coverage_complete=bool(sale_receipt_coverage_complete),
            deep_attempt_ids=deep_attempt_ids,
        )
        for op in sale_evidence_ops:
            note = _read_note(op["note"])
            raw_status = str(op["status"] or "")
            is_listing_missing_unverified = (
                raw_status == POOL_STATUS_LISTING_PENDING
                and str(note.get("confirmationStatus") or "") == "listing_missing_unverified"
            )
            pool_status = pool_status_map.get(op["market_hash_name"], POOL_STATUS_HOLDING)
            if pool_status == POOL_STATUS_LISTING_PENDING and not is_listing_missing_unverified:
                continue
            operation_id = int(op["id"])
            if operation_id in deep_attempt_ids:
                self._record_sale_receipt_deep_attempt(
                    note,
                    attempted_at=sale_receipt_deep_attempted_at,
                )
            listing_id = str(note.get("listingId") or "")
            asset_id = str(op["asset_id"] or "")
            is_stale_listed = self._is_stale_guadao_listed_operation(op, now=now)

            if self._listing_is_active(
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
                listing_id=listing_id,
                asset_id=asset_id,
            ):
                if is_listing_missing_unverified:
                    note["confirmationStatus"] = "listing_active_reverified"
                    note["confirmationRecoveredAt"] = utc_now_iso()
                    self._mark_steam_listing_active(op, note)
                    continue
                if is_stale_listed:
                    keep_decision = self._keep_stale_active_listing_if_still_competitive(
                        op,
                        note,
                        client=active_client,
                        now=now,
                        market_snapshot_cache=stale_market_snapshot_cache,
                    )
                    if keep_decision is True or keep_decision is None:
                        continue
                    self._remove_stale_active_guadao_listing(
                        op,
                        note,
                        client=active_client,
                        active=active,
                        active_listing_ids=active_listing_ids,
                    )
                    continue
                if not note.get("activeVerifiedAt"):
                    note["activeVerifiedAt"] = utc_now_iso()
                    self.db.update_pool_operation(op["id"], note=_build_note(note))
                continue

            if is_listing_missing_unverified:
                self._record_listing_missing_observation(note)
            sale_receipt = sale_receipt_results.get(operation_id)
            if sale_receipt is not None:
                self._mark_steam_listing_sold(
                    op,
                    note,
                    sale_receipt=sale_receipt,
                    existing_rebuy_sources=existing_rebuy_sources,
                    existing_rebuy_sell_ops=existing_rebuy_sell_ops,
                )
                sold_count += 1
                continue
            inventory_check = inventory_return_checks.get(operation_id)
            if inventory_check is not None:
                check_status = str(inventory_check.get("status") or "")
                if check_status == "found_same_asset":
                    self._release_listing_missing_asset_returned(
                        op,
                        note,
                        inventory_check=inventory_check,
                    )
                    continue
                if inventory_check.get("checkedAt"):
                    note["steamInventoryReturnCheckAt"] = inventory_check["checkedAt"]
                    note["steamInventoryReturnCheckStatus"] = check_status
                    note["steamInventoryReturnCheckAssetId"] = asset_id
                    note["steamInventoryReturnPagesScanned"] = inventory_check.get("pagesScanned")
                    note["steamInventoryReturnCoverageComplete"] = inventory_check.get(
                        "coverageComplete"
                    )
                    note["steamSaleEvidenceHistoryCoverageComplete"] = inventory_check.get(
                        "historyCoverageComplete"
                    )
                    if inventory_check.get("message"):
                        note["steamInventoryReturnCheckMessage"] = str(
                            inventory_check["message"]
                        )
                elif check_status:
                    # This records why the inventory call was deliberately
                    # skipped without pretending that Steam inventory itself
                    # was successfully queried.
                    note["steamInventoryReturnPrecondition"] = check_status
                    if inventory_check.get("message"):
                        note["steamInventoryReturnPreconditionMessage"] = str(
                            inventory_check["message"]
                        )
            if is_stale_listed:
                self._mark_stale_listed_manual_required(
                    op,
                    note,
                    reason="stale listed operation missing active Steam listing and sale receipt",
                )
                continue

            created_at = _parse_iso(op["created_at"])
            if created_at and (now - created_at).total_seconds() < self._minimum_action_confirmation_seconds():
                continue

            # A listing disappearing from mylistings is not sale evidence.
            # History may lag, mylistings may be incomplete, or a manual
            # removal may have occurred.  Keep the asset locked in a resumable
            # confirmation state until Steam supplies an official receipt (or
            # the existing safe pending-state reconciliation proves it can be
            # released).  Never create a rebuy from absence alone.
            self._mark_steam_listing_pending(op, note, reason="listing_missing_unverified")
            print(
                f"[挂单待确认] {op['market_hash_name']} | asset={asset_id or '-'} | "
                f"listing={listing_id or '-'} | Steam活跃挂单未找到且无官方卖出回执，未判定为卖出"
            )
            continue
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
            if created_at and (now - created_at).total_seconds() < self._minimum_action_confirmation_seconds():
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

    def _resolve_trade_url_for_account(
        self,
        *,
        account: Account | None,
        steam_id64: str | None,
        client: SteamMarketClient | None,
    ) -> str | None:
        expected_steam_id = str(steam_id64 or (account.steam_id64 if account else "") or "").strip() or None
        if account and account.trade_url:
            if self._is_trade_url_for_steam_id(account.trade_url, expected_steam_id):
                return account.trade_url
            actual = _steam_id64_from_trade_url(account.trade_url) or "unknown"
            print(
                "[警告] 已忽略不匹配的补仓交易链接："
                f"卖出流水 steam={expected_steam_id or 'unknown'}，tradeUrl 指向 steam={actual}"
            )

        if account is None and client is self.steam_client:
            return self._resolve_trade_url()
        if client is None:
            return None
        try:
            url = client.get_trade_url()
            if not self._is_trade_url_for_steam_id(url, expected_steam_id):
                actual = _steam_id64_from_trade_url(url) or "unknown"
                print(
                    "[警告] 自动获取到的交易链接与卖出流水账号不一致，"
                    f"卖出流水 steam={expected_steam_id or 'unknown'}，tradeUrl 指向 steam={actual}"
                )
                return None
            if account and getattr(self, "account_store", None):
                self.account_store.update_account(account.id, trade_url=url)
                account.trade_url = url
            return url
        except Exception as exc:
            print(f"[警告] 自动获取交易链接失败: {exc}")
            return None

    def _resolve_rebuy_trade_url(self, note: dict[str, Any]) -> str | None:
        account = self._account_by_id(note.get("steamAccountId"))
        steam_id64 = str(note.get("steamId64") or "").strip() or None
        if account is None and steam_id64:
            account = self._account_by_steam_id64(steam_id64)

        if account is None:
            return self._resolve_trade_url()

        expected_steam_id = str(steam_id64 or account.steam_id64 or "").strip() or None
        if account.trade_url and self._is_trade_url_for_steam_id(
            account.trade_url,
            expected_steam_id,
        ):
            # A persisted, locally attributable trade URL is sufficient for
            # C5 delivery.  Constructing a Steam client here used to trigger
            # one MyListings validation per receiving account before every
            # category batch, even though no Steam evidence was needed.
            return account.trade_url

        client = self._steam_client_for_account(account, steam_id64)
        return self._resolve_trade_url_for_account(
            account=account,
            steam_id64=steam_id64,
            client=client,
        )

    def _steam_client_for_rebuy_note(self, note: dict[str, Any]) -> SteamMarketClient | None:
        account = self._account_by_id(note.get("steamAccountId"))
        steam_id64 = str(note.get("steamId64") or "").strip() or None
        if account is None and steam_id64:
            account = self._account_by_steam_id64(steam_id64)
        if account is None:
            return self.steam_client
        return self._steam_client_for_account(account, steam_id64)

    def _infer_rebuy_account_fields(self, note: dict[str, Any]) -> dict[str, Any]:
        if note.get("steamAccountId") or note.get("steamAccountName") or note.get("steamId64"):
            return {}
        source_sell_op_id = safe_int(note.get("sourceSellOperationId"))
        if not source_sell_op_id:
            return {}
        source_op = self._get_pool_operation_by_id(source_sell_op_id)
        if source_op is None:
            return {}
        source_note = _read_note(source_op["note"])
        if source_note.get("steamAccountId") or source_note.get("steamAccountName") or source_note.get("steamId64"):
            return {
                "steamAccountId": source_note.get("steamAccountId"),
                "steamAccountName": source_note.get("steamAccountName"),
                "steamId64": source_note.get("steamId64"),
            }
        asset_id = str(source_op["asset_id"] or "").strip()
        if not asset_id:
            return {}
        asset = self.db.get_asset(asset_id)
        if asset is None:
            return {}
        steam_id64 = str(asset["steam_id"] or "").strip() or None
        if not steam_id64:
            return {}
        account = self._account_by_steam_id64(steam_id64)
        return self._account_note_fields(account, steam_id64)

    def _get_pool_operation_by_id(self, op_id: int) -> Any | None:
        return self.db.conn.execute("SELECT * FROM pool_operations WHERE id = ?", (op_id,)).fetchone()

    def _has_replacement_child(self, note: dict[str, Any]) -> bool:
        replacement_id = safe_int(note.get("replacementRebuyOperationId"))
        if not replacement_id:
            return False
        return self._get_pool_operation_by_id(replacement_id) is not None

    def _failed_rebuy_counts_as_open(self, op: Any) -> bool:
        note = _read_note(op["note"])
        if self._has_replacement_child(note):
            return False
        return bool(note.get(REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY))

    def _op_is_within_rebuy_audit_window(self, op: Any, cutoff: datetime) -> bool:
        op_time = _parse_iso(op["completed_at"]) or _parse_iso(op["created_at"])
        if op_time is None:
            return False
        if op_time.tzinfo is None:
            op_time = op_time.replace(tzinfo=timezone.utc)
        return op_time >= cutoff

    def _persist_c5_submission_unconfirmed(
        self,
        op: Any,
        note: dict[str, Any],
        result: Any,
    ) -> None:
        payload = getattr(result, "payload", None)
        submitted_at = (
            _parse_iso(str(getattr(result, "submitted_at", None) or ""))
            or _now_utc()
        )
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        submitted_at = submitted_at.astimezone(timezone.utc)
        asset_order_id, trade_order_id, pay_status = _c5_submission_credentials(payload)
        uncertain_note = {
            **note,
            "c5OutTradeNo": getattr(result, "out_trade_no", None),
            "c5OrderId": asset_order_id,
            "c5TradeOrderId": trade_order_id,
            "c5PayStatus": pay_status,
            "c5OrderStatus": C5_SUBMISSION_UNCONFIRMED,
            C5_DELIVERY_STATUS_KEY: C5_SUBMISSION_UNCONFIRMED,
            "c5OrderSubmittedAt": submitted_at.isoformat(),
            "c5OrderPayload": payload,
            "c5SubmissionUnconfirmedAt": utc_now_iso(),
            "c5SubmissionUnconfirmedReason": str(getattr(result, "reason", "") or "unknown"),
            "c5SubmissionReconcileAbsenceCount": 0,
        }
        # A delivery deadline is meaningful only after both C5 order ids are
        # proven.  Remove a stale deadline when migrating an old bad state.
        uncertain_note.pop("c5DeliveryDeadlineAt", None)
        self.db.update_pool_operation(
            op["id"],
            status=C5_SUBMISSION_UNCONFIRMED,
            actual_price=getattr(result, "actual_price", None),
            note=_build_note(uncertain_note),
        )
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
        self._emit_guadao_local_event(
            operation="c5_rebuy_submission_unconfirmed",
            message="C5 补仓提交结果待核对，确认远端终态前不会重复购买",
            level="WARNING",
            market_hash_name=str(op["market_hash_name"]),
            operation_id=int(op["id"]),
            asset_id=str(op["asset_id"] or "") or None,
            note=uncertain_note,
            context={
                "state": C5_SUBMISSION_UNCONFIRMED,
                "c5OutTradeNo": uncertain_note.get("c5OutTradeNo"),
                "c5OrderId": asset_order_id,
                "c5TradeOrderId": trade_order_id,
                "payStatus": pay_status,
                "reason": uncertain_note.get("c5SubmissionUnconfirmedReason"),
            },
        )

    def _read_c5_buyer_order_rows(
        self,
        *,
        submitted_at: datetime | None,
        page_budget: int,
    ) -> tuple[list[dict[str, Any]], bool, int, str | None]:
        rows: list[dict[str, Any]] = []
        coverage_complete = False
        seen_row_keys: set[tuple[str, ...]] = set()
        previous_page_keys: tuple[tuple[str, ...], ...] | None = None
        last_row_time: datetime | None = None
        time_order_is_monotonic = True
        page_num = 1
        pages_read = 0
        stop_reason: str | None = None
        if submitted_at is not None:
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
            submitted_at = submitted_at.astimezone(timezone.utc)
        coverage_boundary = (
            submitted_at - timedelta(seconds=C5_SUBMISSION_MATCH_WINDOW_SECONDS)
            if submitted_at is not None
            else None
        )

        # There is intentionally no fixed business page limit here.  Stop only
        # when C5 declares the last page, the fetched order times cover the
        # local submission window, or the API stops making progress.  An
        # abnormal/repeated page ends this attempt as incomplete, so absence is
        # never inferred from a truncated prefix.
        while True:
            if pages_read >= max(1, int(page_budget)):
                stop_reason = "single_run_page_budget_exhausted"
                break
            payload = self.c5_client.buyer_order_status(
                page_num=page_num,
                page_size=100,
                status=None,
            )
            pages_read += 1
            if not _c5_buyer_status_has_list(payload):
                raise RuntimeError("C5 buyer order response has no order list")
            page_rows = _c5_buyer_status_rows(payload)
            pages = _c5_buyer_status_pages(payload)

            page_keys: list[tuple[str, ...]] = []
            page_times: list[datetime] = []
            new_rows = 0
            for row in page_rows:
                asset_order_id = _c5_buyer_row_asset_order_id(row) or ""
                trade_order_id = _c5_buyer_row_trade_order_id(row) or ""
                out_trade_no = str(
                    row.get("outTradeNo") or row.get("out_trade_no") or ""
                ).strip()
                created_text = str(
                    _normalize_timestamp_iso(row.get("createTime") or row.get("createdAt"))
                    or ""
                )
                row_key = (
                    asset_order_id,
                    trade_order_id,
                    out_trade_no,
                    created_text,
                    _c5_order_detail_market_hash_name(row),
                    str(row.get("receiveSteamId") or row.get("steamId") or ""),
                    str(row.get("actualPay") or row.get("price") or ""),
                )
                page_keys.append(row_key)
                if row_key not in seen_row_keys:
                    seen_row_keys.add(row_key)
                    rows.append(row)
                    new_rows += 1
                parsed_time = _parse_iso(created_text)
                if parsed_time is not None:
                    if parsed_time.tzinfo is None:
                        parsed_time = parsed_time.replace(tzinfo=timezone.utc)
                    parsed_time = parsed_time.astimezone(timezone.utc)
                    if last_row_time is not None and parsed_time > last_row_time:
                        time_order_is_monotonic = False
                    last_row_time = parsed_time
                    page_times.append(parsed_time)

            if pages is not None and page_num >= pages:
                coverage_complete = True
                stop_reason = "api_last_page"
                break
            if not page_rows:
                # Empty before an API-declared later page is an abnormal gap.
                # Without page metadata an empty list is the endpoint's only
                # explicit end-of-list signal.
                coverage_complete = pages is None
                stop_reason = "api_empty_last_page" if pages is None else "unexpected_empty_page"
                break
            if (
                coverage_boundary is not None
                and time_order_is_monotonic
                and page_times
                and min(page_times) <= coverage_boundary
            ):
                coverage_complete = True
                stop_reason = "submitted_time_window_covered"
                break

            normalized_page_keys = tuple(page_keys)
            if new_rows == 0 or normalized_page_keys == previous_page_keys:
                coverage_complete = False
                stop_reason = "buyer_order_page_no_progress"
                break
            previous_page_keys = normalized_page_keys
            page_num += 1
        return rows, coverage_complete, pages_read, stop_reason

    def _c5_submission_candidate_rows(
        self,
        op: Any,
        note: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        (
            claimed_order_ids,
            claimed_out_trade_nos,
            ambiguous_order_ids,
            ambiguous_out_trade_nos,
            claim_evidence_available,
            pending_sweeper_submissions,
        ) = self._claimed_c5_order_evidence(
            exclude_operation_id=int(op["id"])
        )
        expected_out_trade_no = str(note.get("c5OutTradeNo") or "").strip()
        submitted_at = _parse_iso(str(note.get("c5OrderSubmittedAt") or ""))
        if submitted_at is not None and submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        expected_price = safe_float(op["actual_price"]) or safe_float(op["expected_price"])
        expected_steam_id = str(note.get("steamId64") or "").strip()
        exact_candidates: list[dict[str, Any]] = []
        fuzzy_candidates: list[dict[str, Any]] = []
        for row in rows:
            row_out_trade_no = str(row.get("outTradeNo") or row.get("out_trade_no") or "").strip()
            exact_out_trade_match = bool(
                expected_out_trade_no and row_out_trade_no == expected_out_trade_no
            )
            fuzzy_match = False
            if exact_out_trade_match:
                fuzzy_match = True
            elif not (expected_out_trade_no and row_out_trade_no):
                row_name = _c5_order_detail_market_hash_name(row)
                row_price = safe_float(row.get("actualPay")) or safe_float(row.get("price"))
                row_steam_id = str(
                    row.get("receiveSteamId") or row.get("steamId") or row.get("steamId64") or ""
                ).strip()
                row_created_at = _parse_iso(
                    str(_normalize_timestamp_iso(row.get("createTime") or row.get("createdAt")) or "")
                )
                if row_created_at is not None and row_created_at.tzinfo is None:
                    row_created_at = row_created_at.replace(tzinfo=timezone.utc)
                # Fuzzy ownership is allowed only with a complete evidence
                # tuple.  Missing either timestamp or Steam account used to
                # broaden the match and could steal an unrelated C5 sweeper
                # order whose buyer/status row omitted outTradeNo.
                time_matches = bool(
                    submitted_at is not None
                    and row_created_at is not None
                    and abs((row_created_at - submitted_at).total_seconds())
                    <= C5_SUBMISSION_MATCH_WINDOW_SECONDS
                )
                fuzzy_match = bool(
                    row_name == str(op["market_hash_name"])
                    and expected_price is not None
                    and row_price is not None
                    and abs(row_price - expected_price) <= 0.02
                    and bool(expected_steam_id)
                    and bool(row_steam_id)
                    and row_steam_id == expected_steam_id
                    and time_matches
                )
            if not fuzzy_match:
                continue

            if not exact_out_trade_match:
                for pending_submission in pending_sweeper_submissions:
                    pending_at = _parse_iso(
                        str(pending_submission.get("submittedAt") or "")
                    )
                    if pending_at is not None and pending_at.tzinfo is None:
                        pending_at = pending_at.replace(tzinfo=timezone.utc)
                    pending_price = safe_float(pending_submission.get("buyPrice"))
                    if (
                        pending_submission.get("marketHashName") == row_name
                        and str(pending_submission.get("receivingSteamId") or "").strip()
                        == row_steam_id
                        and pending_at is not None
                        and row_created_at is not None
                        and abs((row_created_at - pending_at).total_seconds())
                        <= C5_SUBMISSION_MATCH_WINDOW_SECONDS
                        and pending_price is not None
                        and row_price is not None
                        and abs(pending_price - row_price) <= 0.02
                    ):
                        return (
                            [],
                            "remote_order_overlaps_unconfirmed_c5_sweeper_submission",
                            claim_evidence_available,
                        )

            row_order_ids = {
                value
                for value in (
                    _c5_buyer_row_asset_order_id(row),
                    _c5_buyer_row_trade_order_id(row),
                )
                if value
            }
            if row_order_ids.intersection(ambiguous_order_ids) or (
                row_out_trade_no and row_out_trade_no in ambiguous_out_trade_nos
            ):
                return [], "remote_order_claimed_by_incomplete_local_evidence", claim_evidence_available
            if exact_out_trade_match and (
                row_order_ids.intersection(claimed_order_ids)
                or row_out_trade_no in claimed_out_trade_nos
            ):
                return [], "exact_out_trade_no_claimed_by_other_operation", claim_evidence_available
            if row_order_ids.intersection(claimed_order_ids):
                continue
            if row_out_trade_no and row_out_trade_no in claimed_out_trade_nos:
                continue
            if exact_out_trade_match:
                exact_candidates.append(row)
            else:
                fuzzy_candidates.append(row)
        return (exact_candidates or fuzzy_candidates), None, claim_evidence_available

    def _claimed_c5_order_evidence(
        self,
        *,
        exclude_operation_id: int,
    ) -> tuple[set[str], set[str], set[str], set[str], bool, list[dict[str, Any]]]:
        order_ids: set[str] = set()
        out_trade_nos: set[str] = set()
        ambiguous_order_ids: set[str] = set()
        ambiguous_out_trade_nos: set[str] = set()
        for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=50_000):
            if int(row["id"]) == int(exclude_operation_id):
                continue
            other_note = _read_note(row["note"])
            evidence_complete = _has_confirmed_c5_order_note(other_note)
            target_order_ids = order_ids if evidence_complete else ambiguous_order_ids
            target_out_trade_nos = out_trade_nos if evidence_complete else ambiguous_out_trade_nos
            for value in (
                other_note.get("c5OrderId"),
                other_note.get("c5TradeOrderId"),
                _extract_c5_order_id(other_note.get("c5OrderPayload")),
                _extract_c5_trade_order_id(other_note.get("c5OrderPayload")),
            ):
                if value not in (None, ""):
                    target_order_ids.add(str(value))
            out_trade_no = str(other_note.get("c5OutTradeNo") or "").strip()
            if out_trade_no:
                target_out_trade_nos.add(out_trade_no)
        (
            sweeper_order_ids,
            sweeper_out_trade_nos,
            sweeper_ambiguous_order_ids,
            sweeper_ambiguous_out_trade_nos,
            sweeper_evidence_available,
            pending_sweeper_submissions,
        ) = self._c5_sweeper_claimed_order_evidence()
        order_ids.update(sweeper_order_ids)
        out_trade_nos.update(sweeper_out_trade_nos)
        ambiguous_order_ids.update(sweeper_ambiguous_order_ids)
        ambiguous_out_trade_nos.update(sweeper_ambiguous_out_trade_nos)
        return (
            order_ids,
            out_trade_nos,
            ambiguous_order_ids,
            ambiguous_out_trade_nos,
            sweeper_evidence_available,
            pending_sweeper_submissions,
        )

    def _c5_sweeper_claimed_order_evidence(
        self,
    ) -> tuple[set[str], set[str], set[str], set[str], bool, list[dict[str, Any]]]:
        state_path = PROJECT_ROOT / "data" / "c5_case_sweeper_v2_state.json"
        if not state_path.exists():
            return set(), set(), set(), set(), True, []
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return set(), set(), set(), set(), False, []
        if not isinstance(state, dict):
            return set(), set(), set(), set(), False, []

        order_ids: set[str] = set()
        out_trade_nos: set[str] = set()
        ambiguous_order_ids: set[str] = set()
        ambiguous_out_trade_nos: set[str] = set()
        pending_submissions: list[dict[str, Any]] = []
        rounds = state.get("rounds")
        if not isinstance(rounds, list):
            return set(), set(), set(), set(), False, []
        for round_row in rounds:
            if not isinstance(round_row, dict):
                return set(), set(), set(), set(), False, []
            orders = round_row.get("orders")
            if orders is not None and not isinstance(orders, list):
                return set(), set(), set(), set(), False, []
            if isinstance(orders, list):
                for order in orders:
                    if not isinstance(order, dict):
                        return set(), set(), set(), set(), False, []
                    asset_order_id = str(order.get("orderAssetId") or "").strip()
                    trade_order_id = str(order.get("tradeOrderId") or "").strip()
                    out_trade_no = str(order.get("outTradeNo") or "").strip()
                    evidence_complete = bool(asset_order_id and trade_order_id)
                    target_ids = order_ids if evidence_complete else ambiguous_order_ids
                    target_out = out_trade_nos if evidence_complete else ambiguous_out_trade_nos
                    if asset_order_id:
                        target_ids.add(asset_order_id)
                    if trade_order_id:
                        target_ids.add(trade_order_id)
                    if out_trade_no:
                        target_out.add(out_trade_no)
            submissions = round_row.get("submissions")
            if submissions is not None and not isinstance(submissions, list):
                return set(), set(), set(), set(), False, []
            if not isinstance(submissions, list):
                continue
            for submission in submissions:
                if not isinstance(submission, dict):
                    return set(), set(), set(), set(), False, []
                submission_out_trade_no = str(submission.get("outTradeNo") or "").strip()
                if submission_out_trade_no:
                    ambiguous_out_trade_nos.add(submission_out_trade_no)
                products = submission.get("products")
                if products is not None and not isinstance(products, list):
                    return set(), set(), set(), set(), False, []
                if not isinstance(products, list):
                    continue
                for product in products:
                    if not isinstance(product, dict):
                        return set(), set(), set(), set(), False, []
                    out_trade_no = str(product.get("outTradeNo") or "").strip()
                    if out_trade_no:
                        ambiguous_out_trade_nos.add(out_trade_no)
                    if submission.get("status") in {"submitting", "uncertain"}:
                        pending_submissions.append(
                            {
                                "marketHashName": str(
                                    submission.get("marketHashName")
                                    or round_row.get("marketHashName")
                                    or ""
                                ).strip(),
                                "receivingSteamId": str(
                                    submission.get("receivingSteamId")
                                    or round_row.get("receivingSteamId")
                                    or ""
                                ).strip(),
                                "submittedAt": (
                                    submission.get("submittedAt")
                                    or submission.get("createdAt")
                                    or round_row.get("createdAt")
                                ),
                                "buyPrice": safe_float(product.get("buyPrice")),
                            }
                        )
        return (
            order_ids,
            out_trade_nos,
            ambiguous_order_ids,
            ambiguous_out_trade_nos,
            True,
            pending_submissions,
        )

    def _c5_submission_window_is_covered(
        self,
        note: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        pagination_complete: bool,
    ) -> bool:
        if pagination_complete:
            return True
        submitted_at = _parse_iso(str(note.get("c5OrderSubmittedAt") or ""))
        if submitted_at is None:
            return False
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        row_times: list[datetime] = []
        for row in rows:
            parsed = _parse_iso(
                str(_normalize_timestamp_iso(row.get("createTime") or row.get("createdAt")) or "")
            )
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            row_times.append(parsed.astimezone(timezone.utc))
        if not row_times:
            return False
        return min(row_times) <= submitted_at.astimezone(timezone.utc) - timedelta(
            seconds=C5_SUBMISSION_MATCH_WINDOW_SECONDS
        )

    def _mark_c5_submission_manual_required(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        reason: str,
        candidate_count: int,
    ) -> None:
        manual_note = {
            **note,
            "c5SubmissionManualRequiredAt": utc_now_iso(),
            "c5SubmissionManualReason": reason,
            "c5SubmissionCandidateCount": candidate_count,
        }
        self.db.update_pool_operation(
            op["id"],
            status="manual_required",
            note=_build_note(manual_note),
        )
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_REBUY_FAILED)
        self._emit_guadao_local_event(
            operation="c5_rebuy_submission_manual_required",
            message="C5 补仓提交对账证据冲突，已停止自动重复购买",
            level="ERROR",
            market_hash_name=str(op["market_hash_name"]),
            operation_id=int(op["id"]),
            asset_id=str(op["asset_id"] or "") or None,
            note=manual_note,
            context={
                "state": "manual_required",
                "reason": reason,
                "candidateCount": candidate_count,
                "c5OutTradeNo": note.get("c5OutTradeNo"),
            },
        )

    def _reconcile_c5_submission(self, op: Any, note: dict[str, Any]) -> tuple[str, int, int]:
        submitted_at = _parse_iso(str(note.get("c5OrderSubmittedAt") or ""))
        page_budget = int(
            safe_int(note.get("c5SubmissionReconcileNextPageBudget"))
            or C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET
        )
        page_budget = min(
            C5_SUBMISSION_RECONCILE_MAX_PAGE_BUDGET,
            max(C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET, page_budget),
        )
        try:
            (
                remote_rows,
                pagination_complete,
                pages_read,
                pagination_stop_reason,
            ) = self._read_c5_buyer_order_rows(
                submitted_at=submitted_at,
                page_budget=page_budget,
            )
        except Exception as exc:
            updated_note = {
                **note,
                "c5SubmissionLastCheckedAt": utc_now_iso(),
                "c5SubmissionLastCheckError": str(exc),
            }
            self.db.update_pool_operation(op["id"], note=_build_note(updated_note))
            return C5_SUBMISSION_UNCONFIRMED, 0, 0

        note = {
            **note,
            "c5SubmissionReconcilePagesRead": pages_read,
            "c5SubmissionReconcileStopReason": pagination_stop_reason,
        }
        if pagination_complete:
            note.pop("c5SubmissionReconcileNextPageBudget", None)
        else:
            if pagination_stop_reason == "single_run_page_budget_exhausted":
                note["c5SubmissionReconcileNextPageBudget"] = min(
                    C5_SUBMISSION_RECONCILE_MAX_PAGE_BUDGET,
                    max(C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET, page_budget * 2),
                )
                if page_budget >= C5_SUBMISSION_RECONCILE_MAX_PAGE_BUDGET:
                    # This cap is a request-storm fuse, never evidence that the
                    # remote order range was fully checked. Runtime keeps the
                    # operation unconfirmed and moves repeated failures to its
                    # existing slow-retry/one-time-alert path.
                    note["c5SubmissionReconcileSafetyCapReachedAt"] = (
                        note.get("c5SubmissionReconcileSafetyCapReachedAt") or utc_now_iso()
                    )
                    note["c5SubmissionReconcileAlertCode"] = (
                        "max_page_budget_exhausted_without_coverage"
                    )
            else:
                note["c5SubmissionReconcileNextPageBudget"] = page_budget

        candidates, evidence_conflict, claim_evidence_available = self._c5_submission_candidate_rows(
            op, note, remote_rows
        )
        checked_at = utc_now_iso()
        if not claim_evidence_available:
            unavailable_note = {
                **note,
                "c5SubmissionLastCheckedAt": checked_at,
                "c5SubmissionLastCheckError": "c5_sweeper_claim_evidence_unavailable",
            }
            self.db.update_pool_operation(op["id"], note=_build_note(unavailable_note))
            return C5_SUBMISSION_UNCONFIRMED, 0, 1
        if evidence_conflict:
            self._mark_c5_submission_manual_required(
                op,
                note,
                reason=evidence_conflict,
                candidate_count=0,
            )
            return "manual_required", 0, 1
        if len(candidates) > 1:
            self._mark_c5_submission_manual_required(
                op,
                note,
                reason="multiple_matching_c5_orders",
                candidate_count=len(candidates),
            )
            return "manual_required", 0, 1

        if len(candidates) == 1 and not pagination_complete:
            expected_out_trade_no = str(note.get("c5OutTradeNo") or "").strip()
            candidate_out_trade_no = str(
                candidates[0].get("outTradeNo")
                or candidates[0].get("out_trade_no")
                or ""
            ).strip()
            if not (
                expected_out_trade_no
                and candidate_out_trade_no == expected_out_trade_no
            ):
                # A fuzzy match is "unique" only after the fetched order range
                # covers the local submission window. A later page may contain
                # another same-item/same-account/same-price candidate.
                coverage_note = {
                    **note,
                    "c5SubmissionLastCheckedAt": checked_at,
                    "c5SubmissionLastCheckError": "buyer_order_window_not_covered",
                    "c5SubmissionFetchedRows": len(remote_rows),
                    "c5SubmissionPaginationComplete": False,
                    "c5SubmissionCandidateCount": 1,
                }
                self.db.update_pool_operation(op["id"], note=_build_note(coverage_note))
                return C5_SUBMISSION_UNCONFIRMED, 0, 1

        if len(candidates) == 1:
            candidate = candidates[0]
            asset_order_id = _c5_buyer_row_asset_order_id(candidate)
            trade_order_id = _c5_buyer_row_trade_order_id(candidate)
            pay_status = safe_int(candidate.get("payStatus"))
            if not asset_order_id:
                # A row without any usable C5 lookup id cannot be taken into
                # the delivery state machine, even when its outTradeNo matched.
                updated_note = {
                    **note,
                    "c5SubmissionLastCheckedAt": checked_at,
                    "c5SubmissionLastCheckError": "matched_c5_order_missing_lookup_id",
                    "c5SubmissionCandidateCount": 1,
                }
                self.db.update_pool_operation(op["id"], note=_build_note(updated_note))
                return C5_SUBMISSION_UNCONFIRMED, 0, 1

            recognized_submitted_at = submitted_at or _now_utc()
            if recognized_submitted_at.tzinfo is None:
                recognized_submitted_at = recognized_submitted_at.replace(tzinfo=timezone.utc)
            recognized_submitted_at = recognized_submitted_at.astimezone(timezone.utc)
            deadline = recognized_submitted_at + timedelta(seconds=C5_DELIVERY_DEADLINE_SECONDS)
            expected_out_trade_no = str(note.get("c5OutTradeNo") or "").strip()
            candidate_out_trade_no = str(
                candidate.get("outTradeNo") or candidate.get("out_trade_no") or ""
            ).strip()
            recognized_base_note = dict(note)
            recognized_base_note.pop("c5SubmissionReconcileNextPageBudget", None)
            recognized_note = {
                **recognized_base_note,
                "c5OrderId": asset_order_id,
                "c5TradeOrderId": trade_order_id,
                "c5PayStatus": pay_status,
                "c5OrderRecognized": True,
                "c5OrderRecognizedAt": checked_at,
                "c5OrderMatchMode": (
                    "exact_out_trade_no"
                    if expected_out_trade_no
                    and candidate_out_trade_no == expected_out_trade_no
                    else "safe_unique_fuzzy"
                ),
                "c5SubmissionNotCreatedCount": 0,
                "c5OrderStatus": "ordered",
                C5_DELIVERY_STATUS_KEY: "pending",
                "c5OrderReconciledAt": checked_at,
                "c5OrderReconcileSource": "buyer_order_status",
                "c5OrderReconcilePayload": candidate,
                "c5DeliveryDeadlineAt": deadline.isoformat(),
            }
            self.db.update_pool_operation(
                op["id"],
                status="delivery_pending",
                note=_build_note(recognized_note),
            )
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
            state, replacements = self._confirm_recognized_c5_order_detail(int(op["id"]))
            return state, replacements, 1

        if not self._c5_submission_window_is_covered(
            note,
            remote_rows,
            pagination_complete=pagination_complete,
        ):
            coverage_note = {
                **note,
                "c5SubmissionLastCheckedAt": checked_at,
                "c5SubmissionLastCheckError": "buyer_order_window_not_covered",
                "c5SubmissionFetchedRows": len(remote_rows),
                "c5SubmissionPaginationComplete": pagination_complete,
            }
            self.db.update_pool_operation(op["id"], note=_build_note(coverage_note))
            return C5_SUBMISSION_UNCONFIRMED, 0, 1

        absence_count = int(safe_int(note.get("c5SubmissionReconcileAbsenceCount")) or 0) + 1
        if absence_count < C5_SUBMISSION_ABSENCE_CONFIRMATIONS:
            waiting_note = {
                **note,
                "c5SubmissionReconcileAbsenceCount": absence_count,
                "c5SubmissionLastCheckedAt": checked_at,
                "c5SubmissionLastCheckError": None,
            }
            self.db.update_pool_operation(op["id"], note=_build_note(waiting_note))
            return C5_SUBMISSION_UNCONFIRMED, 0, 1

        not_created_count = int(safe_int(note.get("c5SubmissionNotCreatedCount")) or 0) + 1
        failed_note = {
            **note,
            C5_DELIVERY_STATUS_KEY: C5_DELIVERY_FAILED,
            "c5OrderInvalidated": True,
            "c5OrderFailedCode": "submission_not_created",
            "c5OrderFailedDesc": "C5 补仓提交经多次买家订单对账仍不存在，确认未创建订单",
            "c5SubmissionReconcileAbsenceCount": absence_count,
            "c5SubmissionNotCreatedCount": not_created_count,
            "c5SubmissionLastCheckedAt": checked_at,
            REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY: True,
        }
        if not_created_count >= C5_SUBMISSION_NOT_CREATED_MAX_CHAIN:
            manual_note = {
                **failed_note,
                REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY: False,
                "c5SubmissionManualRequiredAt": checked_at,
                "c5SubmissionManualReason": "submission_not_created_chain_limit",
            }
            self.db.update_pool_operation(
                op["id"],
                status="manual_required",
                note=_build_note(manual_note),
            )
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_REBUY_FAILED)
            self._emit_guadao_local_event(
                operation="c5_rebuy_submission_chain_limited",
                message="C5 连续三次确认未创建补仓订单，已停止自动替换",
                level="ERROR",
                market_hash_name=str(op["market_hash_name"]),
                operation_id=int(op["id"]),
                asset_id=str(op["asset_id"] or "") or None,
                note=manual_note,
                context={
                    "state": "manual_required",
                    "reason": "submission_not_created_chain_limit",
                    "notCreatedCount": not_created_count,
                    "c5OutTradeNo": note.get("c5OutTradeNo"),
                },
            )
            return "manual_required", 0, 1
        self.db.update_pool_operation(
            op["id"],
            status=C5_DELIVERY_FAILED,
            note=_build_note(failed_note),
        )
        replacements = self._create_replacement_rebuy_for_failed_op(
            self._get_pool_operation_by_id(int(op["id"])) or op,
            failed_note,
            replacement_reason="c5_submission_not_created",
            failed_status=C5_DELIVERY_FAILED,
            created_by="c5_submission_reconcile",
        )
        return C5_DELIVERY_FAILED, replacements, 1

    def _rebuy_delivery_deadline(self, op: Any, note: dict[str, Any]) -> datetime | None:
        # The 12-hour delivery review clock starts only after a real C5 order was
        # submitted.  Local operation timestamps describe our state machine,
        # not C5's seller-delivery obligation, and must never start this clock.
        submitted = _parse_iso(str(note.get("c5OrderSubmittedAt") or ""))
        if submitted is None:
            return None
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        return submitted.astimezone(timezone.utc) + timedelta(seconds=C5_DELIVERY_DEADLINE_SECONDS)

    def _mark_rebuy_delivery_overdue_if_due(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        deadline = self._rebuy_delivery_deadline(op, note)
        current = now or _now_utc()
        if deadline is None or current < deadline:
            return note
        overdue_note = {
            **note,
            C5_DELIVERY_STATUS_KEY: "pending",
            "c5DeliveryOverdue": True,
            "c5DeliveryDeadlineAt": deadline.isoformat(),
            "c5DeliveryOverdueAt": note.get("c5DeliveryOverdueAt") or current.isoformat(),
            "c5DeliveryOverdueReason": "delivery_detail_requires_authoritative_recheck",
        }
        self.db.update_pool_operation(
            op["id"],
            status="delivery_pending",
            note=_build_note(overdue_note),
        )
        if not note.get("c5DeliveryOverdueAt"):
            self._emit_guadao_local_event(
                operation="c5_rebuy_delivery_overdue_recheck",
                message="C5 补仓已超过 12 小时，继续以订单详情终态为准",
                level="WARNING",
                market_hash_name=str(op["market_hash_name"]),
                operation_id=int(op["id"]),
                asset_id=str(op["asset_id"] or "") or None,
                note=overdue_note,
                context={
                    "state": "delivery_pending",
                    "c5OrderId": overdue_note.get("c5OrderId"),
                    "c5OutTradeNo": overdue_note.get("c5OutTradeNo"),
                    "deliveryDeadlineAt": deadline.isoformat(),
                    "overdueAt": overdue_note.get("c5DeliveryOverdueAt"),
                },
            )
        return overdue_note

    def _create_replacement_rebuy_for_failed_op(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        replacement_reason: str | None = None,
        failed_status: str | None = None,
        force_rebuy_replacement: bool | None = None,
        created_by: str = "rebuy_delivery_audit",
    ) -> int:
        parent_id = int(op["id"])
        conn = self.db.conn
        replacement_id: int | None = None
        effective_note: dict[str, Any] = {}
        market_hash_name = str(op["market_hash_name"])
        asset_id = str(op["asset_id"] or "") or None
        order_id = ""
        failed_reason: Any = "rebuy_failed"
        expected_price = 0.01

        # Replacement creation is a cross-runner idempotency boundary.  A
        # note-only pre-check leaves a race in which two workers both observe
        # no child and insert one.  BEGIN IMMEDIATE serializes the re-read,
        # child search, insert and parent-link update as one SQLite write unit.
        # Do not call Database helpers here: they commit midway through this
        # critical section.
        conn.execute("BEGIN IMMEDIATE")
        try:
            fresh_op = conn.execute(
                "SELECT * FROM pool_operations WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if fresh_op is None:
                conn.rollback()
                return 0

            fresh_note = _read_note(fresh_op["note"])
            # Preserve caller-derived failure evidence that has not yet been
            # persisted (for example canceled audit metadata), while allowing
            # the transactional re-read to win for every current DB field.
            effective_note = {**note, **fresh_note}
            if effective_note.get(C5_DELIVERY_STATUS_KEY) == C5_DELIVERY_SUCCESS:
                conn.rollback()
                return 0

            existing_child_id: int | None = None
            child_rows = conn.execute(
                """
                SELECT id, note FROM pool_operations
                WHERE strategy = ? AND operation_type = ?
                """,
                (STRATEGY_GUADAO, OP_REBUY_C5),
            ).fetchall()
            for child_row in child_rows:
                child_note = _read_note(child_row["note"])
                if safe_int(child_note.get("replacementForRebuyOperationId")) == parent_id:
                    existing_child_id = int(child_row["id"])
                    break

            resolved_replacement_reason = replacement_reason
            if resolved_replacement_reason is None:
                resolved_replacement_reason = (
                    "c5_delivery_failed"
                    if effective_note.get(C5_DELIVERY_STATUS_KEY) == C5_DELIVERY_FAILED
                    else "rebuy_operation_failed"
                )
            resolved_failed_status = failed_status
            if resolved_failed_status is None:
                resolved_failed_status = (
                    C5_DELIVERY_FAILED
                    if resolved_replacement_reason == "c5_delivery_failed"
                    else "failed"
                )
            resolved_force_replacement = bool(force_rebuy_replacement)
            market_hash_name = str(fresh_op["market_hash_name"])
            asset_id = str(fresh_op["asset_id"] or "") or None

            if existing_child_id is not None:
                # Repair a missing parent link left by an older partial write,
                # but never insert another child.
                if safe_int(effective_note.get("replacementRebuyOperationId")) != existing_child_id:
                    repaired_note = {
                        **effective_note,
                        "replacementRebuyOperationId": existing_child_id,
                        "replacementReason": resolved_replacement_reason,
                        REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY: True,
                    }
                    conn.execute(
                        "UPDATE pool_operations SET status = ?, note = ? WHERE id = ?",
                        (resolved_failed_status, _build_note(repaired_note), parent_id),
                    )
                conn.commit()
                return 0

            order_id = str(effective_note.get("c5OrderId") or "").strip()
            failed_reason = (
                effective_note.get("c5OrderFailedDesc")
                or effective_note.get("failedReason")
                or effective_note.get("c5OrderStatusName")
                or "rebuy_failed"
            )
            expected_price = (
                safe_float(fresh_op["actual_price"])
                or safe_float(fresh_op["expected_price"])
                or 0.01
            )
            # This counter tracks consecutive C5 submissions proven not to
            # have created an order.  Delivery failures, cancellations and
            # other replacement causes break that chain.
            not_created_count = (
                int(safe_int(effective_note.get("c5SubmissionNotCreatedCount")) or 0)
                if resolved_replacement_reason == "c5_submission_not_created"
                else 0
            )
            replacement_note = {
                "replacementForRebuyOperationId": parent_id,
                "replacementForC5OrderId": order_id,
                "replacementReason": resolved_replacement_reason,
                "replacementFailedCode": effective_note.get("c5OrderFailedCode"),
                "replacementFailedDesc": failed_reason,
                "forceRebuyReplacement": resolved_force_replacement,
                "sourceSellOperationId": effective_note.get("sourceSellOperationId"),
                "sourceListing": effective_note.get("sourceListing"),
                "steamListPrice": effective_note.get("steamListPrice"),
                "listingRatioAtOpen": effective_note.get("listingRatioAtOpen"),
                "maxRebuyRatioAtOpen": effective_note.get("maxRebuyRatioAtOpen"),
                "guadaoMaxListingRatioAtOpen": effective_note.get("guadaoMaxListingRatioAtOpen"),
                "steamNetFactorAtOpen": effective_note.get("steamNetFactorAtOpen"),
                "guadaoRatioRuleSource": effective_note.get("guadaoRatioRuleSource"),
                "guadaoRatioRuleId": effective_note.get("guadaoRatioRuleId"),
                "guadaoRatioRuleVersion": effective_note.get("guadaoRatioRuleVersion"),
                "steamAccountId": effective_note.get("steamAccountId"),
                "steamAccountName": effective_note.get("steamAccountName"),
                "steamId64": effective_note.get("steamId64"),
                "c5SubmissionNotCreatedCount": not_created_count,
                "createdBy": created_by,
                **(
                    {
                        "replacementMaxPrice": expected_price,
                        "replacementPricePolicy": "original_failed_order_price",
                    }
                    if resolved_replacement_reason
                    in {"c5_delivery_failed", "c5_submission_not_created"}
                    else {}
                ),
            }
            now = utc_now_iso()
            cursor = conn.execute(
                """
                INSERT INTO pool_operations (
                    market_hash_name, strategy, operation_type, status,
                    quantity, expected_price, asset_id, note, created_at
                ) VALUES (?, ?, ?, 'pending', 1, ?, NULL, ?, ?)
                """,
                (
                    market_hash_name,
                    STRATEGY_GUADAO,
                    OP_REBUY_C5,
                    expected_price,
                    _build_note(replacement_note),
                    now,
                ),
            )
            replacement_id = int(cursor.lastrowid)
            parent_note = {
                **effective_note,
                "replacementRebuyOperationId": replacement_id,
                "replacementReason": resolved_replacement_reason,
                REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY: True,
                **(
                    {C5_DELIVERY_STATUS_KEY: C5_DELIVERY_FAILED}
                    if resolved_replacement_reason
                    in {"c5_delivery_failed", "c5_submission_not_created"}
                    else {}
                ),
            }
            completed_at = now if resolved_failed_status in {
                "completed",
                "failed",
                "skipped",
                "dry_run",
                "sold",
            } else fresh_op["completed_at"]
            conn.execute(
                """
                UPDATE pool_operations
                SET status = ?, note = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    resolved_failed_status,
                    _build_note(parent_note),
                    completed_at,
                    parent_id,
                ),
            )
            conn.execute(
                """
                UPDATE inventory_pool
                SET status = ?, updated_at = ?
                WHERE market_hash_name = ?
                """,
                (POOL_STATUS_PENDING_REBUY, now, market_hash_name),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        assert replacement_id is not None
        print(
            f"[补仓失效] {market_hash_name} | 原补仓 op={parent_id} 已失败，"
            f"已创建替换补仓 op={replacement_id} | 原因: {failed_reason}"
        )
        self._emit_guadao_local_event(
            operation="c5_rebuy_replacement_created",
            message="C5 失败补仓已创建替换补仓任务",
            level="WARNING",
            market_hash_name=market_hash_name,
            operation_id=parent_id,
            asset_id=asset_id,
            note=effective_note,
            context={
                "state": "replacement_pending",
                "replacementOperationId": replacement_id,
                "replacementReason": resolved_replacement_reason,
                "failedReason": failed_reason,
                "originalC5OrderId": order_id or None,
                "replacementMaxPrice": (
                    expected_price
                    if resolved_replacement_reason
                    in {"c5_delivery_failed", "c5_submission_not_created"}
                    else None
                ),
                "maxRebuyRatioAtOpen": effective_note.get("maxRebuyRatioAtOpen"),
                "steamNetFactorAtOpen": effective_note.get("steamNetFactorAtOpen"),
            },
        )
        return 1

    def _fetch_c5_buyer_order_detail(self, op: Any, note: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
        lookup_ids = _c5_order_lookup_ids(note)
        if not lookup_ids:
            return None, None, note

        last_error: dict[str, Any] | None = None
        for order_id in lookup_ids:
            try:
                detail = self.c5_client.buyer_order_detail(order_id)
            except C5GameError as exc:
                payload = _parse_c5_error_payload(exc)
                if payload and payload.get("errorCode") == 101200:
                    last_error = payload
                    continue
                raise
            if order_id != str(note.get("c5OrderId") or ""):
                note = {
                    **note,
                    "c5OrderId": order_id,
                    "c5OrderLookupFixedAt": utc_now_iso(),
                }
                self.db.update_pool_operation(op["id"], note=_build_note(note))
            return detail, order_id, note

        updated_note = {
            **note,
            "c5OrderLookupErrorCode": last_error.get("errorCode") if last_error else None,
            "c5OrderLookupErrorMsg": last_error.get("errorMsg") if last_error else "order detail not found",
            "c5OrderCheckedAt": utc_now_iso(),
        }
        self.db.update_pool_operation(op["id"], note=_build_note(updated_note))
        print(
            f"[提示] C5补仓订单暂未查到详情: op={op['id']} | "
            f"已尝试订单号 {', '.join(lookup_ids)}，本轮不按成功/失败处理。"
        )
        return None, None, updated_note

    def _apply_recognized_c5_order_detail(
        self,
        op: Any,
        note: dict[str, Any],
        detail: dict[str, Any],
        order_id: str | None,
    ) -> tuple[str, int]:
        """Apply buyer/detail as the sole delivery terminal-state authority."""

        checked_at = utc_now_iso()
        asset_order_id = _extract_c5_order_id(detail) or note.get("c5OrderId")
        # buyer_order_detail commonly returns the asset-order id in its generic
        # ``orderId`` field.  The quick-buy response is the authoritative
        # source for the parent trade-order id, so never overwrite it with the
        # detail lookup id.  Only backfill from detail when no trade id was
        # previously recorded and the detail exposes both identifier roles.
        trade_order_id = note.get("c5TradeOrderId") or _c5_buyer_row_trade_order_id(detail)
        detail_pay_status = safe_int(detail.get("payStatus"))
        checked_note = {
            **note,
            "c5OrderId": asset_order_id,
            "c5TradeOrderId": trade_order_id,
            "c5PayStatus": (
                detail_pay_status
                if detail_pay_status is not None
                else safe_int(note.get("c5PayStatus"))
            ),
            "c5OrderRecognized": True,
            "c5SubmissionNotCreatedCount": 0,
            "c5OrderStatus": safe_int(detail.get("status")),
            "c5OrderStatusName": detail.get("statusName"),
            "c5OrderCheckedAt": checked_at,
            "c5OrderDetailPayload": detail,
            "c5OrderDetailLastError": None,
        }
        detail_market_hash_name = _c5_order_detail_market_hash_name(detail)
        if detail_market_hash_name and detail_market_hash_name != op["market_hash_name"]:
            checked_note["c5OrderDetailLastError"] = "c5_order_market_hash_name_mismatch"
            checked_note["c5OrderDetailMarketHashName"] = detail_market_hash_name
            checked_note["c5SubmissionManualRequiredAt"] = checked_at
            checked_note["c5SubmissionManualReason"] = "c5_order_market_hash_name_mismatch"
            self.db.update_pool_operation(
                op["id"],
                status="manual_required",
                note=_build_note(checked_note),
            )
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_REBUY_FAILED)
            return "manual_required", 0

        final_status = _c5_delivery_final_status(detail)
        if final_status is None:
            deadline = self._rebuy_delivery_deadline(op, checked_note)
            checked_note["c5DeliveryDeadlineAt"] = deadline.isoformat() if deadline else None
            checked_note[C5_DELIVERY_STATUS_KEY] = "pending"
            checked_note = self._mark_rebuy_delivery_overdue_if_due(op, checked_note)
            self.db.update_pool_operation(
                op["id"],
                status="delivery_pending",
                note=_build_note(checked_note),
            )
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
            return "delivery_pending", 0

        if final_status == C5_DELIVERY_SUCCESS:
            completed_note = {
                **checked_note,
                C5_DELIVERY_STATUS_KEY: C5_DELIVERY_SUCCESS,
                "c5OrderInvalidated": False,
            }
            self.db.update_pool_operation(
                op["id"],
                status="completed",
                note=_build_note(completed_note),
            )
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
            self._emit_guadao_local_event(
                operation="c5_rebuy_completed",
                message="C5 补仓已确认发货完成",
                market_hash_name=str(op["market_hash_name"]),
                operation_id=int(op["id"]),
                asset_id=str(op["asset_id"] or "") or None,
                note=completed_note,
                context={
                    "state": "completed",
                    "c5ActualPrice": safe_float(op["actual_price"]),
                    "c5ExpectedPrice": safe_float(op["expected_price"]),
                    "c5OrderId": order_id or completed_note.get("c5OrderId"),
                    "c5OutTradeNo": completed_note.get("c5OutTradeNo"),
                    "deliveryStatus": C5_DELIVERY_SUCCESS,
                },
            )
            return "completed", 0

        failed_note = {
            **checked_note,
            C5_DELIVERY_STATUS_KEY: C5_DELIVERY_FAILED,
            "c5OrderFailedCode": detail.get("failedCode"),
            "c5OrderFailedDesc": detail.get("failedDesc"),
            "c5OrderInvalidated": True,
            REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY: True,
        }
        self.db.update_pool_operation(
            op["id"],
            status=C5_DELIVERY_FAILED,
            note=_build_note(failed_note),
        )
        self._emit_guadao_local_event(
            operation="c5_rebuy_delivery_failed",
            message="C5 补仓订单已明确发货失败",
            level="ERROR",
            market_hash_name=str(op["market_hash_name"]),
            operation_id=int(op["id"]),
            asset_id=str(op["asset_id"] or "") or None,
            note=failed_note,
            context={
                "state": C5_DELIVERY_FAILED,
                "c5OrderId": order_id or failed_note.get("c5OrderId"),
                "failedCode": failed_note.get("c5OrderFailedCode"),
                "failedReason": failed_note.get("c5OrderFailedDesc"),
            },
        )
        replacements = self._create_replacement_rebuy_for_failed_op(
            self._get_pool_operation_by_id(int(op["id"])) or op,
            failed_note,
            replacement_reason="c5_delivery_failed",
            failed_status=C5_DELIVERY_FAILED,
            created_by="c5_order_detail_terminal",
        )
        return C5_DELIVERY_FAILED, replacements

    def _confirm_recognized_c5_order_detail(self, operation_id: int) -> tuple[str, int]:
        op = self._get_pool_operation_by_id(int(operation_id))
        if op is None:
            return "missing", 0
        note = _read_note(op["note"])
        try:
            detail, order_id, note = self._fetch_c5_buyer_order_detail(op, note)
        except Exception as exc:
            retry_note = {
                **note,
                "c5OrderRecognized": True,
                "c5OrderDetailLastCheckedAt": utc_now_iso(),
                "c5OrderDetailLastError": str(exc),
            }
            self.db.update_pool_operation(
                op["id"],
                status="delivery_pending",
                note=_build_note(retry_note),
            )
            self._mark_rebuy_delivery_overdue_if_due(op, retry_note)
            return "delivery_pending", 0
        if detail is None:
            # Keep every known real identifier. A temporarily unreadable detail
            # response is a retry condition, never proof that the order is
            # absent and never permission to buy again.
            retry_note = {
                **note,
                "c5OrderRecognized": True,
                "c5OrderDetailLastCheckedAt": utc_now_iso(),
                "c5OrderDetailLastError": note.get("c5OrderLookupErrorMsg")
                or "c5_order_detail_unavailable",
            }
            self.db.update_pool_operation(
                op["id"],
                status="delivery_pending",
                note=_build_note(retry_note),
            )
            self._mark_rebuy_delivery_overdue_if_due(op, retry_note)
            return "delivery_pending", 0
        return self._apply_recognized_c5_order_detail(op, note, detail, order_id)

    def _check_recent_rebuy_delivery_failures(self, *, operation_id: int | None = None) -> int:
        if self.config.dry_run or (not self.config.auto_rebuy_enabled and operation_id is None):
            return 0

        cutoff = _now_utc() - timedelta(days=REBUY_ORDER_AUDIT_LOOKBACK_DAYS)
        checked = 0
        successes = 0
        failures = 0
        replacements = 0
        delivery_candidates = self.db.list_pool_operations_by_type_and_statuses(
            OP_REBUY_C5,
            statuses=["delivery_pending", C5_SUBMISSION_UNCONFIRMED, "completed"],
            limit=5000,
        )
        if operation_id is not None:
            delivery_candidates = [
                op for op in delivery_candidates if int(op["id"]) == int(operation_id)
            ]
        for op in delivery_candidates:
            is_delivery_pending = str(op["status"] or "") == "delivery_pending"
            is_submission_unconfirmed = str(op["status"] or "") == C5_SUBMISSION_UNCONFIRMED
            if not is_delivery_pending and not self._op_is_within_rebuy_audit_window(op, cutoff):
                if not is_submission_unconfirmed:
                    continue
            note = _read_note(op["note"])
            if note.get(C5_DELIVERY_STATUS_KEY) in {C5_DELIVERY_SUCCESS, C5_DELIVERY_FAILED}:
                continue
            if is_delivery_pending and not _has_confirmed_c5_order_note(note):
                note = {
                    **note,
                    C5_DELIVERY_STATUS_KEY: C5_SUBMISSION_UNCONFIRMED,
                    "c5OrderStatus": C5_SUBMISSION_UNCONFIRMED,
                    "c5SubmissionUnconfirmedAt": note.get("c5SubmissionUnconfirmedAt") or utc_now_iso(),
                    "c5SubmissionUnconfirmedReason": "legacy_delivery_missing_required_order_evidence",
                    "c5SubmissionReconcileAbsenceCount": int(
                        safe_int(note.get("c5SubmissionReconcileAbsenceCount")) or 0
                    ),
                }
                note.pop("c5DeliveryDeadlineAt", None)
                self.db.update_pool_operation(
                    op["id"],
                    status=C5_SUBMISSION_UNCONFIRMED,
                    note=_build_note(note),
                )
                is_delivery_pending = False
                is_submission_unconfirmed = True
            if is_submission_unconfirmed:
                state, created, reconciled_checked = self._reconcile_c5_submission(op, note)
                replacements += created
                checked += reconciled_checked
                if state == C5_DELIVERY_FAILED:
                    failures += 1
                continue
            if not _c5_order_lookup_ids(note):
                continue

            checked += 1
            try:
                detail, order_id, note = self._fetch_c5_buyer_order_detail(op, note)
            except Exception as exc:
                retry_note = {
                    **note,
                    "c5OrderRecognized": True,
                    "c5OrderDetailLastCheckedAt": utc_now_iso(),
                    "c5OrderDetailLastError": str(exc),
                }
                self.db.update_pool_operation(op["id"], note=_build_note(retry_note))
                print(f"[警告] 复查 C5 补仓订单失败: op={op['id']} | {exc}")
                self._mark_rebuy_delivery_overdue_if_due(op, retry_note)
                continue
            if detail is None:
                self._mark_rebuy_delivery_overdue_if_due(op, note)
                continue

            detail_state, created = self._apply_recognized_c5_order_detail(
                op,
                note,
                detail,
                order_id,
            )
            replacements += created
            if detail_state == "completed":
                successes += 1
                continue
            if detail_state == C5_DELIVERY_FAILED:
                failures += 1
                continue
            if detail_state == "delivery_pending":
                latest = self._get_pool_operation_by_id(int(op["id"]))
                latest_note = _read_note(latest["note"]) if latest is not None else note
                self._mark_rebuy_delivery_overdue_if_due(latest or op, latest_note)

        for failed_status in (C5_DELIVERY_FAILED, "failed", "canceled", "skipped"):
            for op in self.db.list_pool_operations_by_type(OP_REBUY_C5, status=failed_status, limit=5000):
                if operation_id is not None and int(op["id"]) != int(operation_id):
                    continue
                if failed_status not in {"canceled", "skipped"} and not self._op_is_within_rebuy_audit_window(op, cutoff):
                    continue
                note = _read_note(op["note"])
                if note.get(C5_DELIVERY_STATUS_KEY) == C5_DELIVERY_SUCCESS:
                    continue
                if failed_status == "failed" and not note.get(REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY):
                    continue
                if failed_status == "canceled":
                    canceled_reason = (
                        note.get("timeoutReason")
                        or note.get("lastSkipReason")
                        or note.get("failedReason")
                        or "rebuy_canceled"
                    )
                    note = {
                        **note,
                        "failedReason": canceled_reason,
                        "canceledTreatedAsFailedAt": utc_now_iso(),
                        REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY: True,
                    }
                    replacements += self._create_replacement_rebuy_for_failed_op(
                        op,
                        note,
                        replacement_reason="rebuy_canceled",
                        failed_status="failed",
                        force_rebuy_replacement=False,
                        created_by="rebuy_canceled_audit",
                    )
                    continue
                if failed_status == "skipped":
                    if note.get("skipReason") != "c5_balance_insufficient":
                        continue
                    if self._has_replacement_child(note):
                        continue
                    note = {
                        **note,
                        "lastSkipReason": "c5_balance_insufficient",
                        "balanceInsufficientRetriedAt": utc_now_iso(),
                    }
                    self.db.update_pool_operation(op["id"], status="pending", note=_build_note(note))
                    self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
                    continue
                if failed_status == C5_DELIVERY_FAILED and note.get(C5_DELIVERY_STATUS_KEY) != C5_DELIVERY_FAILED:
                    note = {**note, C5_DELIVERY_STATUS_KEY: C5_DELIVERY_FAILED}
                    self.db.update_pool_operation(op["id"], note=_build_note(note))
                replacements += self._create_replacement_rebuy_for_failed_op(op, note)

        if checked or replacements:
            print(
                f"[复查] 最近 {REBUY_ORDER_AUDIT_LOOKBACK_DAYS} 天程序补仓订单 {checked} 条，"
                f"确认成功 {successes} 条，确认失败 {failures} 条，创建替换补仓 {replacements} 条。"
            )
        return replacements

    def _execute_rebuys(self, *, operation_id: int | None = None) -> int:
        if not self.config.auto_rebuy_enabled:
            return 0
        pending = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=200)
        if operation_id is not None:
            pending = [op for op in pending if int(op["id"]) == int(operation_id)]
        rebuy_count = 0
        for op in pending:
            expected_price = op["expected_price"]
            if expected_price is None:
                continue
            note = _read_note(op["note"])
            inferred_account_fields = self._infer_rebuy_account_fields(note)
            if inferred_account_fields:
                note = {**note, **inferred_account_fields}
                self.db.update_pool_operation(op["id"], note=_build_note(note))
            is_replacement = safe_int(note.get("replacementForRebuyOperationId")) is not None
            replacement_max_price = safe_float(note.get("replacementMaxPrice"))
            manual_refrozen_price = safe_float(note.get("manualRebuyRefrozenPrice"))
            manual_steam_net_amount = safe_float(note.get("manualRebuySteamNetAmount"))
            if is_replacement and note.get("forceRebuyReplacement") and replacement_max_price is None:
                replacement_max_price = safe_float(expected_price)
                note = {
                    **note,
                    "forceRebuyReplacement": False,
                    "replacementMaxPrice": replacement_max_price,
                    "replacementPricePolicy": "original_failed_order_price_legacy_migration",
                }
                self.db.update_pool_operation(op["id"], note=_build_note(note))
            expected_steam_list = note.get("steamListPrice")
            rebuy_max_listing_ratio = self._rebuy_max_listing_ratio_for_note(note)
            rebuy_steam_net_factor = safe_float(note.get("steamNetFactorAtOpen")) or self.config.steam_net_factor
            rebuy_steam_client = self._steam_client_for_rebuy_note(note)
            trade_url = self._resolve_rebuy_trade_url(note)
            if not trade_url:
                account_label = note.get("steamAccountName") or note.get("steamId64") or "当前账号"
                print(
                    f"[提示] {op['market_hash_name']} 未能获取 {account_label} 的交易链接，"
                    "将尝试不带 tradeUrl 直接补仓（C5 使用账号预设链接）"
                )
            result = execute_rebuy(
                client=self.c5_client,
                steam_client=rebuy_steam_client,
                market_hash_name=op["market_hash_name"],
                expected_price=float(expected_price),
                expected_steam_list_price=float(expected_steam_list) if expected_steam_list else None,
                app_id=self.settings.app_id,
                tolerance_pct=self.config.price_tolerance_pct,
                dry_run=self.config.dry_run,
                steam_net_factor=rebuy_steam_net_factor,
                guadao_max_listing_ratio=rebuy_max_listing_ratio,
                trade_url=trade_url,
                use_live_price_as_max=False,
                max_price_override=manual_refrozen_price or replacement_max_price,
                steam_net_amount_override=manual_steam_net_amount,
            )
            submission_outcome = str(
                getattr(result, "submission_outcome", "not_submitted") or "not_submitted"
            )
            if submission_outcome == "unconfirmed":
                self._persist_c5_submission_unconfirmed(op, note, result)
                print(
                    f"[补仓待核对] {op['market_hash_name']} | "
                    f"账号={_steam_account_log_label(note) or '-'} | "
                    "C5 提交结果不确定，确认远端终态前不会重复购买"
                )
                continue
            if result.reason in (
                "steam_crashed",
                "c5_network_error",
                "ratio_no_longer_profitable",
                "no_matching_listing",
            ):
                timeout_triggered = False
                if result.reason == "no_matching_listing":
                    timeout_triggered = self._handle_no_matching_rebuy_timeout(
                        op=op,
                        note=note,
                        result=result,
                    )
                    if timeout_triggered:
                        return rebuy_count
                    no_matching_since = self._rebuy_wait_started_at.get(int(op["id"]))
                else:
                    no_matching_since = None
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
                            "noMatchingSince": no_matching_since.isoformat() if no_matching_since else None,
                            "c5OutTradeNo": getattr(result, "out_trade_no", None),
                            "c5ErrorPayload": getattr(result, "payload", None),
                        }
                    ),
                )
                steam_sold_net = None
                if result.listing_ratio_now:
                    steam_sold_net = (
                        manual_steam_net_amount
                        if manual_steam_net_amount is not None and manual_steam_net_amount > 0
                        else (
                            float(result.steam_reference_price) * float(rebuy_steam_net_factor)
                            if result.steam_reference_price is not None
                            else None
                        )
                    )
                    wait_message = (
                        f"[补仓等待] {op['market_hash_name']} | "
                        f"账号={_steam_account_log_label(note) or '-'} | "
                        f"原因: {result.reason} | "
                        f"C5价: {_format_decimal(result.actual_price)} | "
                        f"最高补仓价: {_format_decimal(result.max_price)} | "
                        f"Steam卖出税后到手: {_format_decimal(steam_sold_net)} | "
                        f"补仓比例: {_format_pct(result.listing_ratio_now)}"
                    )
                    print(wait_message)
                else:
                    print(
                        f"[补仓等待] {op['market_hash_name']} | "
                        f"账号={_steam_account_log_label(note) or '-'} | "
                        f"原因: {result.reason}"
                    )
                self._emit_guadao_local_event(
                    operation="c5_rebuy_waiting",
                    message="C5 补仓条件暂不满足，已安排后续重试",
                    level="WARNING",
                    market_hash_name=str(op["market_hash_name"]),
                    operation_id=int(op["id"]),
                    asset_id=str(op["asset_id"] or "") or None,
                    note=note,
                    context={
                        "state": "pending",
                        "reason": result.reason,
                        "c5ActualPrice": result.actual_price,
                        "c5MaxPrice": result.max_price,
                        "steamNetAmount": steam_sold_net,
                        "rebuyRatio": result.listing_ratio_now,
                        "isReplacement": bool(is_replacement),
                        "nextAttemptPolicy": "scheduled_task",
                    },
                )
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
                print(f"[补仓跳过] {op['market_hash_name']} | C5价格过高: CNY {_format_decimal(result.actual_price)}")
                continue
            if result.success and not result.skipped:
                c5_payload = getattr(result, "payload", None)
                c5_order_id = _extract_c5_order_id(c5_payload)
                c5_trade_order_id = _extract_c5_trade_order_id(c5_payload)
                c5_pay_status = safe_int(c5_payload.get("payStatus")) if isinstance(c5_payload, dict) else None
                if not _has_confirmed_c5_submission(c5_payload):
                    # Compatibility defense: even if an older/custom buy helper
                    # incorrectly says success=True, incomplete C5 evidence can
                    # never advance to delivery_pending.
                    self._persist_c5_submission_unconfirmed(op, note, result)
                    continue
                submitted_at = (
                    _parse_iso(str(getattr(result, "submitted_at", None) or ""))
                    or _now_utc()
                )
                if submitted_at.tzinfo is None:
                    submitted_at = submitted_at.replace(tzinfo=timezone.utc)
                submitted_at = submitted_at.astimezone(timezone.utc)
                delivery_deadline = submitted_at + timedelta(seconds=C5_DELIVERY_DEADLINE_SECONDS)
                self.db.update_pool_operation(
                    op["id"],
                    status="delivery_pending",
                    actual_price=result.actual_price,
                    note=_build_note(
                        {
                            **note,
                            "c5OutTradeNo": getattr(result, "out_trade_no", None),
                            "c5OrderId": c5_order_id,
                            "c5TradeOrderId": c5_trade_order_id,
                            "c5PayStatus": c5_pay_status,
                            "c5OrderRecognized": True,
                            "c5OrderRecognizedAt": utc_now_iso(),
                            "c5OrderMatchMode": "quick_buy_response_ids",
                            "c5SubmissionNotCreatedCount": 0,
                            "c5OrderStatus": "ordered",
                            C5_DELIVERY_STATUS_KEY: "pending",
                            "c5OrderSubmittedAt": submitted_at.isoformat(),
                            "c5DeliveryDeadlineAt": delivery_deadline.isoformat(),
                            "c5OrderPayload": c5_payload,
                        }
                    ),
                )
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
                # Both C5 identifiers prove the order exists regardless of
                # payStatus. Query detail in the same execution round so a
                # terminal success/failure is applied immediately; temporary
                # detail outages retain the identifiers for scheduled retry.
                detail_state, _detail_replacements = self._confirm_recognized_c5_order_detail(
                    int(op["id"])
                )
                prefix = "[补仓替换]" if is_replacement else "[补仓]"
                print(
                    f"{prefix} {op['market_hash_name']} | "
                    f"账号={_steam_account_log_label(note) or '-'} | "
                    f"C5买入 CNY {_format_decimal(result.actual_price)}"
                )
                if detail_state == "delivery_pending":
                    current = self._get_pool_operation_by_id(int(op["id"]))
                    current_note = _read_note(current["note"]) if current is not None else note
                    self._emit_guadao_local_event(
                        operation="c5_rebuy_submitted",
                        message="C5 补仓已提交，等待发货确认",
                        market_hash_name=str(op["market_hash_name"]),
                        operation_id=int(op["id"]),
                        asset_id=str(op["asset_id"] or "") or None,
                        note=current_note,
                        context={
                            "state": detail_state,
                            "c5ActualPrice": result.actual_price,
                            "c5MaxPrice": result.max_price,
                            "c5OutTradeNo": getattr(result, "out_trade_no", None),
                            "c5OrderId": c5_order_id,
                            "c5TradeOrderId": c5_trade_order_id,
                            "deliveryDeadlineAt": delivery_deadline.isoformat(),
                            "isReplacement": bool(is_replacement),
                            "replacementMaxPrice": replacement_max_price,
                        },
                    )
                rebuy_count += 1
            elif result.skipped:
                self.db.update_pool_operation(op["id"], status="dry_run")
            else:
                if _is_c5_insufficient_balance_rebuy_result(result):
                    self.db.update_pool_operation(
                        op["id"],
                        note=_build_note(
                            {
                                **note,
                                "lastSkipReason": "c5_balance_insufficient",
                                "lastNotifiedSkipReason": "c5_balance_insufficient",
                                "actualPrice": result.actual_price,
                                "c5OutTradeNo": getattr(result, "out_trade_no", None),
                                "c5ErrorPayload": getattr(result, "payload", None),
                                "balanceInsufficientAt": utc_now_iso(),
                            }
                        ),
                    )
                    print(
                        f"[补仓等待] {op['market_hash_name']} | "
                        f"账号={_steam_account_log_label(note) or '-'} | "
                        "原因: c5_balance_insufficient | 保持 pending，下轮重试"
                    )
                    self._emit_guadao_local_event(
                        operation="c5_rebuy_waiting",
                        message="C5 余额不足，补仓保持等待",
                        level="WARNING",
                        market_hash_name=str(op["market_hash_name"]),
                        operation_id=int(op["id"]),
                        asset_id=str(op["asset_id"] or "") or None,
                        note=note,
                        context={
                            "state": "pending",
                            "reason": "c5_balance_insufficient",
                            "c5ActualPrice": result.actual_price,
                            "c5MaxPrice": result.max_price,
                            "isReplacement": bool(is_replacement),
                        },
                    )
                    continue
                self.db.update_pool_operation(
                    op["id"],
                    status="failed",
                    note=_build_note(
                        {
                            **note,
                            "failedReason": result.reason,
                            "failedAt": utc_now_iso(),
                            "c5OutTradeNo": getattr(result, "out_trade_no", None),
                            "c5ErrorPayload": getattr(result, "payload", None),
                        }
                    ),
                )
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_REBUY_FAILED)
                print(
                    f"[补仓失败] {op['market_hash_name']} | "
                    f"账号={_steam_account_log_label(note) or '-'} | "
                    f"原因: {result.reason}"
                )
        return rebuy_count

    def _due_rebuy_operations_for_batch(self, market_hash_name: str) -> list[Any]:
        """Return the pending operations currently owned by this category batch.

        A per-operation ``rebuy:<id>`` clock remains the source of truth for
        retry cadence.  Its ``waiting`` state intentionally cannot be claimed
        by the generic worker; the category task below owns the one bounded
        C5 page for every due operation of this item.
        """

        now = utc_now_iso()
        due: list[Any] = []
        for op in self.db.list_pool_operations_by_type(
            OP_REBUY_C5,
            status="pending",
            limit=5000,
        ):
            if str(op["market_hash_name"] or "") != str(market_hash_name):
                continue
            task = self.db.get_scheduled_task(f"rebuy:{int(op['id'])}")
            if task is None:
                # Direct engine use in a diagnostic/test process has no
                # runtime controller to seed the per-operation clock.  It is
                # safe to treat the new pending operation as due once.
                due.append(op)
                continue
            if (
                str(task["task_type"] or "") == "rebuy_attempt"
                and str(task["status"] or "") == "waiting"
                and str(task["next_attempt_at"] or "") <= now
            ):
                due.append(op)
        return due

    @staticmethod
    def _batch_rebuy_market_rows(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        rows = payload.get("list")
        if not isinstance(rows, list) and isinstance(payload.get("data"), dict):
            rows = payload["data"].get("list")
        return [dict(row) for row in rows or [] if isinstance(row, dict)]

    def _resolve_batch_rebuy_trade_urls(
        self,
        operations: list[Any],
    ) -> tuple[dict[int, tuple[dict[str, Any], str]], int]:
        """Resolve each receiving account once, never falling back to another one."""

        resolved: dict[int, tuple[dict[str, Any], str]] = {}
        cached_urls: dict[tuple[str, str], str | None] = {}
        missing = 0
        for op in operations:
            note = _read_note(op["note"])
            inferred = self._infer_rebuy_account_fields(note)
            if inferred:
                note = {**note, **inferred}
                self.db.update_pool_operation(int(op["id"]), note=_build_note(note))
            cache_key = (
                str(note.get("steamAccountId") or "").strip(),
                str(note.get("steamId64") or "").strip(),
            )
            if cache_key not in cached_urls:
                cached_urls[cache_key] = self._resolve_rebuy_trade_url(note)
            trade_url = cached_urls[cache_key]
            if not trade_url:
                missing += 1
                self.db.update_pool_operation(
                    int(op["id"]),
                    note=_build_note(
                        {
                            **note,
                            "lastSkipReason": "missing_rebuy_trade_url",
                            "lastSkipAt": utc_now_iso(),
                        }
                    ),
                )
                continue
            resolved[int(op["id"])] = (note, trade_url)
        return resolved, missing

    def _persist_batch_rebuy_success(
        self,
        *,
        op: Any,
        note: dict[str, Any],
        request: dict[str, Any],
        response: dict[str, Any],
        submitted_at: datetime,
        submission_mode: str = "batch",
    ) -> bool:
        """Advance only a C5 submission with both real order identifiers."""

        payload = dict(response)
        order_asset_id = _extract_c5_order_id(payload)
        trade_order_id = _extract_c5_trade_order_id(payload)
        price = safe_float(payload.get("actualPay")) or safe_float(request.get("buyPrice"))
        if not order_asset_id or not trade_order_id or price is None or price <= 0:
            self._persist_c5_submission_unconfirmed(
                op,
                note,
                RebuyResult(
                    False,
                    False,
                    f"c5_{submission_mode}_success_missing_order_evidence",
                    actual_price=price,
                    max_price=safe_float(op["expected_price"]),
                    payload=payload,
                    out_trade_no=str(request.get("outTradeNo") or "") or None,
                    submitted_at=submitted_at.isoformat(),
                    submission_outcome="unconfirmed",
                ),
            )
            return False

        deadline = submitted_at + timedelta(seconds=C5_DELIVERY_DEADLINE_SECONDS)
        match_mode = (
            "gap_quick_buy_response_ids"
            if submission_mode == "gap_quick"
            else "batch_buy_response_ids"
        )
        updated_note = {
            **note,
            "c5OutTradeNo": str(request.get("outTradeNo") or "") or None,
            "c5ProductId": str(request.get("productId") or "") or None,
            "c5OrderId": order_asset_id,
            "c5TradeOrderId": trade_order_id,
            "c5PayStatus": safe_int(payload.get("payStatus")),
            "c5OrderRecognized": True,
            "c5OrderRecognizedAt": utc_now_iso(),
            "c5OrderMatchMode": match_mode,
            "c5SubmissionNotCreatedCount": 0,
            "c5OrderStatus": "ordered",
            C5_DELIVERY_STATUS_KEY: "pending",
            "c5OrderSubmittedAt": submitted_at.isoformat(),
            "c5DeliveryDeadlineAt": deadline.isoformat(),
            "c5OrderPayload": payload,
        }
        if submission_mode == "gap_quick":
            updated_note.update(
                {
                    "c5GapQuickSubmissionState": "confirmed",
                    "c5GapQuickMaxPrice": request.get("maxPrice"),
                    "c5GapQuickPriceBatchFloor": request.get("priceBatchFloor"),
                    "c5GapQuickConcreteFloor": request.get("concreteFloor"),
                    "c5GapQuickSubmittedAt": submitted_at.isoformat(),
                }
            )
        else:
            updated_note.update(
                {
                    "c5BatchSubmissionId": request.get("batchSubmissionId"),
                    "c5BatchSubmittedAt": submitted_at.isoformat(),
                }
            )
        self.db.update_pool_operation(
            int(op["id"]),
            status="delivery_pending",
            actual_price=price,
            note=_build_note(updated_note),
        )
        self.db.set_pool_status(str(op["market_hash_name"]), POOL_STATUS_PENDING_REBUY)
        self._emit_guadao_local_event(
            operation="c5_rebuy_submitted",
            message=(
                "C5 差价区快速补仓已提交，等待发货确认"
                if submission_mode == "gap_quick"
                else "C5 批量补仓已提交，等待发货确认"
            ),
            market_hash_name=str(op["market_hash_name"]),
            operation_id=int(op["id"]),
            asset_id=str(op["asset_id"] or "") or None,
            note=updated_note,
            context={
                "state": "delivery_pending",
                "c5ActualPrice": price,
                "c5MaxPrice": safe_float(op["expected_price"]),
                "c5OutTradeNo": updated_note["c5OutTradeNo"],
                "c5OrderId": order_asset_id,
                "c5TradeOrderId": trade_order_id,
                "deliveryDeadlineAt": deadline.isoformat(),
                "submissionMode": submission_mode,
                "batchSubmissionId": request.get("batchSubmissionId"),
            },
        )
        return True

    def run_guadao_rebuy_batch_task(self, market_hash_name: str) -> dict[str, Any]:
        """Process one C5 price snapshot and one bounded concrete page.

        ``price_batch`` remains the minimum-price authority.  The concrete
        page is retained once and reused for normal batch buying; when its
        visible floor is higher, a tightly capped quick-buy loop covers only
        that hidden low-price gap before the retained page is matched.
        """

        started = time.perf_counter()
        name = str(market_hash_name or "").strip()
        result: dict[str, Any] = {
            "ok": bool(name),
            "marketHashName": name,
            "dueOperations": 0,
            "eligibleOperations": 0,
            "priceBatchRequests": 0,
            "priceBatchFloor": None,
            "priceBatchItemId": None,
            "concreteListingsRead": 0,
            "concreteFloor": None,
            "priceFloorGap": None,
            "marketReadRequests": 0,
            "c5RequestCount": 0,
            "quickBuyAttempts": 0,
            "quickBuySuccesses": 0,
            "quickBuyNoMatch": 0,
            "quickBuyRejected": 0,
            "quickBuyUnconfirmed": 0,
            "matched": 0,
            "batchRequests": 0,
            "submitted": 0,
            "normalBatchMatched": 0,
            "normalBatchRequests": 0,
            "normalBatchSubmitted": 0,
            "normalBatchSuccesses": 0,
            "successes": 0,
            "failed": 0,
            "unconfirmed": 0,
            "missingTradeUrl": 0,
        }
        if not name:
            result["error"] = "missing_market_hash_name"
            return result
        if not self.config.auto_rebuy_enabled:
            result["reason"] = "auto_rebuy_disabled"
            result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        due = self._due_rebuy_operations_for_batch(name)
        result["dueOperations"] = len(due)
        if not due:
            result["reason"] = "no_due_operations"
            result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        resolved, missing_trade_url = self._resolve_batch_rebuy_trade_urls(due)
        result["missingTradeUrl"] = missing_trade_url
        eligible: list[tuple[Any, dict[str, Any], str, float]] = []
        for op in due:
            state = resolved.get(int(op["id"]))
            frozen_price = safe_float(op["expected_price"])
            if state is None or frozen_price is None or frozen_price <= 0:
                continue
            note, trade_url = state
            eligible.append((op, note, trade_url, round(frozen_price, 2)))
        result["eligibleOperations"] = len(eligible)
        if not eligible:
            result["reason"] = "no_eligible_operations"
            result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        try:
            result["priceBatchRequests"] = 1
            result["c5RequestCount"] = 1
            price_batch_payload = self.c5_client.price_batch(
                [name],
                app_id=self.settings.app_id,
            )
            price_batch_row = price_batch_payload.get(name)
            if not isinstance(price_batch_row, dict):
                raise ValueError("price_batch missing item row")
            item_id = str(price_batch_row.get("itemId") or "").strip()
            price_batch_floor = safe_float(price_batch_row.get("price"))
            if not item_id:
                raise ValueError("price_batch missing itemId")
            if price_batch_floor is None or price_batch_floor <= 0:
                raise ValueError("price_batch missing positive price")
            price_batch_floor = round(price_batch_floor, 2)
            result["priceBatchItemId"] = item_id
            result["priceBatchFloor"] = price_batch_floor
        except Exception as exc:
            for op, note, _trade_url, frozen_price in eligible:
                self.db.update_pool_operation(
                    int(op["id"]),
                    note=_build_note(
                        {
                            **note,
                            "lastSkipReason": "c5_price_batch_read_failed",
                            "lastSkipAt": utc_now_iso(),
                            "c5ErrorPayload": {"error": str(exc)},
                            "frozenRebuyPrice": frozen_price,
                        }
                    ),
                )
            result.update(
                {"ok": False, "reason": "c5_price_batch_read_failed", "error": str(exc)}
            )
            result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        try:
            result["marketReadRequests"] = 1
            result["c5RequestCount"] = int(result["c5RequestCount"]) + 1
            payload = self.c5_client.market_products_search(
                item_id=item_id,
                page_size=50,
            )
        except Exception as exc:
            for op, note, _trade_url, frozen_price in eligible:
                self.db.update_pool_operation(
                    int(op["id"]),
                    note=_build_note(
                        {
                            **note,
                            "lastSkipReason": "c5_batch_listing_read_failed",
                            "lastSkipAt": utc_now_iso(),
                            "c5ErrorPayload": {"error": str(exc)},
                            "frozenRebuyPrice": frozen_price,
                        }
                    ),
                )
            result.update({"ok": False, "reason": "c5_batch_listing_read_failed", "error": str(exc)})
            result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        concrete: list[tuple[float, str]] = []
        seen_products: set[str] = set()
        for row in self._batch_rebuy_market_rows(payload):
            product_id = str(row.get("productId") or "").strip()
            price = safe_float(row.get("price"))
            if not product_id or product_id in seen_products or price is None or price <= 0:
                continue
            price = round(price, 2)
            seen_products.add(product_id)
            concrete.append((price, product_id))
        concrete.sort(key=lambda row: (row[0], row[1]))
        result["concreteListingsRead"] = len(concrete)
        concrete_floor = concrete[0][0] if concrete else None
        result["concreteFloor"] = concrete_floor
        if concrete_floor is not None:
            result["priceFloorGap"] = round(concrete_floor - price_batch_floor, 2)

        eligible.sort(key=lambda row: (row[3], int(row[0]["id"])))
        available = list(eligible)

        # ``products/search`` is an internal-preview snapshot and can omit the
        # real minimum exposed by price_batch.  Cover only the hidden band:
        # never let quick-buy reach the first concrete listing retained below.
        price_batch_cents = int(round(price_batch_floor * 100))
        concrete_floor_cents = (
            int(round(concrete_floor * 100)) if concrete_floor is not None else None
        )
        gap_exists = (
            concrete_floor_cents is None or price_batch_cents < concrete_floor_cents
        )
        quick_limit = len(available)
        while gap_exists and int(result["quickBuyAttempts"]) < quick_limit:
            match_index = next(
                (
                    index
                    for index, (_op, _note, _url, ceiling) in enumerate(available)
                    if price_batch_cents <= int(round(ceiling * 100))
                ),
                None,
            )
            if match_index is None:
                break
            op, note, trade_url, ceiling = available[match_index]
            max_cents = int(round(ceiling * 100))
            if concrete_floor_cents is not None:
                max_cents = min(max_cents, concrete_floor_cents - 1)
            if max_cents < price_batch_cents:
                break

            previous_state = str(note.get("c5GapQuickSubmissionState") or "")
            previous_out_trade_no = str(note.get("c5GapQuickOutTradeNo") or "").strip()
            if previous_state == "submitting" and previous_out_trade_no:
                self._persist_c5_submission_unconfirmed(
                    op,
                    note,
                    RebuyResult(
                        False,
                        False,
                        "c5_gap_quick_submission_unconfirmed",
                        actual_price=price_batch_floor,
                        max_price=max_cents / 100,
                        payload={
                            "error": "previous quick-buy response was not durably recorded"
                        },
                        out_trade_no=previous_out_trade_no,
                        submitted_at=str(note.get("c5GapQuickSubmittedAt") or utc_now_iso()),
                        submission_outcome="unconfirmed",
                    ),
                )
                result["quickBuyUnconfirmed"] = int(result["quickBuyUnconfirmed"]) + 1
                result["unconfirmed"] = int(result["unconfirmed"]) + 1
                result["reason"] = "c5_gap_quick_unconfirmed"
                result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
                return result

            out_trade_no = uuid.uuid4().hex
            submitted_at = _now_utc()
            max_price = max_cents / 100
            quick_request = {
                "buyPrice": price_batch_floor,
                "maxPrice": max_price,
                "outTradeNo": out_trade_no,
                "priceBatchFloor": price_batch_floor,
                "concreteFloor": concrete_floor,
            }
            persisted_note = {
                **note,
                "c5OutTradeNo": out_trade_no,
                "c5GapQuickOutTradeNo": out_trade_no,
                "c5GapQuickSubmissionState": "submitting",
                "c5GapQuickSubmittedAt": submitted_at.isoformat(),
                "c5GapQuickMaxPrice": max_price,
                "c5GapQuickPriceBatchFloor": price_batch_floor,
                "c5GapQuickConcreteFloor": concrete_floor,
            }
            # The idempotency key is durable before the HTTP call.  A lost
            # response therefore enters reconciliation instead of rebuying.
            self.db.update_pool_operation(
                int(op["id"]),
                note=_build_note(persisted_note),
            )
            result["quickBuyAttempts"] = int(result["quickBuyAttempts"]) + 1
            result["c5RequestCount"] = int(result["c5RequestCount"]) + 1
            try:
                quick_response = self.c5_client.quick_buy(
                    app_id=self.settings.app_id,
                    item_id=item_id,
                    max_price=max_price,
                    out_trade_no=out_trade_no,
                    trade_url=trade_url,
                )
            except C5GameError as exc:
                try:
                    error_payload = json.loads(str(exc))
                except (TypeError, ValueError):
                    error_payload = None
                if isinstance(error_payload, dict) and safe_int(
                    error_payload.get("errorCode")
                ) in {1317, 1014452}:
                    rejected_note = {
                        **persisted_note,
                        "lastSkipReason": "no_matching_listing",
                        "lastSkipAt": utc_now_iso(),
                        "c5GapQuickSubmissionState": "rejected_no_match",
                        "c5ErrorPayload": error_payload,
                    }
                    self.db.update_pool_operation(
                        int(op["id"]),
                        note=_build_note(rejected_note),
                    )
                    available[match_index] = (op, rejected_note, trade_url, ceiling)
                    result["quickBuyNoMatch"] = int(result["quickBuyNoMatch"]) + 1
                    break
                if isinstance(error_payload, dict):
                    self.db.update_pool_operation(
                        int(op["id"]),
                        note=_build_note(
                            {
                                **persisted_note,
                                "lastSkipReason": "c5_gap_quick_rejected",
                                "lastSkipAt": utc_now_iso(),
                                "c5GapQuickSubmissionState": "rejected",
                                "c5ErrorPayload": error_payload,
                            }
                        ),
                    )
                    result["quickBuyRejected"] = int(result["quickBuyRejected"]) + 1
                    result["failed"] = int(result["failed"]) + 1
                    result["reason"] = "c5_gap_quick_rejected"
                    result["elapsedMs"] = round(
                        (time.perf_counter() - started) * 1000,
                        1,
                    )
                    return result
                quick_response = None
                quick_error: Exception | None = exc
            except Exception as exc:
                quick_response = None
                quick_error = exc
            else:
                quick_error = None

            if quick_error is not None or not _has_confirmed_c5_submission(quick_response):
                payload_for_reconcile = (
                    dict(quick_response)
                    if isinstance(quick_response, dict)
                    else {
                        "error": str(quick_error or "missing C5 order identifiers"),
                        "exceptionType": (
                            type(quick_error).__name__ if quick_error is not None else None
                        ),
                    }
                )
                self._persist_c5_submission_unconfirmed(
                    op,
                    persisted_note,
                    RebuyResult(
                        False,
                        False,
                        "c5_gap_quick_submission_unconfirmed",
                        actual_price=price_batch_floor,
                        max_price=max_price,
                        payload=payload_for_reconcile,
                        out_trade_no=out_trade_no,
                        submitted_at=submitted_at.isoformat(),
                        submission_outcome="unconfirmed",
                    ),
                )
                result["quickBuyUnconfirmed"] = int(result["quickBuyUnconfirmed"]) + 1
                result["unconfirmed"] = int(result["unconfirmed"]) + 1
                result["reason"] = "c5_gap_quick_unconfirmed"
                result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
                return result

            assert isinstance(quick_response, dict)
            if self._persist_batch_rebuy_success(
                op=op,
                note=persisted_note,
                request=quick_request,
                response=quick_response,
                submitted_at=submitted_at,
                submission_mode="gap_quick",
            ):
                available.pop(match_index)
                result["quickBuySuccesses"] = int(result["quickBuySuccesses"]) + 1
                result["successes"] = int(result["successes"]) + 1
            else:
                result["quickBuyUnconfirmed"] = int(result["quickBuyUnconfirmed"]) + 1
                result["unconfirmed"] = int(result["unconfirmed"]) + 1
                result["reason"] = "c5_gap_quick_unconfirmed"
                result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
                return result

        # Lowest listing gets the tightest ceiling that still accepts it.  A
        # looser order is kept for a later, more expensive product, maximising
        # the number of filled pending rebuys from this one 50-row snapshot.
        selected: list[tuple[Any, dict[str, Any], str, float, str]] = []
        for price, product_id in concrete:
            match_index = next(
                (
                    index
                    for index, (_op, _note, _url, ceiling) in enumerate(available)
                    if price <= ceiling + 0.005
                ),
                None,
            )
            if match_index is None:
                continue
            op, note, trade_url, _ceiling = available.pop(match_index)
            selected.append((op, note, trade_url, price, product_id))
        result["matched"] = len(selected)
        result["normalBatchMatched"] = len(selected)
        if not selected:
            for op, note, _trade_url, frozen_price in available:
                self.db.update_pool_operation(
                    int(op["id"]),
                    note=_build_note(
                        {
                            **note,
                            "lastSkipReason": "no_matching_listing",
                            "lastSkipAt": utc_now_iso(),
                            "c5BatchConcreteListingCount": len(concrete),
                            "c5PriceBatchFloor": price_batch_floor,
                            "c5ConcreteFloor": concrete_floor,
                            "frozenRebuyPrice": frozen_price,
                        }
                    ),
                )
            result["reason"] = (
                "quick_buy_gap_exhausted"
                if int(result["quickBuySuccesses"]) > 0
                else "no_matching_listing"
            )
            result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        batch_submission_id = uuid.uuid4().hex
        submitted_at = _now_utc()
        by_trade_url: dict[str, list[tuple[Any, dict[str, Any], dict[str, Any]]]] = {}
        for op, note, trade_url, price, product_id in selected:
            request = {
                "productId": product_id,
                "buyPrice": price,
                "outTradeNo": uuid.uuid4().hex,
                "batchSubmissionId": batch_submission_id,
            }
            persisted_note = {
                **note,
                "c5BatchSubmissionId": batch_submission_id,
                "c5BatchSubmittedAt": submitted_at.isoformat(),
                "c5OutTradeNo": request["outTradeNo"],
                "c5ProductId": product_id,
                "c5BatchRequestedPrice": price,
                "c5BatchSubmissionState": "submitting",
            }
            # Persist the product/outTradeNo map before the HTTP call.  If the
            # process loses the response, reconciliation has durable evidence
            # and the same product will never be blindly bought again.
            self.db.update_pool_operation(int(op["id"]), note=_build_note(persisted_note))
            by_trade_url.setdefault(trade_url, []).append((op, persisted_note, request))

        result["submitted"] = len(selected)
        result["normalBatchSubmitted"] = len(selected)
        for trade_url, group in by_trade_url.items():
            result["batchRequests"] = int(result["batchRequests"]) + 1
            result["normalBatchRequests"] = int(result["normalBatchRequests"]) + 1
            result["c5RequestCount"] = int(result["c5RequestCount"]) + 1
            product_list = [request for _op, _note, request in group]
            requests_by_out = {str(request["outTradeNo"]): (op, note, request) for op, note, request in group}
            requests_by_product = {str(request["productId"]): (op, note, request) for op, note, request in group}
            try:
                response = self.c5_client.batch_buy(
                    product_list=[
                        {
                            "productId": request["productId"],
                            "buyPrice": request["buyPrice"],
                            "outTradeNo": request["outTradeNo"],
                        }
                        for request in product_list
                    ],
                    trade_url=trade_url,
                )
            except Exception as exc:
                response = {"_batchException": str(exc)}

            success_rows = response.get("successList") if isinstance(response, dict) else None
            failed_rows = response.get("failedList") if isinstance(response, dict) else None
            if not isinstance(success_rows, list) or not isinstance(failed_rows, list):
                success_rows, failed_rows = [], []
                response = dict(response) if isinstance(response, dict) else {"response": response}
                response.setdefault("_batchUnconfirmedReason", "missing_success_or_failed_list")

            resolved_out_trade_nos: set[str] = set()
            for row in success_rows:
                if not isinstance(row, dict):
                    continue
                match = (
                    requests_by_out.get(str(row.get("outTradeNo") or ""))
                    or requests_by_product.get(str(row.get("productId") or ""))
                )
                if match is None:
                    continue
                op, note, request = match
                resolved_out_trade_nos.add(str(request["outTradeNo"]))
                merged = {**request, **row}
                if self._persist_batch_rebuy_success(
                    op=op,
                    note=note,
                    request=request,
                    response=merged,
                    submitted_at=submitted_at,
                ):
                    result["successes"] = int(result["successes"]) + 1
                    result["normalBatchSuccesses"] = int(result["normalBatchSuccesses"]) + 1
                else:
                    result["unconfirmed"] = int(result["unconfirmed"]) + 1

            for row in failed_rows:
                if not isinstance(row, dict):
                    continue
                match = (
                    requests_by_out.get(str(row.get("outTradeNo") or ""))
                    or requests_by_product.get(str(row.get("productId") or ""))
                )
                if match is None:
                    continue
                op, note, request = match
                resolved_out_trade_nos.add(str(request["outTradeNo"]))
                self.db.update_pool_operation(
                    int(op["id"]),
                    note=_build_note(
                        {
                            **note,
                            "lastSkipReason": "c5_batch_rejected",
                            "lastSkipAt": utc_now_iso(),
                            "c5BatchSubmissionState": "rejected",
                            "c5ErrorPayload": dict(row),
                            "c5OutTradeNo": request["outTradeNo"],
                        }
                    ),
                )
                result["failed"] = int(result["failed"]) + 1

            for out_trade_no, (op, note, request) in requests_by_out.items():
                if out_trade_no in resolved_out_trade_nos:
                    continue
                self._persist_c5_submission_unconfirmed(
                    op,
                    note,
                    RebuyResult(
                        False,
                        False,
                        "c5_batch_submission_unconfirmed",
                        actual_price=safe_float(request.get("buyPrice")),
                        max_price=safe_float(op["expected_price"]),
                        payload=response,
                        out_trade_no=out_trade_no,
                        submitted_at=submitted_at.isoformat(),
                        submission_outcome="unconfirmed",
                    ),
                )
                result["unconfirmed"] = int(result["unconfirmed"]) + 1

        result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
        return result

    def run_guadao_scan_task(self) -> dict[str, Any]:
        """Run only the new-candidate branch; existing-state work has its own tasks."""

        self._pending_confirmation_count = 0
        self._market_pending_cleanup_failed_count = 0
        self._steam_market_validated_accounts = set()
        self._sync_assets()
        pool_names = self.db.get_pool_market_hash_names()
        if not pool_names:
            return {"ok": True, "listed": 0, "evaluated": 0, "reason": "empty_pool"}
        weapon_case_market_hash_names = {
            name for name in pool_names if self._is_weapon_case(name)
        }
        scan_pool_names = self._pool_names_for_strategy_scan(pool_names)
        report = scan_strategies(
            self.settings,
            self.config,
            allow_cached_fallback=True,
            cache_max_age_minutes=180,
            pool_market_hash_names=scan_pool_names,
            inventory_payload=self._last_inventory_payload,
            weapon_case_market_hash_names=weapon_case_market_hash_names,
            steam_request_source="guadao",
            refresh_steam_accounts=False,
            steam_orderbook_max_workers=1,
            steam_orderbook_admission_timeout_seconds=(
                GUADAO_SCAN_ORDERBOOK_ADMISSION_SECONDS
            ),
            steam_orderbook_price_resolver=self._guadao_scan_orderbook_price,
        )
        status_map = self.db.get_pool_status_map()
        blocked_by_open_cycle = self._has_open_guadao_cycle(status_map)
        inventory_infos = {
            info.candidate.market_hash_name: info
            for info in self._guadao_account_inventory_infos(report)
        }
        listed = 0
        if not blocked_by_open_cycle:
            listed = self._execute_guadao_listings(report, status_map)
        self._release_full_case_listing_capacity()
        candidate_names = {
            candidate.market_hash_name
            for candidate in list(getattr(report, "guadao_candidates", []) or [])
        }
        evaluated_items: list[dict[str, Any]] = []
        rejection_counts: dict[str, int] = {}
        executable_count = 0
        for candidate in list(getattr(report, "all_evaluated", []) or []):
            market_hash_name = str(candidate.market_hash_name)
            info = inventory_infos.get(market_hash_name)
            max_ratio = float(
                self.config.guadao_max_listing_ratio_for(market_hash_name)
            )
            eligible = market_hash_name in candidate_names
            local_available = int(info.configured_available) if info is not None else 0
            if not eligible:
                decision = "ratio_above_limit"
            elif local_available <= 0:
                decision = "no_local_executable_asset"
            elif blocked_by_open_cycle:
                decision = "waiting_existing_cycle"
            else:
                decision = "executable_candidate"
                executable_count += 1
            rejection_counts[decision] = rejection_counts.get(decision, 0) + 1
            evaluated_items.append(
                {
                    "name": candidate.name,
                    "marketHashName": market_hash_name,
                    "listingRatio": round(float(candidate.listing_ratio), 6),
                    "listingRatioPct": round(float(candidate.listing_ratio_pct), 2),
                    "maxListingRatio": round(max_ratio, 6),
                    "maxListingRatioPct": round(max_ratio * 100.0, 2),
                    "steamListPrice": float(candidate.steam_sell_price),
                    "steamNetAmount": round(float(candidate.steam_after_tax_price), 2),
                    "c5RebuyPrice": float(candidate.rebuy_price),
                    "inventoryCount": int(candidate.inventory_count),
                    "c5TradableCount": int(candidate.tradable_count),
                    "localExecutableCount": local_available,
                    "accountInventory": [
                        {"accountName": account_name, "count": int(count)}
                        for account_name, count in (info.account_counts if info else [])
                    ],
                    "eligible": eligible,
                    "decision": decision,
                }
            )
        evaluated_names = {
            str(item.get("marketHashName") or "")
            for item in evaluated_items
        }
        raw_outcomes = list(getattr(report, "item_outcomes", []) or [])
        outcome_counts: dict[str, int] = {}
        missing_price_items: list[dict[str, Any]] = []
        queue_deferred_count = 0
        for raw_outcome in raw_outcomes:
            if not isinstance(raw_outcome, dict):
                continue
            status = str(raw_outcome.get("status") or "unclassified")
            outcome_counts[status] = outcome_counts.get(status, 0) + 1
            market_hash_name = str(raw_outcome.get("marketHashName") or "")
            if status == "evaluated" or market_hash_name in evaluated_names:
                continue
            rejection_counts[status] = rejection_counts.get(status, 0) + 1
            if status == "queue_deferred":
                queue_deferred_count += 1
            if status in {"steam_price_missing", "c5_price_missing"}:
                missing_price_items.append(
                    {
                        "name": raw_outcome.get("name"),
                        "marketHashName": market_hash_name,
                        "status": status,
                        "reason": raw_outcome.get("reason"),
                        "stage": raw_outcome.get("stage"),
                    }
                )
            evaluated_items.append(
                {
                    "name": raw_outcome.get("name") or market_hash_name,
                    "marketHashName": market_hash_name,
                    "listingRatio": raw_outcome.get("listingRatio"),
                    "listingRatioPct": raw_outcome.get("listingRatioPct"),
                    "maxListingRatio": None,
                    "maxListingRatioPct": None,
                    "steamListPrice": raw_outcome.get("steamListPrice"),
                    "steamNetAmount": None,
                    "c5RebuyPrice": raw_outcome.get("c5RebuyPrice"),
                    "inventoryCount": raw_outcome.get("inventoryCount"),
                    "c5TradableCount": raw_outcome.get("tradableCount"),
                    "localExecutableCount": None,
                    "accountInventory": [],
                    "eligible": False,
                    "decision": status,
                    "reason": raw_outcome.get("reason"),
                    "stage": raw_outcome.get("stage"),
                    "requestSent": raw_outcome.get("requestSent"),
                }
            )
        evaluated_items.sort(
            key=lambda item: (
                float(item.get("listingRatio") or 9999.0),
                str(item.get("marketHashName") or ""),
            )
        )
        scan_round = {
            "generatedAt": getattr(report, "generated_at", utc_now_iso()),
            "inventorySource": getattr(report, "inventory_source", None),
            "poolTypeCount": int(getattr(report, "total_pool_types", 0) or 0),
            "evaluatedCount": len(getattr(report, "all_evaluated", []) or []),
            "missingPriceCount": int(getattr(report, "missing_price_count", 0) or 0),
            "notEvaluatedCount": max(
                0,
                int(getattr(report, "total_pool_types", 0) or 0)
                - len(getattr(report, "all_evaluated", []) or []),
            ),
            "queueDeferredCount": queue_deferred_count,
            "outcomeCounts": outcome_counts,
            "missingPriceItems": missing_price_items,
            "candidateCount": len(candidate_names),
            "executableCount": executable_count,
            "listedCount": listed,
            "blockedByOpenCycle": blocked_by_open_cycle,
            "globalMaxListingRatioPct": round(
                float(self.config.guadao_max_listing_ratio) * 100.0,
                2,
            ),
            "decisionCounts": rejection_counts,
            # Keep each persisted round bounded. Counts above still cover the
            # complete scan; rows retain the most useful lowest-ratio items.
            "items": evaluated_items[:80],
            "itemsTruncated": len(evaluated_items) > 80,
        }
        return {
            "ok": True,
            "listed": listed,
            "evaluated": len(getattr(report, "all_evaluated", []) or []),
            "candidateCount": len(getattr(report, "guadao_candidates", []) or []),
            "executableCount": executable_count,
            "missingPriceCount": int(getattr(report, "missing_price_count", 0) or 0),
            "notEvaluatedCount": max(
                0,
                int(getattr(report, "total_pool_types", 0) or 0)
                - len(getattr(report, "all_evaluated", []) or []),
            ),
            "queueDeferredCount": queue_deferred_count,
            "generatedAt": getattr(report, "generated_at", utc_now_iso()),
            "scanRound": scan_round,
        }

    def _load_account_my_listings_snapshot(
        self,
        client: SteamMarketClient,
    ) -> dict[str, Any]:
        loader = getattr(client, "my_listings_snapshot", None)
        if callable(loader):
            snapshot = loader()
            return {
                "active": list(getattr(snapshot, "active_listings", ()) or ()),
                "pending": list(getattr(snapshot, "pending_listings", ()) or ()),
                "officialActiveCount": safe_int(
                    getattr(snapshot, "official_active_count", None)
                ),
                "actualActiveCount": max(
                    0,
                    int(getattr(snapshot, "actual_active_count", 0) or 0),
                ),
                "pagesScanned": max(
                    0,
                    int(getattr(snapshot, "pages_scanned", 0) or 0),
                ),
                "complete": bool(getattr(snapshot, "complete", False)),
                "observedAt": str(getattr(snapshot, "observed_at", "") or "")
                or utc_now_iso(),
                "error": str(getattr(snapshot, "error", "") or "") or None,
            }

        # Compatibility for isolated injected clients.  Production always uses
        # the pagination-aware snapshot above.
        active = list(client.list_active_listings())
        pending_loader = getattr(client, "list_confirmation_pending_listings", None)
        pending = list(pending_loader()) if callable(pending_loader) else []
        return {
            "active": active,
            "pending": pending,
            "officialActiveCount": len(active),
            "actualActiveCount": len(active),
            "pagesScanned": 1,
            "complete": True,
            "observedAt": utc_now_iso(),
            "error": None,
        }

    def _operation_matches_listing_rows(
        self,
        op: Any,
        *,
        listing_ids: set[str],
        asset_ids: set[str],
    ) -> bool:
        note = _read_note(op["note"])
        return self._listing_is_active(
            active_listing_ids=listing_ids,
            active_asset_ids=asset_ids,
            listing_id=str(note.get("listingId") or "").strip(),
            asset_id=str(op["asset_id"] or "").strip(),
        )

    def _apply_my_listings_active_evidence(
        self,
        client: SteamMarketClient,
        operations: list[Any],
        active_listings: list[Any],
    ) -> tuple[set[int], int]:
        listing_ids, asset_ids = self._active_listing_identity_sets(active_listings)
        resolved_ids: set[int] = set()
        newly_listed = 0
        verified_at = utc_now_iso()
        for op in operations:
            if not self._operation_matches_client(op, client):
                continue
            if not self._operation_matches_listing_rows(
                op,
                listing_ids=listing_ids,
                asset_ids=asset_ids,
            ):
                continue
            operation_id = int(op["id"])
            note = _read_note(op["note"])
            raw_status = str(op["status"] or "")
            listing_id = str(note.get("listingId") or "").strip()
            asset_id = str(op["asset_id"] or "").strip()
            if not listing_id and asset_id:
                recovered_listing_id = self._active_listing_id_for_asset(
                    active_listings,
                    asset_id,
                )
                if recovered_listing_id:
                    note["listingId"] = recovered_listing_id
            previous_confirmation_status = str(
                note.get("confirmationStatus") or ""
            )
            note["confirmationStatus"] = (
                "listing_active_reverified"
                if raw_status == "listed"
                or previous_confirmation_status == "listing_missing_unverified"
                else "confirmed_late"
            )
            note["activeVerifiedAt"] = verified_at
            note["confirmationRecoveredAt"] = verified_at
            self._mark_steam_listing_active(op, note)
            resolved_ids.add(operation_id)
            if raw_status != "listed":
                newly_listed += 1
        self.db.conn.commit()
        return resolved_ids, newly_listed

    def _record_my_listings_pending_evidence(
        self,
        client: SteamMarketClient,
        operations: list[Any],
        pending_listings: list[Any],
    ) -> set[int]:
        pending_listing_ids, pending_asset_ids = self._active_listing_identity_sets(
            pending_listings
        )
        pending_ids: set[int] = set()
        observed_at = utc_now_iso()
        for op in operations:
            if not self._operation_matches_client(op, client):
                continue
            if not self._operation_matches_listing_rows(
                op,
                listing_ids=pending_listing_ids,
                asset_ids=pending_asset_ids,
            ):
                continue
            operation_id = int(op["id"])
            note = _read_note(op["note"])
            asset_id = str(op["asset_id"] or "").strip()
            pending_listing_id = (
                str(note.get("listingId") or "").strip()
                or self._active_listing_id_for_asset(pending_listings, asset_id)
                or ""
            )
            if pending_listing_id:
                note["listingId"] = pending_listing_id
                note["marketPendingListingId"] = pending_listing_id
            note["confirmationStatus"] = "market_pending_visible"
            note["marketPendingVerifiedAt"] = observed_at
            note["listingPendingAt"] = note.get("listingPendingAt") or observed_at
            self.db.update_pool_operation(
                operation_id,
                status=POOL_STATUS_LISTING_PENDING,
                note=_build_note(note),
            )
            self.db.set_pool_status(
                op["market_hash_name"],
                POOL_STATUS_LISTING_PENDING,
            )
            if asset_id:
                self.db.set_asset_status(asset_id, "listing_pending")
            pending_ids.add(operation_id)
        self.db.conn.commit()
        return pending_ids

    def _confirm_pending_listing_operations_batch(
        self,
        client: SteamMarketClient,
        operations: list[Any],
        *,
        pending_listings: list[Any],
        confirmation_operation_ids: set[int] | None,
    ) -> tuple[set[int], str | None]:
        if confirmation_operation_ids == set():
            return set(), None
        pending_listing_ids, pending_asset_ids = self._active_listing_identity_sets(
            pending_listings
        )
        targets: list[Any] = []
        for op in operations:
            operation_id = int(op["id"])
            if (
                confirmation_operation_ids is not None
                and operation_id not in confirmation_operation_ids
            ):
                continue
            if not self._operation_matches_client(op, client):
                continue
            if not self._operation_matches_listing_rows(
                op,
                listing_ids=pending_listing_ids,
                asset_ids=pending_asset_ids,
            ):
                continue
            targets.append(op)
        if not targets:
            return set(), None
        confirmer = getattr(client, "confirm_listing_assets", None)
        if not callable(confirmer):
            return {
                int(op["id"]) for op in targets
            }, "Steam client does not support scoped listing confirmation"

        asset_ids = [
            str(op["asset_id"] or "").strip()
            for op in targets
            if str(op["asset_id"] or "").strip()
        ]
        listing_ids = [
            str(_read_note(op["note"]).get("listingId") or "").strip()
            for op in targets
            if str(_read_note(op["note"]).get("listingId") or "").strip()
        ]
        attempted_ids = {int(op["id"]) for op in targets}
        attempted_at = utc_now_iso()
        error: str | None = None
        confirmed_count: int | None = None
        try:
            try:
                confirmed_count = int(
                    confirmer(
                        asset_ids=asset_ids,
                        listing_ids=listing_ids or None,
                        pending_listings=pending_listings,
                    )
                    or 0
                )
            except TypeError as exc:
                # Test doubles and older injected clients may not yet accept
                # the cached pending snapshot.  The fallback is still one
                # account-batched confirmation call.
                if "pending_listings" not in str(exc):
                    raise
                confirmed_count = int(
                    confirmer(
                        asset_ids=asset_ids,
                        listing_ids=listing_ids or None,
                    )
                    or 0
                )
        except Exception as exc:
            error = str(exc)

        for op in targets:
            note = _read_note(op["note"])
            note["confirmationRetryAt"] = attempted_at
            if error:
                note["confirmationRetryStatus"] = "failed"
                note["confirmationRetryMessage"] = error
            else:
                note["confirmationRetryStatus"] = (
                    "confirmed_waiting_active_listing"
                    if (confirmed_count or 0) > 0
                    else "not_found"
                )
                note["confirmationRetryCount"] = confirmed_count
            self.db.update_pool_operation(op["id"], note=_build_note(note))
        self.db.conn.commit()
        return attempted_ids, error

    def run_guadao_account_sync_task(
        self,
        account_id: str | None,
        *,
        confirmation_operation_ids: set[int] | None = None,
        sale_operation_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        if confirmation_operation_ids == set() and sale_operation_ids == set():
            return {
                "ok": True,
                "skipped": True,
                "reason": "no_due_operations",
                "accountId": account_id,
                "confirmed": 0,
                "backfilled": 0,
                "sold": 0,
                "myListingsResolved": 0,
                "historySold": 0,
                "historyDeferred": 0,
                "historyError": None,
                "partial": False,
            }
        account = self._account_by_id(account_id) if account_id else self.account
        steam_id = str(account.steam_id64 or "").strip() if account else None
        client = self._steam_client_for_account(account, steam_id)
        if client is None:
            return {"ok": False, "sold": 0, "error": "steam client unavailable"}
        self._steam_market_validated_accounts = set()

        due_operations = self.db.list_pool_operations_by_type_and_statuses(
            OP_SELL_STEAM,
            statuses=[POOL_STATUS_LISTING_PENDING, "listed", "manual_required"],
            limit=5000,
        )
        if confirmation_operation_ids is not None or sale_operation_ids is not None:
            due_operation_ids = set(confirmation_operation_ids or set()) | set(
                sale_operation_ids or set()
            )
            due_operations = [
                op for op in due_operations if int(op["id"]) in due_operation_ids
            ]

        # Phase 1: MyListings is the first and independent source of truth.
        # Even when a later page is unavailable, exact active/pending matches
        # are committed before history is considered.
        try:
            snapshot = self._load_account_my_listings_snapshot(client)
        except Exception as exc:
            return {
                "ok": False,
                "accountId": account.id if account else account_id,
                "steamId": steam_id,
                "confirmed": 0,
                "backfilled": 0,
                "sold": 0,
                "myListingsResolved": 0,
                "historySold": 0,
                "historyDeferred": len(due_operations),
                "historyDeferredOperationIds": [
                    int(op["id"]) for op in due_operations
                ],
                "historyError": None,
                "partial": True,
                "error": f"Steam MyListings snapshot failed: {exc}",
            }

        active_listings = list(snapshot["active"])
        pending_listings = list(snapshot["pending"])
        active_resolved_ids, initial_confirmed = (
            self._apply_my_listings_active_evidence(
                client,
                due_operations,
                active_listings,
            )
        )
        unresolved_after_active = [
            op for op in due_operations if int(op["id"]) not in active_resolved_ids
        ]
        pending_operation_ids = self._record_my_listings_pending_evidence(
            client,
            unresolved_after_active,
            pending_listings,
        )
        attempted_confirmation_ids, confirmation_error = (
            self._confirm_pending_listing_operations_batch(
                client,
                unresolved_after_active,
                pending_listings=pending_listings,
                confirmation_operation_ids=confirmation_operation_ids,
            )
        )

        confirmed = initial_confirmed
        if attempted_confirmation_ids and not confirmation_error:
            try:
                post_confirmation_snapshot = self._load_account_my_listings_snapshot(
                    client
                )
            except Exception as exc:
                post_confirmation_snapshot = {
                    **snapshot,
                    "complete": False,
                    "error": f"post-confirmation MyListings failed: {exc}",
                }
            snapshot = post_confirmation_snapshot
            active_listings = list(snapshot["active"])
            pending_listings = list(snapshot["pending"])
            post_resolved_ids, post_confirmed = (
                self._apply_my_listings_active_evidence(
                    client,
                    [
                        op
                        for op in due_operations
                        if int(op["id"]) not in active_resolved_ids
                    ],
                    active_listings,
                )
            )
            active_resolved_ids.update(post_resolved_ids)
            confirmed += post_confirmed
            pending_operation_ids.update(
                self._record_my_listings_pending_evidence(
                    client,
                    [
                        op
                        for op in due_operations
                        if int(op["id"]) not in active_resolved_ids
                    ],
                    pending_listings,
                )
            )
        pending_operation_ids.difference_update(active_resolved_ids)
        self.db.conn.commit()

        snapshot_complete = bool(snapshot.get("complete"))
        snapshot_error = str(snapshot.get("error") or "") or None
        history_candidates = [
            op
            for op in due_operations
            if int(op["id"]) not in active_resolved_ids
            and int(op["id"]) not in pending_operation_ids
        ]
        history_candidate_ids = {int(op["id"]) for op in history_candidates}

        if not snapshot_complete:
            # An incomplete MyListings walk can prove rows that were seen, but
            # it cannot prove absence.  MyHistory is deliberately not queried.
            return {
                "ok": bool(snapshot.get("pagesScanned"))
                or bool(active_listings)
                or bool(pending_listings),
                "accountId": account.id if account else account_id,
                "steamId": steam_id,
                "confirmed": confirmed,
                "backfilled": 0,
                "sold": 0,
                "myListingsResolved": len(active_resolved_ids),
                "historySold": 0,
                "historyDeferred": len(history_candidate_ids),
                "historyDeferredOperationIds": sorted(history_candidate_ids),
                "historyError": None,
                "myListingsComplete": False,
                "myListingsOfficialCount": snapshot.get("officialActiveCount"),
                "myListingsReadCount": snapshot.get("actualActiveCount"),
                "myListingsPages": snapshot.get("pagesScanned"),
                "myListingsObservedAt": snapshot.get("observedAt"),
                "myListingsError": snapshot_error,
                "partial": True,
                **(
                    {"error": f"Steam MyListings incomplete: {snapshot_error}"}
                    if not snapshot.get("pagesScanned") and snapshot_error
                    else {}
                ),
            }

        # Phase 2: only operations absent from the complete active and pending
        # snapshot are allowed to enter MyHistory.
        receipt_lookup = self._lookup_steam_sale_receipts_for_operations(
            client,
            history_candidates,
            active_listing_ids=set(),
            active_asset_ids=set(),
            raise_on_error=False,
        )
        history_sold = 0
        existing_rebuy_sources: set[str] | None = None
        existing_rebuy_sell_ops: set[str] | None = None
        receipt_positive_ids: set[int] = set()
        operations_by_id = {int(op["id"]): op for op in history_candidates}
        for operation_id, sale_receipt in receipt_lookup.receipts.items():
            if sale_receipt is None:
                continue
            op = operations_by_id.get(int(operation_id))
            if op is None:
                continue
            if existing_rebuy_sources is None or existing_rebuy_sell_ops is None:
                (
                    existing_rebuy_sources,
                    existing_rebuy_sell_ops,
                ) = self._load_existing_rebuy_source_keys()
            self._mark_steam_listing_sold(
                op,
                _read_note(op["note"]),
                sale_receipt=sale_receipt,
                existing_rebuy_sources=existing_rebuy_sources,
                existing_rebuy_sell_ops=existing_rebuy_sell_ops,
            )
            receipt_positive_ids.add(int(operation_id))
            history_sold += 1
        self.db.conn.commit()

        if receipt_lookup.lookup_succeeded:
            no_receipt_ids = history_candidate_ids - receipt_positive_ids
            if no_receipt_ids:
                self._refresh_listings(
                    client=client,
                    active_listings=active_listings,
                    operation_ids=no_receipt_ids,
                    sale_receipt_results={
                        operation_id: None for operation_id in no_receipt_ids
                    },
                    sale_receipt_deep_attempt_ids=receipt_lookup.deep_attempt_ids,
                    sale_receipt_deep_attempted_at=receipt_lookup.deep_attempted_at,
                    sale_receipt_lookup_succeeded=True,
                    sale_receipt_coverage_complete=receipt_lookup.coverage_complete,
                )

        deferred_ids: list[int] = []
        for operation_id in sorted(history_candidate_ids - receipt_positive_ids):
            row = self._get_pool_operation_by_id(operation_id)
            if row is None:
                continue
            if str(row["status"] or "") in {
                POOL_STATUS_LISTING_PENDING,
                "listed",
                "manual_required",
            }:
                deferred_ids.append(operation_id)
        history_error = receipt_lookup.error
        partial = bool(
            confirmation_error
            or history_error
            or (deferred_ids and not receipt_lookup.coverage_complete)
        )
        result = {
            "ok": True,
            "accountId": account.id if account else account_id,
            "steamId": steam_id,
            "confirmed": confirmed,
            "backfilled": 0,
            "sold": history_sold,
            "myListingsResolved": len(active_resolved_ids),
            "historySold": history_sold,
            "historyDeferred": len(deferred_ids),
            "historyDeferredOperationIds": deferred_ids,
            "historyError": history_error,
            "historyRetryAt": receipt_lookup.retry_at,
            "historyPages": receipt_lookup.pages_scanned,
            "historyCoverageComplete": receipt_lookup.coverage_complete,
            "myListingsComplete": True,
            "myListingsOfficialCount": snapshot.get("officialActiveCount"),
            "myListingsReadCount": snapshot.get("actualActiveCount"),
            "myListingsPages": snapshot.get("pagesScanned"),
            "myListingsObservedAt": snapshot.get("observedAt"),
            "myListingsError": snapshot_error,
            "partial": partial,
        }
        if confirmation_error:
            result["confirmationError"] = confirmation_error
        return result

    def run_guadao_rebuy_task(self, operation_id: int) -> dict[str, Any]:
        count = self._execute_rebuys(operation_id=operation_id)
        row = self._get_pool_operation_by_id(int(operation_id))
        return {
            "ok": row is not None,
            "operationId": int(operation_id),
            "rebought": count,
            "status": str(row["status"] or "") if row is not None else "missing",
        }

    def run_guadao_c5_submission_reconcile_task(self, operation_id: int) -> dict[str, Any]:
        row = self._get_pool_operation_by_id(int(operation_id))
        if row is None:
            return {
                "ok": False,
                "operationId": int(operation_id),
                "state": "missing",
                "status": "missing",
                "checked": 0,
                "replacements": 0,
            }
        status = str(row["status"] or "")
        if status != C5_SUBMISSION_UNCONFIRMED:
            return {
                "ok": True,
                "operationId": int(operation_id),
                "state": status,
                "status": status,
                "checked": 0,
                "replacements": 0,
            }
        state, replacements, checked = self._reconcile_c5_submission(row, _read_note(row["note"]))
        return {
            "ok": True,
            "operationId": int(operation_id),
            "state": state,
            "status": state,
            "checked": checked,
            "replacements": replacements,
        }

    def run_guadao_delivery_confirmation_task(self, operation_id: int) -> dict[str, Any]:
        replacements = self._check_recent_rebuy_delivery_failures(operation_id=operation_id)
        row = self._get_pool_operation_by_id(int(operation_id))
        return {
            "ok": row is not None,
            "operationId": int(operation_id),
            "replacements": replacements,
            "status": str(row["status"] or "") if row is not None else "missing",
        }

    def _handle_no_matching_rebuy_timeout(
        self,
        *,
        op: Any,
        note: dict[str, Any],
        result: Any,
    ) -> bool:
        op_id = int(op["id"])
        started_at = self._rebuy_wait_started_at.get(op_id)
        if started_at is None:
            started_at = _now_utc()
            self._rebuy_wait_started_at[op_id] = started_at
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
            "timeoutReason": timeout_reason,
            "timeoutHours": timeout_hours,
            "steamPriceNow": result.steam_price_now,
            "listingRatioNow": result.listing_ratio_now,
        }
        self.db.update_pool_operation(
            op["id"],
            note=_build_note(updated_note),
        )
        print(
            f"[补仓等待超时] {op['market_hash_name']} | "
            f"原因: no_matching_listing 本次运行持续超过 {timeout_hours:.0f} 小时 | "
            "保持 pending，脚本继续运行并在后续轮次重试"
        )
        return False

    def _notify_skip(self, market_hash_name: str, reason: str, details: Any) -> None:
        if not self.serverchan:
            return
        title_map = {
            "steam_crashed": "[rebuy] steam dropped",
            "ratio_no_longer_profitable": "[rebuy] ratio not profitable",
            "steam_price_unavailable": "[steam] price unavailable",
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

