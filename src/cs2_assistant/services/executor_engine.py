from __future__ import annotations

import json
import math
import random
import re
import time
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
from cs2_assistant.services.executor_buy import execute_rebuy, is_retryable_c5_network_error
from cs2_assistant.services.market import calculate_listing_ratio, calculate_transfer_real_ratio
from cs2_assistant.services.pricing import PricingDecision, fetch_listing_price, summarize_orderbook_prices
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


def _guadao_client_telemetry_callback(**context: Any) -> Any:
    """Bind an explicit guadao source without making logging a dependency."""

    try:
        from cs2_assistant.services.guadao_logging import get_guadao_event_logger

        return get_guadao_event_logger().bind_telemetry(**context)
    except Exception:
        return None


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
REBUY_NO_MATCHING_TIMEOUT_SECONDS = 3 * 60 * 60
REBUY_ORDER_AUDIT_LOOKBACK_DAYS = 7
C5_DELIVERY_DEADLINE_SECONDS = 24 * 60 * 60
C5_DELIVERY_STATUS_KEY = "c5FinalStatus"
C5_DELIVERY_SUCCESS = "c5_success"
C5_DELIVERY_FAILED = "c5_failed"
REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY = "autoReplacementEligible"
STEAM_LISTING_RETRY_DELAY_SECONDS = 3.0
STEAM_LISTING_SUCCESS_DELAY_SECONDS = 0.0
STEAM_LISTING_MAX_ATTEMPTS = 10
STEAM_LISTING_TRANSIENT_COOLDOWN_SECONDS = 30 * 60
STEAM_LISTING_ACCOUNT_ATTEMPT_INTERVAL_SECONDS = 1.0
STEAM_LISTING_ACCOUNT_BACKOFF_SECONDS = 3 * 60
GUADAO_STALE_LISTED_CANCEL_AFTER_SECONDS = 48 * 60 * 60
STEAM_SALE_RECEIPT_FAST_LOOKUP_MAX_PAGES = 2
STEAM_SALE_RECEIPT_DEEP_LOOKUP_MAX_PAGES = 30
STEAM_SALE_RECEIPT_DEEP_LOOKUP_INITIAL_DELAY_SECONDS = 30 * 60
STEAM_SALE_RECEIPT_DEEP_LOOKUP_INTERVAL_SECONDS = 6 * 60 * 60
C5_INVENTORY_REFERENCE_CACHE_MAX_AGE_SECONDS = 180 * 60


