from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from steampy.client import SteamClient as SteampyClient
from steampy.models import Currency

from cs2_assistant.accounts import AccountStore
from cs2_assistant.config import PROJECT_ROOT, Settings
from cs2_assistant.utils import safe_float


_SNAPSHOT_LOCK = threading.RLock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_path(settings: Settings) -> Path:
    return settings.db_path.parent / "steam_balance_snapshot.json"


def _cookie_dict(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        text = part.strip()
        if not text or "=" not in text:
            continue
        key, value = text.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def _load_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_snapshot(path: Path, payload: dict[str, Any]) -> None:
    with _SNAPSHOT_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)


def update_steam_account_balance_snapshot(
    settings: Settings,
    *,
    wallet: dict[str, Any],
    account: Any | None = None,
    account_id: str | None = None,
    account_name: str | None = None,
    steam_id64: str | None = None,
    snapshot_path: Path | None = None,
) -> dict[str, Any]:
    """Merge one verified wallet response into the shared balance snapshot.

    Profit Trade and the guadao balance page deliberately use the same file.
    Only public balance fields are persisted; no cookies or credentials are
    accepted by this helper.
    """

    if not isinstance(wallet, dict):
        raise ValueError("Steam wallet response must be an object")
    resolved_id = str(account_id or getattr(account, "id", "") or "").strip()
    resolved_name = str(account_name or getattr(account, "name", "") or "").strip()
    resolved_steam_id = str(
        steam_id64 or getattr(account, "steam_id64", "") or ""
    ).strip()
    if not resolved_id and not resolved_steam_id:
        raise ValueError("Steam balance snapshot update requires account id or Steam ID")

    real_balance = safe_float(
        wallet.get("balance")
        if wallet.get("balance") is not None
        else wallet.get("realBalance")
    )
    pending_balance = safe_float(
        wallet.get("delayed_balance")
        if wallet.get("delayed_balance") is not None
        else wallet.get("pendingBalance")
    )
    if real_balance is None:
        raise ValueError("Steam wallet response is missing balance")
    if pending_balance is None:
        pending_balance = 0.0

    path = snapshot_path or _snapshot_path(settings)
    with _SNAPSHOT_LOCK:
        previous = _load_snapshot(path)
        rows = [
            dict(row)
            for row in previous.get("accounts") or []
            if isinstance(row, dict)
        ]
        matching_index: int | None = None
        for index, row in enumerate(rows):
            row_id = str(row.get("id") or "").strip()
            row_steam_id = str(row.get("steamId") or "").strip()
            if (resolved_id and row_id == resolved_id) or (
                resolved_steam_id and row_steam_id == resolved_steam_id
            ):
                matching_index = index
                break

        previous_row = rows[matching_index] if matching_index is not None else {}
        currency = str(
            wallet.get("currency") or previous_row.get("currency") or ""
        ).strip() or None
        currency_id = wallet.get("currency_id")
        if currency_id is None:
            currency_id = wallet.get("currencyId")
        if currency_id is None:
            currency_id = previous_row.get("currencyId")
        updated_row = {
            **previous_row,
            "id": resolved_id or str(previous_row.get("id") or ""),
            "account": resolved_name or str(previous_row.get("account") or ""),
            "steamId": resolved_steam_id or str(previous_row.get("steamId") or ""),
            "realBalance": round(float(real_balance), 2),
            "pendingBalance": round(float(pending_balance), 2),
            "totalBalance": round(float(real_balance) + float(pending_balance), 2),
            "currency": currency,
            "currencyId": currency_id,
            "status": "ok",
            "error": None,
            "stale": False,
        }
        if matching_index is None:
            rows.append(updated_row)
        else:
            rows[matching_index] = updated_row

        updated_at = _utc_now_iso()
        payload = {
            "accounts": rows,
            "summary": _summary(rows),
            "hasSnapshot": True,
            "updatedAt": updated_at,
            "source": "live",
        }
        _save_snapshot(path, payload)
        return payload


