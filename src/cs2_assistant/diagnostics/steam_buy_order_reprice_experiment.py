from __future__ import annotations

import argparse
import json
import re
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests

from cs2_assistant.accounts import Account, AccountStore
from cs2_assistant.clients.steam_market import SteamMarketClient, SteamMarketError
from cs2_assistant.config import PROJECT_ROOT, load_settings
from cs2_assistant.services.steam_request_scheduler import (
    DirectSteamRequestScheduler,
    get_shared_steam_scheduler,
)


APP_ID = 730
CURRENCY_ID = 23
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "steam_buy_order_reprice_test"
DEFAULT_MAX_PRICE_CENTS = 100

_SENSITIVE_KEY_PARTS = (
    "cookie",
    "password",
    "secret",
    "session",
    "token",
    "trade_url",
    "authorization",
)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(sessionid|cookie|authorization|password|api[-_ ]?key|"
    r"identity[-_ ]?secret|device[-_ ]?secret|shared[-_ ]?secret|token)"
    r"(\s*[=:]\s*)([^&;\s,}\]]+|\"[^\"]*\"|'[^']*')"
)


def utc_iso(timestamp: float | None = None) -> str:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def beijing_iso(timestamp: float | None = None) -> str:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, BEIJING).isoformat()


def _safe_text(value: Any, *, limit: int = 1000) -> str:
    text = _SENSITIVE_TEXT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        str(value or ""),
    )
    return text if len(text) <= limit else f"{text[:limit]}...<truncated>"