class _NewGuadaoActionBlocked(RuntimeError):
    """Internal control-flow signal used before a new real Steam listing."""


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
    if status_name in {
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
            telemetry_callback=_guadao_client_telemetry_callback(**c5_telemetry_context),
            telemetry_context={"source": "guadao", **c5_telemetry_context},
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
    ) -> SteamMarketClient | None:
        expected_steam_id = str(steam_id64 or (account.steam_id64 if account else "") or "").strip() or None
        cached = self._cached_steam_client(account, expected_steam_id)
        if cached is not None:
            return self._ensure_market_client_ready(cached, account)

        if account is None:
            if self._steam_client_matches(expected_steam_id):
                self._cache_steam_client(self.steam_client, self.account)
                return self._ensure_market_client_ready(self.steam_client, self.account)
            print(f"[跳过] steam={expected_steam_id or '-'} 未匹配到 config/accounts.json 中的 Steam 账号。")
            return None
        if not getattr(self, "account_store", None):
            print(f"[跳过] Steam账号 {account.name} 无账号存储上下文，无法创建 Steam client。")
            return None

        refreshed_account = account
        if not refreshed_account.cookies:
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
            )
        except SteamMarketError as exc:
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
                )
                refreshed_account = updated
            except SteamMarketError as relogin_exc:
                print(f"[跳过] Steam账号 {refreshed_account.name} relogin 后仍无法初始化: {relogin_exc}")
                return None

        client = self._ensure_market_client_ready(client, refreshed_account)
        if client is None:
            return None
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
            print("[等待] 检测到挂刀待确认/失败状态，或箱子活跃挂单槽已满，本轮暂停新上架。")
        else:
            case_open_count = self._open_case_guadao_count()
            if case_open_count > 0:
                print(
                    f"[继续] 箱子活跃挂单槽 {case_open_count}/{self._case_max_open_guadao_count()}，"
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

        case_open_count = self._open_case_guadao_count()
        case_has_blocking_pool_status = any(
            status in {POOL_STATUS_LISTING_PENDING, POOL_STATUS_REBUY_FAILED}
            and self._is_weapon_case(market_hash_name)
            for market_hash_name, status in current_status_map.items()
        )
        if case_has_blocking_pool_status:
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
        for op in self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=limit):
            if not self._is_weapon_case(op["market_hash_name"]):
                continue
            quantity = safe_int(op["quantity"]) or 1
            count += max(1, quantity)
        return count

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
        count = self._open_case_guadao_count() if count is None else count
        reached = count > 0 and count >= self._case_max_open_guadao_count()
        if not reached:
            self._case_open_guadao_limit_notified = False
        return reached

    def _notify_case_open_guadao_limit(self, count: int) -> None:
        limit = self._case_max_open_guadao_count()
        message = f"箱子活跃挂单槽已达到 {count}/{limit} 个，已暂停新上架；程序会继续每轮扫描卖出和补仓。"
        if getattr(self, "_case_open_guadao_limit_notified", False):
            return
        self._case_open_guadao_limit_notified = True
        print(f"[提醒] {message}")
        serverchan = getattr(self, "serverchan", None)
        if not serverchan:
            return
        try:
            serverchan.send(
                "[挂刀暂停] 箱子活跃挂单槽已满",
                (
                    f"箱子活跃挂单槽: {count}/{limit}\n"
                    "状态: 已暂停新上架，程序仍会继续扫描卖出和补仓\n"
                    f"处理: 连续满载 {self.config.case_full_release_after_hours:g} 小时后，"
                    f"随机撤销 {self.config.case_full_release_fraction * 100:g}% 的远端活跃挂单"
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
        price_offset = self._listing_price_offset_for_candidate(candidate)
        pricing = fetch_listing_price(
            active_client,
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
                case_open_count = self._open_case_guadao_count()
                if self._case_open_guadao_limit_reached(case_open_count):
                    self._notify_case_open_guadao_limit(case_open_count)
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
                case_open_count = self._open_case_guadao_count()
                if self._case_open_guadao_limit_reached(case_open_count):
                    self._notify_case_open_guadao_limit(case_open_count)
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
                    case_open_count = self._open_case_guadao_count()
                    if self._case_open_guadao_limit_reached(case_open_count):
                        self._notify_case_open_guadao_limit(case_open_count)
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
            (
                sale_receipt_results,
                sale_receipt_deep_attempt_ids,
                sale_receipt_deep_attempted_at,
            ) = self._lookup_steam_sale_receipts_for_operations(
                active_client,
                candidate_ops,
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
            )
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
        note["listingPendingAt"] = utc_now_iso()
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

    def _release_removed_market_pending_listing(self, op: Any, note: dict[str, Any]) -> None:
        asset_id = str(op["asset_id"] or "").strip()
        listing_id = str(note.get("marketPendingListingId") or note.get("listingId") or "").strip()
        note["needsConfirmation"] = False
        note["confirmationStatus"] = "market_pending_removed"
        note["marketPendingRemovedAt"] = note.get("marketPendingRemovedAt") or utc_now_iso()
        self.db.update_pool_operation(op["id"], status="canceled", note=_build_note(note))
        if asset_id:
            self.db.set_asset_status(asset_id, "available")
        if not self._has_other_open_guadao_operation(
            op["market_hash_name"],
            exclude_op_id=int(op["id"]),
        ):
            self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
        print(
            f"[挂单待确认清理] {op['market_hash_name']} | asset={asset_id or '-'} | "
            f"listing={listing_id or '-'} | "
            "Steam 网页待确认挂单已撤下，资产已释放，下轮可重新上架"
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

    def _case_listing_capacity_full_since(self, listed_ops: list[Any]) -> datetime | None:
        capacity = self._case_max_open_guadao_count()
        if capacity <= 0:
            return None
        dated_ops: list[tuple[datetime, Any]] = []
        for op in listed_ops:
            note = _read_note(op["note"])
            slot_started_at = _parse_iso(str(note.get("activeVerifiedAt") or "")) or _parse_iso(
                op["created_at"]
            )
            if slot_started_at is None:
                continue
            if slot_started_at.tzinfo is None:
                slot_started_at = slot_started_at.replace(tzinfo=timezone.utc)
            dated_ops.append((slot_started_at.astimezone(timezone.utc), op))
        dated_ops.sort(key=lambda entry: (entry[0], int(entry[1]["id"])))

        occupied = 0
        for created_at, op in dated_ops:
            occupied += max(1, safe_int(op["quantity"]) or 1)
            if occupied >= capacity:
                return created_at
        return None

    def _release_full_case_listing_capacity(self) -> int:
        listed_ops = self._listed_case_guadao_operations()
        capacity = self._case_max_open_guadao_count()
        occupied = sum(max(1, safe_int(op["quantity"]) or 1) for op in listed_ops)
        if capacity <= 0 or occupied < capacity:
            return 0

        release_after_seconds = self._case_full_release_after_seconds()
        release_fraction = self._case_full_release_fraction()
        if release_after_seconds <= 0 or release_fraction <= 0:
            return 0
        full_since = self._case_listing_capacity_full_since(listed_ops)
        if full_since is None:
            return 0
        now = _now_utc()
        full_seconds = max(0.0, (now - full_since).total_seconds())
        if full_seconds < release_after_seconds:
            return 0

        targets = self._open_guadao_steam_targets()
        if not targets:
            targets = [(str(getattr(self.steam_client, "steam_id64", "") or "") or None, self.account)]

        active_records: list[tuple[Any, dict[str, Any], SteamMarketClient, str]] = []
        seen_operation_ids: set[int] = set()
        for steam_id, account in targets:
            client = self._steam_client_for_account(account, steam_id)
            if client is None:
                continue
            try:
                active = client.list_active_listings()
            except Exception as exc:
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

        if not active_records:
            return 0
        active_records.sort(key=lambda record: int(record[0]["id"]))
        release_count = min(
            len(active_records),
            max(1, int(math.ceil(len(active_records) * release_fraction))),
        )
        selected_records = random.sample(active_records, release_count)
        released = 0
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
            print(
                f"[满载随机释放] {op['market_hash_name']} | asset={asset_id or '-'} | "
                f"listing={listing_id} | 活跃挂单槽连续满载 {full_seconds / 3600.0:.2f} 小时 | "
                f"随机释放比例 {release_fraction * 100:g}% | Steam撤单成功，资产已恢复可上架"
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

    def _stale_listed_recheck_after_seconds(self) -> float:
        try:
            hours = float(self.config.stale_listed_recheck_hours)
        except (TypeError, ValueError):
            return 24.0 * 3600.0
        if not math.isfinite(hours) or hours <= 0:
            return 24.0 * 3600.0
        return hours * 3600.0

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
        if next_check_at is None:
            return True
        if next_check_at.tzinfo is None:
            next_check_at = next_check_at.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc) >= next_check_at.astimezone(timezone.utc)

    def _current_inventory_reference_price(self, market_hash_name: str) -> float | None:
        cache_path = self.settings.db_path.parent / "c5_inventory_all_cache.json"
        try:
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            cached_payload = None
        if isinstance(cached_payload, dict):
            cached_at = _parse_iso(str(cached_payload.get("cachedAt") or ""))
            if cached_at is not None:
                if cached_at.tzinfo is None:
                    cached_at = cached_at.replace(tzinfo=timezone.utc)
                cache_age = (_now_utc() - cached_at.astimezone(timezone.utc)).total_seconds()
            else:
                cache_age = None
            if cache_age is None or cache_age <= C5_INVENTORY_REFERENCE_CACHE_MAX_AGE_SECONDS:
                for summary in summarize_inventory_types(list(cached_payload.get("list") or [])):
                    if str(summary.get("market_hash_name") or "") != market_hash_name:
                        continue
                    price = safe_float(summary.get("reference_price"))
                    if price is not None and price > 0:
                        return price

        summaries = summarize_inventory_types(list(self._last_inventory_payload.get("list") or []))
        for summary in summaries:
            if str(summary.get("market_hash_name") or "") != market_hash_name:
                continue
            price = safe_float(summary.get("reference_price"))
            return price if price is not None and price > 0 else None
        return None

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

        c5_price = self._current_inventory_reference_price(market_hash_name)
        try:
            payload = client.order_book(
                app_id=self.settings.app_id,
                market_hash_name=market_hash_name,
            )
        except Exception as exc:
            result = (None, c5_price, f"Steam orderbook unavailable: {exc}")
            cache[market_hash_name] = result
            return result

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        actual_currency = safe_int(data.get("eCurrency"))
        if actual_currency is not None and actual_currency != int(self.config.steam_currency):
            result = (
                None,
                c5_price,
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
        elif c5_price is None:
            error = "current C5 inventory reference price is unavailable"
        result = (floor_price, c5_price, error)
        cache[market_hash_name] = result
        return result

    def _keep_stale_active_listing_if_still_competitive(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        client: SteamMarketClient,
        now: datetime,
        market_snapshot_cache: dict[str, tuple[float | None, float | None, str | None]],
    ) -> bool:
        if not self._stale_listed_recheck_due(note, now=now):
            return True

        market_hash_name = str(op["market_hash_name"])
        floor_price, c5_price, snapshot_error = self._stale_listed_market_snapshot(
            market_hash_name,
            client=client,
            cache=market_snapshot_cache,
        )
        checked_at = now.astimezone(timezone.utc)
        next_check_at = checked_at + timedelta(seconds=self._stale_listed_recheck_after_seconds())
        note["staleListedCheckedAt"] = checked_at.isoformat()
        note["staleListedNextCheckAt"] = next_check_at.isoformat()
        note["staleListedCurrentFloorPrice"] = floor_price
        note["staleListedCurrentC5Price"] = c5_price

        if snapshot_error:
            note["staleListedCleanupStatus"] = "check_deferred"
            note["staleListedCleanupReason"] = snapshot_error
            self.db.update_pool_operation(op["id"], note=_build_note(note))
            print(
                f"[挂刀老挂单复查延后] {market_hash_name} | "
                f"无法安全读取当前最低价/补仓价，保留挂单，下次复查 {next_check_at.isoformat()} | "
                f"原因: {snapshot_error}"
            )
            return True

        list_price = safe_float(note.get("steamListPrice")) or safe_float(op["expected_price"])
        steam_net_factor = safe_float(note.get("steamNetFactorAtOpen"))
        if steam_net_factor is None or steam_net_factor <= 0:
            steam_net_factor = float(self.config.steam_net_factor)
        hard_max_ratio = safe_float(note.get("guadaoMaxListingRatioAtOpen"))
        if hard_max_ratio is None or hard_max_ratio <= 0:
            hard_max_ratio = float(self.config.guadao_max_listing_ratio)
        allowed_ratio = hard_max_ratio + self._stale_listed_ratio_tolerance()
        steam_after_tax = (
            list_price * steam_net_factor
            if list_price is not None and list_price > 0 and steam_net_factor > 0
            else None
        )
        current_ratio = (
            c5_price / steam_after_tax
            if c5_price is not None and steam_after_tax is not None and steam_after_tax > 0
            else None
        )
        note["staleListedCurrentRatio"] = current_ratio
        note["staleListedAllowedMaxRatio"] = allowed_ratio
        note["staleListedRatioTolerancePct"] = self.config.stale_listed_max_ratio_tolerance_pct

        if list_price is None or floor_price is None or current_ratio is None:
            note["staleListedCleanupStatus"] = "check_deferred"
            note["staleListedCleanupReason"] = "listing price or ratio is unavailable"
            self.db.update_pool_operation(op["id"], note=_build_note(note))
            return True

        # 本单价格低于盘口第一档时同样位于最前面，不能因为盘口短时未包含本单而误撤。
        is_at_market_floor = list_price <= floor_price + 0.005
        note["staleListedAtMarketFloor"] = is_at_market_floor
        if is_at_market_floor and current_ratio <= allowed_ratio:
            note["staleListedCleanupStatus"] = "kept_at_market_floor"
            note["staleListedCleanupReason"] = "still at market floor and ratio remains acceptable"
            self.db.update_pool_operation(op["id"], note=_build_note(note))
            print(
                f"[挂刀老挂单继续等待] {market_hash_name} | 挂价 {list_price:.2f} | "
                f"当前最低价 {floor_price:.2f} | C5价 {c5_price:.2f} | "
                f"挂刀比例 {_format_pct(current_ratio)} <= 允许上限 {_format_pct(allowed_ratio)} | "
                f"下次复查 {next_check_at.isoformat()}"
            )
            return True

        if not is_at_market_floor:
            note["staleListedRemoveReason"] = "listed more than 48 hours and no longer at market floor"
        else:
            note["staleListedRemoveReason"] = "stale listing ratio exceeds tolerated maximum"
        return False

    def _remove_stale_active_guadao_listing(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        client: SteamMarketClient,
        active: list[Any],
        active_listing_ids: set[str],
    ) -> bool:
        asset_id = str(op["asset_id"] or "").strip()
        listing_id = str(note.get("listingId") or "").strip()
        removable_listing_id = listing_id if listing_id and listing_id in active_listing_ids else None
        if removable_listing_id is None and asset_id:
            removable_listing_id = self._active_listing_id_for_asset(active, asset_id)
            if removable_listing_id:
                note["listingId"] = removable_listing_id
        if not removable_listing_id:
            note["staleListedCleanupStatus"] = "manual_required"
            note["staleListedCleanupReason"] = "active listing matched asset but listing id is unavailable"
            note["staleListedCheckedAt"] = utc_now_iso()
            self.db.update_pool_operation(op["id"], note=_build_note(note))
            print(
                f"[挂刀老挂单待处理] {op['market_hash_name']} | asset={asset_id or '-'} | "
                "远端仍在售但缺少可撤单 listingId，未恢复本地资产"
            )
            return False

        remover = getattr(client, "remove_listing", None)
        if not callable(remover):
            remove_error = "Steam client does not support remove_listing"
            removed = False
        else:
            try:
                removed = bool(remover(removable_listing_id))
                remove_error = None if removed else "Steam remove_listing returned false"
            except Exception as exc:
                removed = False
                remove_error = str(exc)

        if not removed:
            note["staleListedCleanupStatus"] = "remove_failed"
            note["staleListedCleanupReason"] = remove_error
            note["staleListedCheckedAt"] = utc_now_iso()
            self.db.update_pool_operation(op["id"], note=_build_note(note))
            print(
                f"[挂刀老挂单撤单失败] {op['market_hash_name']} | asset={asset_id or '-'} | "
                f"listing={removable_listing_id} | 原因: {remove_error}"
            )
            return False

        note["staleListedCleanupStatus"] = "removed"
        note["staleListedRemovedAt"] = utc_now_iso()
        note["staleListedRemoveReason"] = note.get("staleListedRemoveReason") or "listed more than 48 hours"
        self.db.update_pool_operation(op["id"], status="canceled", note=_build_note(note))
        if asset_id:
            self.db.set_asset_status(asset_id, "available")
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

    def _mark_stale_listed_manual_required(self, op: Any, note: dict[str, Any], *, reason: str) -> None:
        asset_id = str(op["asset_id"] or "").strip()
        listing_id = str(note.get("listingId") or "").strip()
        note["staleListedCleanupStatus"] = "manual_required"
        note["staleListedManualRequiredAt"] = utc_now_iso()
        note["staleListedManualRequiredReason"] = reason
        note["manualReviewReason"] = reason
        self.db.update_pool_operation(op["id"], status="manual_required", note=_build_note(note))
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_LISTED)
        print(
            f"[挂刀老挂单人工检查] {op['market_hash_name']} | asset={asset_id or '-'} | "
            f"listing={listing_id or '-'} | 已超过48小时，远端不在售且无Steam卖出回执；未恢复本地资产"
        )

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
            note["confirmationStatus"] = "confirm_sent_waiting_active_listing"
            note["confirmationMessage"] = f"confirmed but active listing check failed: {exc}"
            self._pending_confirmation_count += 1
            return note, POOL_STATUS_LISTING_PENDING
        if not self._listing_is_active(
            active_listing_ids=active_listing_ids,
            active_asset_ids=active_asset_ids,
            listing_id=listing_id,
            asset_id=asset_id,
        ):
            note["confirmationStatus"] = "confirm_sent_waiting_active_listing"
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
    ) -> tuple[dict[int, dict[str, Any] | None], set[int], str | None]:
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
            return {}, set(), None

        max_pages = (
            STEAM_SALE_RECEIPT_DEEP_LOOKUP_MAX_PAGES
            if deep_due_ids
            else STEAM_SALE_RECEIPT_FAST_LOOKUP_MAX_PAGES
        )
        results: dict[int, dict[str, Any] | None] = {
            int(op["id"]): None for op, _ in missing
        }
        batch_finder = getattr(client, "find_sale_receipts_for_targets", None)
        try:
            if callable(batch_finder):
                targets = [
                    {
                        "key": str(int(op["id"])),
                        "listingId": str(note.get("listingId") or "").strip(),
                        "assetId": str(op["asset_id"] or "").strip(),
                    }
                    for op, note in missing
                ]
                batch_results = batch_finder(targets, max_pages=max_pages)
                if isinstance(batch_results, dict):
                    for operation_id in results:
                        receipt = batch_results.get(str(operation_id))
                        if isinstance(receipt, dict):
                            results[operation_id] = receipt
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
        except Exception:
            return results, set(), None
        attempted_at = checked_at.astimezone(timezone.utc).isoformat()
        return results, deep_due_ids, attempted_at if deep_due_ids else None

    @staticmethod
    def _record_sale_receipt_deep_attempt(
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

        listed_ops = [
            op
            for op in self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="listed", limit=200)
            if self._operation_matches_client(op, active_client)
            and (operation_ids is None or int(op["id"]) in operation_ids)
        ]
        if sale_receipt_results is None:
            (
                sale_receipt_results,
                sale_receipt_deep_attempt_ids,
                sale_receipt_deep_attempted_at,
            ) = self._lookup_steam_sale_receipts_for_operations(
                active_client,
                listed_ops,
                active_listing_ids=active_listing_ids,
                active_asset_ids=active_asset_ids,
                now=now,
            )
        deep_attempt_ids = set(sale_receipt_deep_attempt_ids or set())
        for op in listed_ops:
            pool_status = pool_status_map.get(op["market_hash_name"], POOL_STATUS_HOLDING)
            if pool_status == POOL_STATUS_LISTING_PENDING:
                continue
            note = _read_note(op["note"])
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
                if is_stale_listed:
                    if self._keep_stale_active_listing_if_still_competitive(
                        op,
                        note,
                        client=active_client,
                        now=now,
                        market_snapshot_cache=stale_market_snapshot_cache,
                    ):
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

    def _rebuy_delivery_deadline(self, op: Any, note: dict[str, Any]) -> datetime | None:
        # The 24-hour delivery clock starts only after a real C5 order was
        # submitted.  Local operation timestamps describe our state machine,
        # not C5's seller-delivery obligation, and must never start this clock.
        submitted = _parse_iso(str(note.get("c5OrderSubmittedAt") or ""))
        if submitted is None:
            return None
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        return submitted.astimezone(timezone.utc) + timedelta(seconds=C5_DELIVERY_DEADLINE_SECONDS)

    def _expire_rebuy_delivery_if_due(
        self,
        op: Any,
        note: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> bool:
        deadline = self._rebuy_delivery_deadline(op, note)
        current = now or _now_utc()
        if deadline is None or current < deadline:
            return False
        failed_note = {
            **note,
            C5_DELIVERY_STATUS_KEY: C5_DELIVERY_FAILED,
            "c5OrderInvalidated": True,
            "c5OrderFailedCode": "delivery_timeout_24h",
            "c5OrderFailedDesc": "C5 补仓下单后 24 小时仍未发货，按补仓失败处理",
            "c5DeliveryDeadlineAt": deadline.isoformat(),
            "c5DeliveryTimedOutAt": current.isoformat(),
            REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY: True,
        }
        self.db.update_pool_operation(
            op["id"],
            status=C5_DELIVERY_FAILED,
            note=_build_note(failed_note),
        )
        self._emit_guadao_local_event(
            operation="c5_rebuy_delivery_timeout_24h",
            message="C5 补仓满 24 小时仍未发货，已按失败处理",
            level="ERROR",
            market_hash_name=str(op["market_hash_name"]),
            operation_id=int(op["id"]),
            asset_id=str(op["asset_id"] or "") or None,
            note=failed_note,
            context={
                "state": C5_DELIVERY_FAILED,
                "c5OrderId": failed_note.get("c5OrderId"),
                "c5OutTradeNo": failed_note.get("c5OutTradeNo"),
                "c5ActualPrice": safe_float(op["actual_price"]),
                "c5ExpectedPrice": safe_float(op["expected_price"]),
                "deliveryDeadlineAt": deadline.isoformat(),
                "timedOutAt": current.isoformat(),
                "maxRebuyRatioAtOpen": failed_note.get("maxRebuyRatioAtOpen"),
            },
        )
        self._create_replacement_rebuy_for_failed_op(
            self._get_pool_operation_by_id(int(op["id"])) or op,
            failed_note,
            replacement_reason="c5_delivery_failed",
            failed_status=C5_DELIVERY_FAILED,
            created_by="c5_delivery_timeout_24h",
        )
        return True

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
        if note.get(C5_DELIVERY_STATUS_KEY) == C5_DELIVERY_SUCCESS:
            return 0
        if self._has_replacement_child(note):
            return 0

        order_id = str(note.get("c5OrderId") or "").strip()
        failed_reason = (
            note.get("c5OrderFailedDesc")
            or note.get("failedReason")
            or note.get("c5OrderStatusName")
            or "rebuy_failed"
        )
        if replacement_reason is None:
            replacement_reason = (
                "c5_delivery_failed"
                if note.get(C5_DELIVERY_STATUS_KEY) == C5_DELIVERY_FAILED
                else "rebuy_operation_failed"
            )
        if failed_status is None:
            failed_status = C5_DELIVERY_FAILED if replacement_reason == "c5_delivery_failed" else "failed"
        if force_rebuy_replacement is None:
            force_rebuy_replacement = False
        expected_price = safe_float(op["actual_price"]) or safe_float(op["expected_price"]) or 0.01
        replacement_note = {
            "replacementForRebuyOperationId": int(op["id"]),
            "replacementForC5OrderId": order_id,
            "replacementReason": replacement_reason,
            "replacementFailedCode": note.get("c5OrderFailedCode"),
            "replacementFailedDesc": failed_reason,
            "forceRebuyReplacement": bool(force_rebuy_replacement),
            "sourceSellOperationId": note.get("sourceSellOperationId"),
            "sourceListing": note.get("sourceListing"),
            "steamListPrice": note.get("steamListPrice"),
            "listingRatioAtOpen": note.get("listingRatioAtOpen"),
            "maxRebuyRatioAtOpen": note.get("maxRebuyRatioAtOpen"),
            "guadaoMaxListingRatioAtOpen": note.get("guadaoMaxListingRatioAtOpen"),
            "steamNetFactorAtOpen": note.get("steamNetFactorAtOpen"),
            "guadaoRatioRuleSource": note.get("guadaoRatioRuleSource"),
            "guadaoRatioRuleId": note.get("guadaoRatioRuleId"),
            "guadaoRatioRuleVersion": note.get("guadaoRatioRuleVersion"),
            "steamAccountId": note.get("steamAccountId"),
            "steamAccountName": note.get("steamAccountName"),
            "steamId64": note.get("steamId64"),
            "createdBy": created_by,
            **(
                {
                    "replacementMaxPrice": expected_price,
                    "replacementPricePolicy": "original_failed_order_price",
                }
                if replacement_reason == "c5_delivery_failed"
                else {}
            ),
        }
        replacement_id = self.db.add_pool_operation(
            market_hash_name=op["market_hash_name"],
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            quantity=1,
            expected_price=expected_price,
            note=_build_note(replacement_note),
        )
        self.db.update_pool_operation(
            op["id"],
            status=failed_status,
            note=_build_note(
                {
                    **note,
                    "replacementRebuyOperationId": replacement_id,
                    "replacementReason": replacement_reason,
                    REBUY_AUTO_REPLACEMENT_ELIGIBLE_KEY: True,
                    **(
                        {C5_DELIVERY_STATUS_KEY: C5_DELIVERY_FAILED}
                        if replacement_reason == "c5_delivery_failed"
                        else {}
                    ),
                }
            ),
        )
        self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
        print(
            f"[补仓失效] {op['market_hash_name']} | 原补仓 op={op['id']} 已失败，"
            f"已创建替换补仓 op={replacement_id} | 原因: {failed_reason}"
        )
        self._emit_guadao_local_event(
            operation="c5_rebuy_replacement_created",
            message="C5 失败补仓已创建替换补仓任务",
            level="WARNING",
            market_hash_name=str(op["market_hash_name"]),
            operation_id=int(op["id"]),
            asset_id=str(op["asset_id"] or "") or None,
            note=note,
            context={
                "state": "replacement_pending",
                "replacementOperationId": replacement_id,
                "replacementReason": replacement_reason,
                "failedReason": failed_reason,
                "originalC5OrderId": order_id or None,
                "replacementMaxPrice": (
                    expected_price if replacement_reason == "c5_delivery_failed" else None
                ),
                "maxRebuyRatioAtOpen": note.get("maxRebuyRatioAtOpen"),
                "steamNetFactorAtOpen": note.get("steamNetFactorAtOpen"),
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
            statuses=["delivery_pending", "completed"],
            limit=5000,
        )
        if operation_id is not None:
            delivery_candidates = [
                op for op in delivery_candidates if int(op["id"]) == int(operation_id)
            ]
        for op in delivery_candidates:
            is_delivery_pending = str(op["status"] or "") == "delivery_pending"
            if not is_delivery_pending and not self._op_is_within_rebuy_audit_window(op, cutoff):
                continue
            note = _read_note(op["note"])
            if note.get(C5_DELIVERY_STATUS_KEY) in {C5_DELIVERY_SUCCESS, C5_DELIVERY_FAILED}:
                continue
            # C5 requires delivery within 24 hours. Once the persisted deadline
            # has passed, the local terminal decision no longer depends on a
            # successful detail lookup: network errors, mismatched responses,
            # and old audit age must not leave this operation pending forever.
            if is_delivery_pending and self._expire_rebuy_delivery_if_due(op, note):
                failures += 1
                replacements += 1
                continue
            if not _c5_order_lookup_ids(note):
                continue

            checked += 1
            try:
                detail, order_id, note = self._fetch_c5_buyer_order_detail(op, note)
            except Exception as exc:
                print(f"[警告] 复查 C5 补仓订单失败: op={op['id']} | {exc}")
                continue
            if detail is None:
                if self._expire_rebuy_delivery_if_due(op, note):
                    failures += 1
                    replacements += 1
                continue

            final_status = _c5_delivery_final_status(detail)
            checked_note = {
                **note,
                "c5OrderStatus": safe_int(detail.get("status")),
                "c5OrderStatusName": detail.get("statusName"),
                "c5OrderCheckedAt": utc_now_iso(),
            }
            if final_status is None:
                deadline = self._rebuy_delivery_deadline(op, checked_note)
                checked_note["c5DeliveryDeadlineAt"] = deadline.isoformat() if deadline else None
                self.db.update_pool_operation(op["id"], note=_build_note(checked_note))
                if self._expire_rebuy_delivery_if_due(op, checked_note):
                    failures += 1
                    replacements += 1
                continue

            detail_market_hash_name = _c5_order_detail_market_hash_name(detail)
            if detail_market_hash_name and detail_market_hash_name != op["market_hash_name"]:
                print(
                    f"[警告] C5 补仓订单品类不匹配，已跳过自动补仓: "
                    f"op={op['id']} 本地={op['market_hash_name']} C5={detail_market_hash_name}"
                )
                continue

            if final_status == C5_DELIVERY_SUCCESS:
                self.db.update_pool_operation(
                    op["id"],
                    status="completed",
                    note=_build_note(
                        {
                            **checked_note,
                            C5_DELIVERY_STATUS_KEY: C5_DELIVERY_SUCCESS,
                            "c5OrderInvalidated": False,
                        }
                    ),
                )
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_HOLDING)
                completed_note = {
                    **checked_note,
                    C5_DELIVERY_STATUS_KEY: C5_DELIVERY_SUCCESS,
                    "c5OrderInvalidated": False,
                }
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
                        "c5OrderId": order_id or checked_note.get("c5OrderId"),
                        "c5OutTradeNo": checked_note.get("c5OutTradeNo"),
                        "deliveryStatus": C5_DELIVERY_SUCCESS,
                    },
                )
                successes += 1
                continue

            failed_note = {
                **checked_note,
                C5_DELIVERY_STATUS_KEY: C5_DELIVERY_FAILED,
                "c5OrderFailedCode": detail.get("failedCode"),
                "c5OrderFailedDesc": detail.get("failedDesc"),
                "c5OrderInvalidated": True,
            }
            self.db.update_pool_operation(op["id"], status=C5_DELIVERY_FAILED, note=_build_note(failed_note))
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
                    "c5ActualPrice": safe_float(op["actual_price"]),
                    "c5ExpectedPrice": safe_float(op["expected_price"]),
                    "c5OrderId": order_id or failed_note.get("c5OrderId"),
                    "failedCode": failed_note.get("c5OrderFailedCode"),
                    "failedReason": failed_note.get("c5OrderFailedDesc"),
                },
            )
            failures += 1

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
                max_price_override=replacement_max_price,
            )
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
                        float(result.steam_reference_price) * float(rebuy_steam_net_factor)
                        if result.steam_reference_price is not None
                        else None
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
                            "c5OrderStatus": "ordered",
                            C5_DELIVERY_STATUS_KEY: "pending",
                            "c5OrderSubmittedAt": submitted_at.isoformat(),
                            "c5DeliveryDeadlineAt": delivery_deadline.isoformat(),
                            "c5OrderPayload": c5_payload,
                        }
                    ),
                )
                self.db.set_pool_status(op["market_hash_name"], POOL_STATUS_PENDING_REBUY)
                prefix = "[补仓替换]" if is_replacement else "[补仓]"
                print(
                    f"{prefix} {op['market_hash_name']} | "
                    f"账号={_steam_account_log_label(note) or '-'} | "
                    f"C5买入 CNY {_format_decimal(result.actual_price)}"
                )
                self._emit_guadao_local_event(
                    operation="c5_rebuy_submitted",
                    message="C5 补仓已提交，等待发货确认",
                    market_hash_name=str(op["market_hash_name"]),
                    operation_id=int(op["id"]),
                    asset_id=str(op["asset_id"] or "") or None,
                    note=note,
                    context={
                        "state": "delivery_pending",
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
        )
        self._refresh_scan_listing_prices_from_steam(report)
        status_map = self.db.get_pool_status_map()
        listed = 0
        if not self._has_open_guadao_cycle(status_map):
            listed = self._execute_guadao_listings(report, status_map)
        self._release_full_case_listing_capacity()
        return {
            "ok": True,
            "listed": listed,
            "evaluated": len(getattr(report, "all_evaluated", []) or []),
            "candidateCount": len(getattr(report, "guadao_candidates", []) or []),
            "generatedAt": getattr(report, "generated_at", utc_now_iso()),
        }

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
            }
        account = self._account_by_id(account_id) if account_id else self.account
        steam_id = str(account.steam_id64 or "").strip() if account else None
        client = self._steam_client_for_account(account, steam_id)
        if client is None:
            return {"ok": False, "sold": 0, "error": "steam client unavailable"}
        self._steam_market_validated_accounts = set()
        try:
            active_listings = client.list_active_listings()
        except Exception as exc:
            return {"ok": False, "sold": 0, "error": str(exc)}
        active_listing_ids, active_asset_ids = self._active_listing_identity_sets(active_listings)
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
        (
            sale_receipt_results,
            deep_attempt_ids,
            deep_attempted_at,
        ) = self._lookup_steam_sale_receipts_for_operations(
            client,
            due_operations,
            active_listing_ids=active_listing_ids,
            active_asset_ids=active_asset_ids,
        )
        confirmed = self._refresh_pending_listing_confirmations(
            client=client,
            active_listings=active_listings,
            operation_ids=confirmation_operation_ids,
            sale_receipt_results=sale_receipt_results,
            sale_receipt_deep_attempt_ids=deep_attempt_ids,
            sale_receipt_deep_attempted_at=deep_attempted_at,
        )
        backfill_operation_ids = None
        if confirmation_operation_ids is not None or sale_operation_ids is not None:
            backfill_operation_ids = set(confirmation_operation_ids or set()) | set(
                sale_operation_ids or set()
            )
        backfilled = self._backfill_listing_ids(
            client=client,
            active_listings=active_listings,
            operation_ids=backfill_operation_ids,
        )
        sold = self._refresh_listings(
            client=client,
            active_listings=active_listings,
            operation_ids=sale_operation_ids,
            sale_receipt_results=sale_receipt_results,
            sale_receipt_deep_attempt_ids=deep_attempt_ids,
            sale_receipt_deep_attempted_at=deep_attempted_at,
        )
        return {
            "ok": True,
            "accountId": account.id if account else account_id,
            "steamId": steam_id,
            "confirmed": confirmed,
            "backfilled": backfilled,
            "sold": sold,
        }

    def run_guadao_rebuy_task(self, operation_id: int) -> dict[str, Any]:
        count = self._execute_rebuys(operation_id=operation_id)
        row = self._get_pool_operation_by_id(int(operation_id))
        return {
            "ok": row is not None,
            "operationId": int(operation_id),
            "rebought": count,
            "status": str(row["status"] or "") if row is not None else "missing",
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