def _currency_code(currency_id: int) -> str:
    try:
        name = Currency(currency_id).name
    except ValueError:
        return f"STEAM_{currency_id}"
    return "EUR" if name == "EURO" else name


def _steam_wallet_amount(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(Decimal(str(raw)) / Decimal("100"))
    except Exception:
        return None


def _read_steampy_wallet(client: SteampyClient) -> dict[str, Any]:
    """Use steampy's wallet call and retain the full response it normally discards."""

    captured: dict[str, str] = {}
    original_get = client._session.get

    def capture_get(*args: Any, **kwargs: Any) -> Any:
        response = original_get(*args, **kwargs)
        captured["text"] = str(response.text)
        return response

    client._session.get = capture_get
    try:
        balance = safe_float(client.get_wallet_balance())
    finally:
        client._session.get = original_get

    match = re.search(
        r"(?:var\s+)?g_rgWalletInfo\s*=\s*(\{.*?\});",
        captured.get("text") or "",
        re.S,
    )
    if not match:
        raise RuntimeError("steampy 钱包响应缺少 g_rgWalletInfo")
    wallet_info = json.loads(match.group(1))
    delayed_balance = _steam_wallet_amount(wallet_info.get("wallet_delayed_balance"))
    try:
        currency_id = int(wallet_info.get("wallet_currency"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("steampy 钱包响应缺少 wallet_currency") from exc
    if balance is None or delayed_balance is None:
        raise RuntimeError("steampy 返回的 Steam 钱包余额无效")
    return {
        "balance": balance,
        "delayedBalance": delayed_balance,
        "currency": _currency_code(currency_id),
        "currencyId": currency_id,
    }


def _summary(rows: list[dict[str, Any]], *, successful_count: int | None = None) -> dict[str, Any]:
    success = (
        sum(1 for row in rows if row.get("status") == "ok")
        if successful_count is None
        else successful_count
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        currency = str(row.get("currency") or "").strip()
        if not currency:
            continue
        group = grouped.setdefault(
            currency,
            {
                "currency": currency,
                "currencyId": row.get("currencyId"),
                "accountCount": 0,
                "realBalance": 0.0,
                "pendingBalance": 0.0,
                "totalBalance": 0.0,
            },
        )
        group["accountCount"] += 1
        group["realBalance"] += float(row.get("realBalance") or 0)
        group["pendingBalance"] += float(row.get("pendingBalance") or 0)
    currencies = []
    for group in grouped.values():
        group["realBalance"] = round(group["realBalance"], 2)
        group["pendingBalance"] = round(group["pendingBalance"], 2)
        group["totalBalance"] = round(group["realBalance"] + group["pendingBalance"], 2)
        currencies.append(group)
    currencies.sort(key=lambda group: str(group["currency"]))
    single = currencies[0] if len(currencies) == 1 else None
    return {
        "accountCount": len(rows),
        "successfulCount": success,
        "failedCount": len(rows) - success,
        "currencyCount": len(currencies),
        "currencies": currencies,
        "currency": single.get("currency") if single else None,
        "realBalance": single.get("realBalance") if single else None,
        "pendingBalance": single.get("pendingBalance") if single else None,
        "totalBalance": single.get("totalBalance") if single else None,
    }


def load_steam_account_balances(
    settings: Settings,
    *,
    account_store: AccountStore | None = None,
    snapshot_path: Path | None = None,
) -> dict[str, Any]:
    """Return the last saved balances without making a Steam request."""

    store = account_store or AccountStore(PROJECT_ROOT / "config")
    accounts = [account for account in store.list_accounts() if account.cookies]
    snapshot = _load_snapshot(snapshot_path or _snapshot_path(settings))
    cached_by_id = {
        str(row.get("id") or ""): row
        for row in snapshot.get("accounts") or []
        if isinstance(row, dict)
    }
    rows: list[dict[str, Any]] = []
    for account in accounts:
        cached = dict(cached_by_id.get(account.id) or {})
        cached_balance = safe_float(cached.get("realBalance"))
        cached_delayed = safe_float(cached.get("pendingBalance"))
        rows.append(
            {
                "id": account.id,
                "account": account.name,
                "steamId": account.steam_id64,
                "realBalance": cached_balance,
                "pendingBalance": cached_delayed,
                "totalBalance": (
                    round(cached_balance + cached_delayed, 2)
                    if cached_balance is not None and cached_delayed is not None
                    else None
                ),
                "currency": cached.get("currency"),
                "currencyId": cached.get("currencyId"),
                "status": cached.get("status") or "skipped",
                "error": cached.get("error"),
                "stale": bool(cached),
            }
        )
    updated_at = str(snapshot.get("updatedAt") or "").strip() or None
    return {
        "accounts": rows,
        "summary": _summary(rows),
        "hasSnapshot": bool(updated_at),
        "updatedAt": updated_at,
        "source": "cache",
    }


def refresh_steam_account_balances(
    settings: Settings,
    *,
    account_store: AccountStore | None = None,
    client_factory: Callable[..., SteampyClient] = SteampyClient,
    snapshot_path: Path | None = None,
) -> dict[str, Any]:
    """Refresh balances through steampy's public wallet API and save them."""

    store = account_store or AccountStore(PROJECT_ROOT / "config")
    accounts = [account for account in store.list_accounts() if account.cookies]
    path = snapshot_path or _snapshot_path(settings)
    previous = _load_snapshot(path)
    previous_by_id = {
        str(row.get("id") or ""): row
        for row in previous.get("accounts") or []
        if isinstance(row, dict)
    }
    rows: list[dict[str, Any]] = []
    successful_count = 0

    for account in accounts:
        previous_row = dict(previous_by_id.get(account.id) or {})
        balance: float | None = None
        delayed_balance: float | None = None
        currency: str | None = None
        currency_id: int | None = None
        status = "ok"
        error: str | None = None
        stale = False

        if not account.steam_id64:
            status = "skipped"
            error = "账号缺少 Steam ID，无法读取余额"
        else:
            try:
                client = client_factory(
                    api_key="",
                    steam_guard=json.dumps({"steamid": account.steam_id64}),
                    login_cookies=_cookie_dict(account.cookies or ""),
                )
                wallet = _read_steampy_wallet(client)
                balance = safe_float(wallet.get("balance"))
                delayed_balance = safe_float(wallet.get("delayedBalance"))
                currency = str(wallet.get("currency") or "").strip() or None
                currency_id = int(wallet["currencyId"])
                successful_count += 1
            except Exception as exc:
                status = "error"
                error = str(exc)

        if status != "ok" and previous_row.get("realBalance") is not None:
            balance = safe_float(previous_row.get("realBalance"))
            delayed_balance = safe_float(previous_row.get("pendingBalance"))
            currency = str(previous_row.get("currency") or "").strip() or None
            currency_id = previous_row.get("currencyId")
            stale = True

        rows.append(
            {
                "id": account.id,
                "account": account.name,
                "steamId": account.steam_id64,
                "realBalance": balance,
                "pendingBalance": delayed_balance,
                "totalBalance": (
                    round(balance + delayed_balance, 2)
                    if balance is not None and delayed_balance is not None
                    else None
                ),
                "currency": currency,
                "currencyId": currency_id,
                "status": status,
                "error": error,
                "stale": stale,
            }
        )

    updated_at = _utc_now_iso()
    payload = {
        "accounts": rows,
        "summary": _summary(rows, successful_count=successful_count),
        "hasSnapshot": True,
        "updatedAt": updated_at,
        "source": "live",
    }
    _save_snapshot(path, payload)
    return payload