def _assert_public_payload(value: Any, *, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(f"sensitive output field refused: {path}.{key}")
            _assert_public_payload(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_public_payload(child, path=f"{path}[{index}]")


def buy_order_id(payload: dict[str, Any]) -> str:
    for key in ("buy_orderid", "buy_order_id", "buyOrderId", "orderid", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def buy_order_name(row: dict[str, Any]) -> str:
    description = row.get("description") if isinstance(row.get("description"), dict) else {}
    for value in (
        row.get("market_hash_name"),
        row.get("marketHashName"),
        row.get("hash_name"),
        description.get("market_hash_name"),
        description.get("marketHashName"),
        row.get("name"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _parse_price_cents(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return int(round(value * 100.0))
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return int(round(float(match.group(0)) * 100.0))
    except ValueError:
        return None


def buy_order_price_cents(row: dict[str, Any]) -> tuple[int | None, str | None]:
    for key in ("price_total", "priceTotal", "price", "amount", "unit_price"):
        if key not in row:
            continue
        parsed = _parse_price_cents(row.get(key))
        if parsed is not None:
            return parsed, key
    return None, None


def buy_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("buy_orders") or payload.get("buyOrders") or []
    iterable: Iterable[Any] = rows.values() if isinstance(rows, dict) else rows
    return [row for row in iterable if isinstance(row, dict)]


def buy_order_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    price_cents, price_source = buy_order_price_cents(row)
    result = {
        "buyOrderId": buy_order_id(row) or None,
        "marketHashName": buy_order_name(row) or None,
        "priceCents": price_cents,
        "priceSource": price_source,
        "quantity": row.get("quantity"),
        "quantityRemaining": row.get("quantity_remaining", row.get("quantityRemaining")),
    }
    _assert_public_payload(result)
    return result


def candidate_order_snapshots(
    payload: dict[str, Any], market_hash_name: str
) -> list[dict[str, Any]]:
    snapshots = [
        buy_order_snapshot(row)
        for row in buy_orders(payload)
        if buy_order_name(row) == market_hash_name
    ]
    return sorted(snapshots, key=lambda row: str(row.get("buyOrderId") or ""))


def classify_transition(
    *,
    original_order_id: str,
    original_price_cents: int,
    requested_price_cents: int,
    orders_after: Iterable[dict[str, Any]],
) -> str:
    rows = list(orders_after)
    original = next(
        (row for row in rows if str(row.get("buyOrderId") or "") == original_order_id),
        None,
    )
    other_rows = [
        row for row in rows if str(row.get("buyOrderId") or "") != original_order_id
    ]
    if original is not None and not other_rows:
        price = original.get("priceCents")
        if price == requested_price_cents:
            return "same_id_price_changed"
        if price == original_price_cents:
            return "original_order_unchanged"
        if price is None:
            return "same_id_price_unknown"
        return "same_id_unexpected_price"
    if original is not None and other_rows:
        if any(row.get("priceCents") == requested_price_cents for row in other_rows):
            return "second_order_created"
        return "original_and_additional_orders_ambiguous"
    if original is None and len(rows) == 1:
        if rows[0].get("priceCents") == requested_price_cents:
            return "old_order_replaced_with_new_id"
        return "old_order_missing_single_unexpected_order"
    if not rows:
        return "original_order_disappeared"
    return "multiple_new_orders_ambiguous"


def build_create_buy_order_data(
    *,
    session_id: str,
    market_hash_name: str,
    price_total: int,
    buy_order_id_value: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "sessionid": session_id,
        "currency": CURRENCY_ID,
        "appid": APP_ID,
        "market_hash_name": market_hash_name,
        "price_total": int(price_total),
        "tradefee_tax": 0,
        "quantity": 1,
        "first_name": "",
        "last_name": "",
        "billing_address": "",
        "billing_address_two": "",
        "billing_country": "CN",
        "billing_city": "",
        "billing_state": "",
        "billing_postal_code": "",
        "confirmation": "0",
        "save_my_address": "0",
    }
    if buy_order_id_value:
        data["buy_orderid"] = str(buy_order_id_value)
    return data


def public_response_payload(
    payload: dict[str, Any] | None,
    *,
    http_status: int | None = None,
    kind: str,
    fallback_message: str | None = None,
) -> dict[str, Any]:
    source = payload or {}
    result = {
        "kind": kind,
        "httpStatus": http_status,
        "success": source.get("success"),
        "message": _safe_text(source.get("message") or source.get("error") or fallback_message),
        "buyOrderId": buy_order_id(source) or None,
        "needConfirmation": bool(source.get("need_confirmation")),
    }
    _assert_public_payload(result)
    return result


def orderbook_min_sell_cents(payload: dict[str, Any]) -> int | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = data.get("rgCompactSellOrders") or []
    if isinstance(rows, list) and rows:
        first = rows[0]
        value = first[0] if isinstance(first, list) and first else first
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    try:
        value = data.get("amtMinSellOrder")
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class ProbeResult:
    response: dict[str, Any]
    orders_after: list[dict[str, Any]]
    classification: str


class SteamBuyOrderRepriceExperiment:
    def __init__(
        self,
        *,
        output_dir: Path,
        account_name: str,
        market_hash_name: str,
        initial_price_cents: int,
        higher_price_cents: int,
        execute: bool = False,
        probe_explicit_id: bool = True,
        max_price_cents: int = DEFAULT_MAX_PRICE_CENTS,
    ) -> None:
        if initial_price_cents <= 0:
            raise ValueError("initial price must be positive")
        if higher_price_cents <= initial_price_cents:
            raise ValueError("higher price must be greater than initial price")
        if higher_price_cents > max_price_cents:
            raise ValueError(
                f"higher price exceeds the {max_price_cents}-cent experiment cap"
            )
        if not market_hash_name.strip():
            raise ValueError("market hash name is required")
        self.settings = load_settings()
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.account_name = account_name
        self.market_hash_name = market_hash_name.strip()
        self.initial_price_cents = int(initial_price_cents)
        self.higher_price_cents = int(higher_price_cents)
        self.execute = bool(execute)
        self.probe_explicit_id = bool(probe_explicit_id)
        self.max_price_cents = int(max_price_cents)
        self.started_at = time.time()
        self.client: SteamMarketClient | None = None
        self.account: Account | None = None
        self.original_order_id: str | None = None
        self.observed_experiment_order_ids: set[str] = set()
        self.errors: list[str] = []
        self.cleanup_errors: list[str] = []
        self.summary: dict[str, Any] = {
            "status": "planned" if not execute else "running",
            "executed": bool(execute),
            "account": account_name,
            "marketHashName": self.market_hash_name,
            "initialPriceCents": self.initial_price_cents,
            "higherPriceCents": self.higher_price_cents,
            "probeExplicitId": self.probe_explicit_id,
            "startedAt": utc_iso(self.started_at),
        }

    @property
    def events_path(self) -> Path:
        return self.output_dir / "events.jsonl"

    def event(self, kind: str, **fields: Any) -> None:
        payload = {
            "utc": utc_iso(),
            "beijing": beijing_iso(),
            "event": kind,
            **fields,
        }
        _assert_public_payload(payload)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _write_state(self) -> None:
        public = dict(self.summary)
        public["observedExperimentOrderIds"] = sorted(self.observed_experiment_order_ids)
        public["errors"] = list(self.errors)
        public["cleanupErrors"] = list(self.cleanup_errors)
        _assert_public_payload(public)
        (self.output_dir / "summary.json").write_text(
            json.dumps(public, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_report(self) -> None:
        summary = self.summary
        ordinary = summary.get("ordinaryDuplicate") or {}
        explicit = summary.get("explicitIdProbe") or {}
        cleanup = summary.get("cleanup") or {}
        lines = [
            "# Steam 求购原单改价接口实测",
            "",
            f"- 状态：`{summary.get('status')}`",
            f"- 账号：`{summary.get('account')}`",
            f"- 物品：`{summary.get('marketHashName')}`",
            f"- 初始价：`{summary.get('initialPriceCents')} CNY 分`",
            f"- 提高价：`{summary.get('higherPriceCents')} CNY 分`",
            f"- 普通重复 createbuyorder：`{ordinary.get('classification', '未执行')}`",
            f"- createbuyorder + 旧 buy_orderid：`{explicit.get('classification', '未执行')}`",
            f"- 最终判断：`{summary.get('conclusion', '尚未形成')}`",
            f"- 清理确认：`{cleanup.get('confirmedAbsent', False)}`",
            "",
            "## 证据边界",
            "",
            "本报告只根据 createbuyorder 响应、mylistings 中的远端订单 ID/价格/数量、"
            "官方 market history 与钱包快照判断；不把 orderbook 当作订单终态证据。",
        ]
        if self.errors or self.cleanup_errors:
            lines.extend(["", "## 错误", ""])
            lines.extend(f"- {message}" for message in [*self.errors, *self.cleanup_errors])
        (self.output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _assert_environment_stopped(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", 8765)) == 0:
                raise RuntimeError("port 8765 is listening; stop the backend before this experiment")
        scheduler = get_shared_steam_scheduler()
        if not isinstance(scheduler, DirectSteamRequestScheduler):
            raise RuntimeError("shared Steam scheduler is configured; run the experiment standalone")
        con = sqlite3.connect(self.settings.db_path)
        con.row_factory = sqlite3.Row
        try:
            enabled = con.execute(
                "SELECT executor_key FROM executor_runtime_state WHERE enabled = 1"
            ).fetchall()
            if enabled:
                raise RuntimeError(
                    "executor runtime flags are enabled: "
                    + ", ".join(str(row["executor_key"]) for row in enabled)
                )
            risky = con.execute(
                """
                SELECT id, status
                FROM profit_trades
                WHERE status IN ('locked', 'buying', 'steam_bought', 'listing_c5')
                ORDER BY id
                """
            ).fetchall()
            if risky:
                labels = ", ".join(f"{row['id']}:{row['status']}" for row in risky)
                raise RuntimeError(f"Profit Trade has execution-stage rows: {labels}")
        finally:
            con.close()

    def _load_account_and_client(self) -> None:
        account = AccountStore(PROJECT_ROOT / "config").get_account(self.account_name)
        if account is None:
            raise RuntimeError(f"account not found: {self.account_name}")
        if not account.cookies or not account.steam_id64:
            raise RuntimeError(f"account lacks Steam cookies or SteamID: {account.name}")
        self.account = account
        self.client = SteamMarketClient(
            cookies=account.cookies,
            steam_id64=account.steam_id64,
            identity_secret=account.identity_secret,
            device_id=account.device_id,
            account_id=account.id,
            base_url=self.settings.steam_market_base_url,
            request_source="steam_buy_order_reprice_experiment",
        )

    def _wallet_snapshot(self) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("Steam client is unavailable")
        wallet = self.client.wallet_balance(safety_terminal=True)
        currency_id = int(wallet.get("currency_id") or 0)
        if currency_id != CURRENCY_ID:
            raise RuntimeError(f"wallet is not CNY: currencyId={currency_id}")
        return {
            "balance": wallet.get("balance"),
            "delayedBalance": wallet.get("delayed_balance"),
            "currency": wallet.get("currency"),
            "currencyId": currency_id,
        }

    def _candidate_orders(self) -> list[dict[str, Any]]:
        if self.client is None:
            raise RuntimeError("Steam client is unavailable")
        payload = self.client.my_listings(count=100, safety_terminal=True)
        rows = candidate_order_snapshots(payload, self.market_hash_name)
        for row in rows:
            order_id_value = str(row.get("buyOrderId") or "")
            if order_id_value:
                self.observed_experiment_order_ids.add(order_id_value)
        return rows

    @staticmethod
    def _order_signature(rows: Iterable[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
        return tuple(
            (
                row.get("buyOrderId"),
                row.get("priceCents"),
                row.get("quantity"),
                row.get("quantityRemaining"),
            )
            for row in rows
        )

    def _settled_candidate_orders(
        self,
        *,
        minimum_wait_seconds: float = 2.0,
        timeout_seconds: float = 20.0,
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        deadline = started + timeout_seconds
        last_signature: tuple[tuple[Any, ...], ...] | None = None
        stable_count = 0
        latest: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            latest = self._candidate_orders()
            signature = self._order_signature(latest)
            if signature == last_signature:
                stable_count += 1
            else:
                last_signature = signature
                stable_count = 1
            if time.monotonic() - started >= minimum_wait_seconds and stable_count >= 2:
                return latest
            time.sleep(1.0)
        return latest

    def _create_initial_order(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self.client is None:
            raise RuntimeError("Steam client is unavailable")
        payload = self.client.create_buy_order(
            app_id=APP_ID,
            market_hash_name=self.market_hash_name,
            price_total=self.initial_price_cents,
            quantity=1,
            currency=CURRENCY_ID,
        )
        response = public_response_payload(payload, http_status=200, kind="success")
        rows = self._settled_candidate_orders()
        response_order_id = str(response.get("buyOrderId") or "")
        if response_order_id:
            original_order_id = response_order_id
        elif len(rows) == 1:
            original_order_id = str(rows[0].get("buyOrderId") or "")
        else:
            original_order_id = ""
        if not original_order_id:
            raise RuntimeError("initial createbuyorder returned no recoverable order ID")
        matching = [row for row in rows if row.get("buyOrderId") == original_order_id]
        if len(matching) != 1:
            raise RuntimeError("initial buy order did not become uniquely visible in mylistings")
        visible_price = matching[0].get("priceCents")
        if visible_price is not None and visible_price != self.initial_price_cents:
            raise RuntimeError(
                f"initial buy order price mismatch: expected={self.initial_price_cents} "
                f"actual={visible_price}"
            )
        self.original_order_id = original_order_id
        self.observed_experiment_order_ids.add(original_order_id)
        self.event(
            "initial_order_visible",
            buyOrderId=original_order_id,
            priceCents=visible_price,
            orders=rows,
        )
        self._write_state()
        return response, rows

    def _ordinary_duplicate_probe(self) -> ProbeResult:
        if self.client is None or not self.original_order_id:
            raise RuntimeError("initial order is unavailable")
        try:
            payload = self.client.create_buy_order(
                app_id=APP_ID,
                market_hash_name=self.market_hash_name,
                price_total=self.higher_price_cents,
                quantity=1,
                currency=CURRENCY_ID,
            )
            response = public_response_payload(payload, http_status=200, kind="success")
        except SteamMarketError as exc:
            response = public_response_payload(
                exc.payload,
                http_status=exc.status_code,
                kind="steam_error",
                fallback_message=str(exc),
            )
            if exc.status_code == 429:
                raise
        rows = self._settled_candidate_orders()
        classification = classify_transition(
            original_order_id=self.original_order_id,
            original_price_cents=self.initial_price_cents,
            requested_price_cents=self.higher_price_cents,
            orders_after=rows,
        )
        self.event(
            "ordinary_duplicate_result",
            response=response,
            orders=rows,
            classification=classification,
        )
        return ProbeResult(response, rows, classification)

    def _raw_create_with_old_id(self) -> dict[str, Any]:
        if self.client is None or not self.original_order_id:
            raise RuntimeError("initial order is unavailable")
        data = build_create_buy_order_data(
            session_id=self.client.sessionid,
            market_hash_name=self.market_hash_name,
            price_total=self.higher_price_cents,
            buy_order_id_value=self.original_order_id,
        )
        request_path = "/market/createbuyorder/"
        try:
            response = self.client._session.post(  # noqa: SLF001 - isolated diagnostic probe
                f"{self.client.base_url}{request_path}",
                data=data,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Origin": self.client.base_url,
                    "Referer": (
                        f"{self.client.base_url}/market/listings/{APP_ID}/"
                        f"{quote(self.market_hash_name, safe='')}"
                    ),
                    "X-Requested-With": "XMLHttpRequest",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    ),
                },
                timeout=self.client.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SteamMarketError(f"explicit-id createbuyorder request failed: {exc}") from exc
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {"message": _safe_text(payload)}
        except ValueError:
            payload = {"message": _safe_text(response.text)}
        return public_response_payload(
            payload,
            http_status=response.status_code,
            kind="raw_response",
        )

    def _explicit_id_probe(self) -> ProbeResult:
        if not self.original_order_id:
            raise RuntimeError("initial order is unavailable")
        before = self._candidate_orders()
        if len(before) != 1 or before[0].get("buyOrderId") != self.original_order_id:
            raise RuntimeError("original order is not uniquely intact before explicit-id probe")
        before_price = before[0].get("priceCents")
        if before_price is not None and before_price != self.initial_price_cents:
            raise RuntimeError("original order price changed before explicit-id probe")
        response = self._raw_create_with_old_id()
        rows = self._settled_candidate_orders()
        classification = classify_transition(
            original_order_id=self.original_order_id,
            original_price_cents=self.initial_price_cents,
            requested_price_cents=self.higher_price_cents,
            orders_after=rows,
        )
        if response.get("needConfirmation"):
            classification = "confirmation_required_not_approved"
        self.event(
            "explicit_id_result",
            response=response,
            orders=rows,
            classification=classification,
        )
        return ProbeResult(response, rows, classification)

    def _purchase_receipt_snapshot(self) -> dict[str, Any] | None:
        if self.client is None:
            return None
        receipt = self.client.find_purchase_receipt(
            market_hash_name=self.market_hash_name,
            maximum_total=self.higher_price_cents / 100.0,
            earliest_time=self.started_at - 5.0,
            count=100,
            max_pages=2,
            safety_terminal=True,
        )
        if not receipt:
            return None
        return {
            "listingId": receipt.get("listingId") or receipt.get("listing_id"),
            "purchaseId": receipt.get("purchaseId") or receipt.get("purchase_id"),
            "assetId": receipt.get("assetId") or receipt.get("asset_id"),
            "total": receipt.get("total") or receipt.get("price"),
            "time": receipt.get("time") or receipt.get("timestamp"),
        }

    def _cleanup(self) -> dict[str, Any]:
        if self.client is None:
            return {"attempted": False, "confirmedAbsent": False, "cancelledOrderIds": []}
        rows = self._settled_candidate_orders(minimum_wait_seconds=1.0, timeout_seconds=8.0)
        active_ids = [str(row.get("buyOrderId") or "") for row in rows]
        active_ids = [value for value in active_ids if value]
        cancelled: list[str] = []
        for order_id_value in active_ids:
            # The baseline check proved this account had no order for this item.
            # Therefore every same-item order observed during this isolated run
            # belongs to the experiment, even if Steam did not return its ID.
            try:
                self.client.cancel_buy_order(buy_order_id=order_id_value)
                cancelled.append(order_id_value)
                self.event("cleanup_cancel_submitted", buyOrderId=order_id_value)
            except Exception as exc:
                message = f"cancel {order_id_value} failed: {_safe_text(exc)}"
                self.cleanup_errors.append(message)
                self.event("cleanup_cancel_failed", buyOrderId=order_id_value, error=message)
        remaining = self._settled_candidate_orders(minimum_wait_seconds=2.0, timeout_seconds=20.0)
        if remaining:
            message = "candidate buy orders remain active after cleanup"
            self.cleanup_errors.append(message)
            self.event("cleanup_remote_state_not_empty", orders=remaining)
        result = {
            "attempted": True,
            "cancelledOrderIds": cancelled,
            "remainingOrders": remaining,
            "confirmedAbsent": not remaining,
        }
        self.event("cleanup_finished", **result)
        return result

    @staticmethod
    def _conclusion(
        ordinary: ProbeResult | None,
        explicit: ProbeResult | None,
    ) -> str:
        classifications = {
            row.classification for row in (ordinary, explicit) if row is not None
        }
        if "same_id_price_changed" in classifications:
            return "same_id_reprice_observed_needs_ac_time_priority_test"
        if classifications.intersection(
            {"second_order_created", "old_order_replaced_with_new_id"}
        ):
            return "request_did_not_preserve_the_original_order_identity"
        if ordinary and ordinary.classification == "original_order_unchanged":
            if explicit is None or explicit.classification in {
                "original_order_unchanged",
                "confirmation_required_not_approved",
            }:
                return "no_in_place_reprice_behavior_observed"
        return "server_behavior_remains_ambiguous"

    def run(self) -> int:
        if not self.execute:
            self.summary.update(
                {
                    "status": "dry_run",
                    "conclusion": "no Steam request sent",
                    "safety": {
                        "currencyId": CURRENCY_ID,
                        "maxPriceCents": self.max_price_cents,
                        "sellAsset": False,
                        "touchC5": False,
                    },
                }
            )
            self._write_state()
            self._write_report()
            return 0

        ordinary: ProbeResult | None = None
        explicit: ProbeResult | None = None
        cleanup: dict[str, Any] = {
            "attempted": False,
            "confirmedAbsent": False,
            "cancelledOrderIds": [],
        }
        try:
            self._assert_environment_stopped()
            self._load_account_and_client()
            wallet_before = self._wallet_snapshot()
            self.summary["walletBefore"] = wallet_before
            self.event("wallet_verified", **wallet_before)

            if self.client is None:
                raise RuntimeError("Steam client is unavailable")
            orderbook = self.client.order_book(
                app_id=APP_ID,
                market_hash_name=self.market_hash_name,
            )
            min_sell = orderbook_min_sell_cents(orderbook)
            if min_sell is None:
                raise RuntimeError("Steam orderbook has no readable minimum sell price")
            if min_sell <= self.higher_price_cents * 10:
                raise RuntimeError(
                    f"minimum sell price {min_sell} cents lacks the required 10x safety gap"
                )
            self.summary["safety"] = {
                "currencyId": CURRENCY_ID,
                "minSellCents": min_sell,
                "requiredGapMultiple": 10,
                "maxPriceCents": self.max_price_cents,
                "sellAsset": False,
                "touchC5": False,
            }
            self.event("orderbook_safety_gap_verified", minSellCents=min_sell)

            baseline = self._candidate_orders()
            self.summary["baselineOrders"] = baseline
            if baseline:
                raise RuntimeError(
                    "account already has a buy order for the candidate; refusing to touch it"
                )
            self.event("baseline_clear")

            initial_response, initial_orders = self._create_initial_order()
            self.summary["initialOrder"] = {
                "response": initial_response,
                "orders": initial_orders,
            }

            ordinary = self._ordinary_duplicate_probe()
            self.summary["ordinaryDuplicate"] = {
                "response": ordinary.response,
                "ordersAfter": ordinary.orders_after,
                "classification": ordinary.classification,
            }
            self._write_state()

            original_intact = (
                ordinary.classification
                in {"original_order_unchanged", "same_id_price_unknown"}
                and len(ordinary.orders_after) == 1
                and ordinary.orders_after[0].get("buyOrderId") == self.original_order_id
            )
            if self.probe_explicit_id and original_intact:
                explicit = self._explicit_id_probe()
                self.summary["explicitIdProbe"] = {
                    "response": explicit.response,
                    "ordersAfter": explicit.orders_after,
                    "classification": explicit.classification,
                }
            elif self.probe_explicit_id:
                self.summary["explicitIdProbe"] = {
                    "skipped": True,
                    "reason": "ordinary probe did not leave the original order uniquely intact",
                }

            self.summary["purchaseReceiptBeforeCleanup"] = self._purchase_receipt_snapshot()
            self.summary["conclusion"] = self._conclusion(ordinary, explicit)
        except Exception as exc:
            message = _safe_text(exc)
            self.errors.append(message)
            self.event("experiment_error", error=message)
        finally:
            if self.client is not None:
                try:
                    cleanup = self._cleanup()
                except Exception as exc:
                    message = f"cleanup failed: {_safe_text(exc)}"
                    self.cleanup_errors.append(message)
                    self.event("cleanup_error", error=message)
                try:
                    self.summary["walletAfter"] = self._wallet_snapshot()
                except Exception as exc:
                    self.errors.append(f"wallet-after check failed: {_safe_text(exc)}")
                try:
                    self.summary["purchaseReceiptAfterCleanup"] = self._purchase_receipt_snapshot()
                except Exception as exc:
                    self.errors.append(f"history check failed: {_safe_text(exc)}")
            self.summary["cleanup"] = cleanup
            self.summary["finishedAt"] = utc_iso()
            if self.cleanup_errors or not cleanup.get("confirmedAbsent"):
                self.summary["status"] = "cleanup_not_confirmed"
            elif self.errors:
                self.summary["status"] = "completed_with_errors"
            else:
                self.summary["status"] = "completed"
            self._write_state()
            self._write_report()
        return 0 if self.summary.get("status") == "completed" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolated Steam createbuyorder same-order reprice probe"
    )
    parser.add_argument("--account", required=True, help="local account id or name")
    parser.add_argument("--market-hash-name", default="Glove Case")
    parser.add_argument("--initial-price-cents", type=int, default=21)
    parser.add_argument("--higher-price-cents", type=int, default=22)
    parser.add_argument("--max-price-cents", type=int, default=DEFAULT_MAX_PRICE_CENTS)
    parser.add_argument("--skip-explicit-id-probe", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stamp = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / stamp
    experiment = SteamBuyOrderRepriceExperiment(
        output_dir=output_dir,
        account_name=str(args.account),
        market_hash_name=str(args.market_hash_name),
        initial_price_cents=int(args.initial_price_cents),
        higher_price_cents=int(args.higher_price_cents),
        execute=bool(args.execute),
        probe_explicit_id=not bool(args.skip_explicit_id_probe),
        max_price_cents=int(args.max_price_cents),
    )
    return experiment.run()


if __name__ == "__main__":
    raise SystemExit(main())
