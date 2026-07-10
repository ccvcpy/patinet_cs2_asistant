from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable
from urllib.parse import quote, unquote

import requests

from cs2_assistant.accounts import AccountStore
from cs2_assistant.accounts.steam_auth import try_steam_auto_relogin
from cs2_assistant.config import PROJECT_ROOT


class SteamMarketError(RuntimeError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload


def _parse_cookie_string(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def _extract_sessionid(cookies: dict[str, str]) -> str | None:
    return cookies.get("sessionid")


def _extract_steam_id64(cookies: dict[str, str]) -> str | None:
    raw = cookies.get("steamLoginSecure") or cookies.get("steamLogin")
    if raw:
        decoded = unquote(raw)
        for sep in ("||", "|"):
            if sep in decoded:
                candidate = decoded.split(sep, 1)[0]
                if candidate.isdigit():
                    return candidate
        match = re.match(r"(\\d{16,17})", decoded)
        if match:
            return match.group(1)
    steam_id = cookies.get("steamid") or cookies.get("steamId") or cookies.get("steamID")
    if steam_id and steam_id.isdigit():
        return steam_id
    return None


def _normalize_identity_secret(raw: str) -> str:
    """Normalize Steam identity_secret that may contain JSON-escaped characters."""
    raw = raw.strip()
    raw = raw.replace("\\u002B", "+").replace("\\u002b", "+")
    raw = raw.replace("\u002B", "+").replace("\u002b", "+")
    raw = raw.replace("\\/", "/")
    return raw


def _steam_confirmation_key(secret_b64: str, tag: str, timestamp: int) -> str:
    """Generate a Steam Guard confirmation HMAC-SHA1 key.

    Steam Guard packs the timestamp as a big-endian unsigned 64-bit integer
    followed by the ASCII tag bytes — NOT as a UTF-8 string.
    """
    secret = base64.b64decode(secret_b64)
    time_bytes = struct.pack(">Q", int(timestamp))
    if tag:
        time_bytes += tag.encode("ascii", errors="ignore")
    digest = hmac.new(secret, time_bytes, hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


@dataclass(slots=True)
class SteamListing:
    listing_id: str
    asset_id: str | None
    market_hash_name: str | None
    price: float | None
    status: int | None


_STEAM_CURRENCY_CODES = {
    1: "USD",
    3: "EUR",
    5: "RUB",
    6: "PLN",
    7: "BRL",
    8: "JPY",
    9: "NOK",
    10: "IDR",
    11: "MYR",
    12: "PHP",
    13: "SGD",
    14: "THB",
    15: "VND",
    16: "KRW",
    17: "TRY",
    18: "UAH",
    19: "MXN",
    20: "CAD",
    21: "AUD",
    22: "NZD",
    23: "CNY",
    24: "INR",
    25: "CLP",
    26: "PEN",
    27: "COP",
    28: "ZAR",
    29: "HKD",
    30: "TWD",
    31: "SAR",
    32: "AED",
}


def _steam_wallet_amount(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(Decimal(str(raw)) / Decimal("100"))
    except Exception:
        return None


def _safe_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(Decimal(str(raw)))
    except Exception:
        return None


def _first_int(raw: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _safe_int(raw.get(key))
        if value is not None:
            return value
    return None


def _confirmation_id_from_payload(payload: dict[str, Any]) -> str:
    confirmation = payload.get("confirmation")
    if isinstance(confirmation, dict):
        for key in ("confirmation_id", "confirmationid", "id"):
            value = str(confirmation.get(key) or "").strip()
            if value:
                return value
    for key in ("confirmation_id", "confirmationid", "confirmationId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _market_listing_hash_name(row: dict[str, Any]) -> str | None:
    description = row.get("description")
    if isinstance(description, dict):
        value = description.get("market_hash_name") or description.get("market_name")
        if value:
            return str(value)
    value = row.get("market_hash_name") or row.get("marketHashName")
    if value:
        return str(value)
    return None


def _compact_listing_asset(asset: Any) -> Any:
    if not isinstance(asset, dict):
        return asset
    keys = ("id", "assetid", "classid", "instanceid", "amount", "appid", "contextid")
    return {key: asset.get(key) for key in keys if key in asset}


def _normalize_listing_row(
    row: dict[str, Any],
    *,
    raw_listing_id: str | None = None,
) -> dict[str, Any] | None:
    listing_id = str(row.get("listingid") or raw_listing_id or "").strip()
    if not listing_id:
        return None

    asset = row.get("asset")
    if isinstance(asset, dict) and _safe_int(asset.get("amount")) == 0:
        return None
    if row.get("bMine") is True:
        return None

    subtotal = _first_int(
        row,
        (
            "converted_price",
            "unPricePerUnit",
            "unPrice",
            "subtotal",
            "price",
        ),
    )
    fee = _first_int(
        row,
        (
            "converted_fee",
            "unFeePerUnit",
            "unFee",
            "fee",
        ),
    )
    total = _first_int(
        row,
        (
            "converted_total",
            "unTotal",
            "unTotalPerUnit",
            "total",
        ),
    )
    if total is None and subtotal is not None and fee is not None:
        total = subtotal + fee
    if subtotal is None and total is not None and fee is not None:
        subtotal = total - fee
    if fee is None and total is not None and subtotal is not None:
        fee = total - subtotal
    if subtotal is None or fee is None or total is None or total <= 0:
        return None

    currency = _first_int(
        row,
        (
            "converted_currencyid",
            "currencyid",
            "eCurrency",
            "currency",
        ),
    )
    description = row.get("description") if isinstance(row.get("description"), dict) else None
    return {
        "listingid": listing_id,
        "converted_price": int(subtotal),
        "converted_fee": int(fee),
        "converted_total": int(total),
        "converted_currencyid": currency,
        "eCurrency": currency,
        "asset": _compact_listing_asset(asset),
        "description": description,
        "market_hash_name": _market_listing_hash_name(row),
        "strSubtotal": row.get("strSubtotal"),
        "unPrice": row.get("unPrice"),
        "unFee": row.get("unFee"),
        "unPricePerUnit": row.get("unPricePerUnit"),
        "unFeePerUnit": row.get("unFeePerUnit"),
        "bMine": row.get("bMine"),
    }


def _normalize_market_search_payload(
    payload: dict[str, Any],
    *,
    market_hash_name: str,
    limit: int | None = None,
) -> dict[str, Any]:
    listinginfo: dict[str, Any] = {}
    rows = payload.get("listings")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_hash_name = _market_listing_hash_name(row)
            if row_hash_name and row_hash_name != market_hash_name:
                continue
            normalized = _normalize_listing_row(row)
            if normalized is None:
                continue
            listinginfo[str(normalized["listingid"])] = normalized
            if limit is not None and len(listinginfo) >= limit:
                break
    elif isinstance(rows, dict):
        for listing_id, row in rows.items():
            if not isinstance(row, dict):
                continue
            row_hash_name = _market_listing_hash_name(row)
            if row_hash_name and row_hash_name != market_hash_name:
                continue
            normalized = _normalize_listing_row(row, raw_listing_id=str(listing_id))
            if normalized is None:
                continue
            listinginfo[str(normalized["listingid"])] = normalized
            if limit is not None and len(listinginfo) >= limit:
                break

    result = dict(payload)
    result["success"] = payload.get("success", True)
    result["listinginfo"] = listinginfo
    return result


class SteamMarketClient:
    def __init__(
        self,
        *,
        cookies: str | None,
        steam_id64: str | None = None,
        identity_secret: str | None = None,
        device_id: str | None = None,
        account_id: str | None = None,
        base_url: str = "https://steamcommunity.com",
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity_secret = _normalize_identity_secret(identity_secret) if identity_secret else identity_secret
        self.device_id = unquote(device_id) if device_id else device_id
        self.timeout = timeout
        self.account_id = str(account_id or "").strip() or None
        self._account_store = AccountStore(PROJECT_ROOT / "config") if self.account_id else None

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Steam Mobile/10372190 CFNetwork/3860.100.1 Darwin/25.0.0",
            }
        )
        cookie_source = cookies
        if not cookie_source and self._account_store and self.account_id:
            account = self._account_store.get_account(self.account_id)
            if account:
                cookie_source = account.cookies
                steam_id64 = steam_id64 or account.steam_id64
                if not self.identity_secret:
                    self.identity_secret = account.identity_secret
                if not self.device_id:
                    self.device_id = account.device_id
        if not cookie_source:
            raise SteamMarketError("missing Steam cookies")
        self._apply_cookie_string(cookie_source, steam_id64=steam_id64)

    @property
    def sessionid(self) -> str:
        return self._sessionid

    def _apply_cookie_string(self, cookies: str, *, steam_id64: str | None = None) -> None:
        cookie_map = _parse_cookie_string(cookies)
        self._session.cookies.clear()
        self._session.cookies.update(cookie_map)
        self._sessionid = _extract_sessionid(cookie_map)
        if not self._sessionid:
            raise SteamMarketError("Steam cookies missing sessionid")
        resolved_id64 = steam_id64 or _extract_steam_id64(cookie_map)
        if not resolved_id64:
            raise SteamMarketError("missing Steam ID64 in cookies")
        self.steam_id64 = resolved_id64

    def _try_account_relogin(self) -> bool:
        if not self._account_store or not self.account_id:
            return False
        ok, _, account = try_steam_auto_relogin(
            self._account_store,
            account_id=self.account_id,
            force_login=True,
        )
        if not ok or account is None or not account.cookies:
            return False
        self._apply_cookie_string(account.cookies, steam_id64=account.steam_id64)
        if account.identity_secret and not self.identity_secret:
            self.identity_secret = account.identity_secret
        if account.device_id and not self.device_id:
            self.device_id = account.device_id
        return True

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | str | None = None,
        files: Any | None = None,
        headers: dict[str, str] | None = None,
        _allow_retry: bool = True,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        merged_headers = dict(headers or {})
        attempts = 3 if method.upper() == "GET" else 1
        last_exc: requests.RequestException | None = None
        try:
            response = None
            for attempt in range(attempts):
                try:
                    response = self._session.request(
                        method=method,
                        url=url,
                        params=params,
                        data=data,
                        files=files,
                        headers=merged_headers,
                        timeout=self.timeout,
                    )
                    break
                except (requests.Timeout, requests.ConnectionError) as exc:
                    last_exc = exc
                    if attempt >= attempts - 1:
                        raise
                    time.sleep(1.0 + attempt)
            if response is None:
                raise last_exc or SteamMarketError("Steam request failed without response")
            if response.status_code in (400, 401) and _allow_retry and self._try_account_relogin():
                response = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    files=files,
                    headers=merged_headers,
                    timeout=self.timeout,
                )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SteamMarketError(f"Steam request failed: {method} {path}: {exc}") from exc
        return response

    def get_trade_url(self) -> str:
        """从 Steam 交易隐私页面自动获取当前账号的交易链接。"""
        response = self._request("GET", f"/profiles/{self.steam_id64}/tradeoffers/privacy")
        match = re.search(
            r"https://steamcommunity\.com/tradeoffer/new/\?partner=\d+&token=[A-Za-z0-9_-]+",
            response.text,
        )
        if match:
            return match.group(0)
        raise SteamMarketError("无法从页面提取交易链接，请确认账号已登录且交易链接已启用")

    def wallet_balance(self) -> dict[str, Any]:
        response = self._request("GET", "/market/")
        match = re.search(r"g_rgWalletInfo\s*=\s*(\{.*?\});", response.text, re.S)
        if not match:
            raise SteamMarketError("Steam wallet info not found")
        try:
            wallet_info = json.loads(match.group(1))
        except ValueError as exc:
            raise SteamMarketError("Steam wallet info invalid JSON") from exc
        safe_currency_id: int | None = None
        currency_id: int | str | None
        try:
            safe_currency_id = int(wallet_info.get("wallet_currency"))
            currency_id = safe_currency_id
        except Exception:
            currency_id = wallet_info.get("wallet_currency")
        return {
            "balance": _steam_wallet_amount(wallet_info.get("wallet_balance")),
            "delayed_balance": _steam_wallet_amount(wallet_info.get("wallet_delayed_balance")),
            "currency": _STEAM_CURRENCY_CODES.get(safe_currency_id, str(currency_id or "")),
            "currency_id": currency_id,
            "raw": wallet_info,
        }

    def remove_listing(self, listing_id: str) -> bool:
        """Cancel a Steam market listing by listing ID."""
        response = self._request(
            "POST",
            f"/market/removelisting/{listing_id}",
            data={"sessionid": self.sessionid},
            headers={"Referer": f"{self.base_url}/market"},
        )
        try:
            payload = response.json()
            if isinstance(payload, list):
                return response.status_code == 200
            return bool(payload.get("success", True))
        except ValueError:
            return response.status_code == 200

    def sell_item(
        self,
        *,
        app_id: int,
        context_id: str,
        asset_id: str,
        price: float,
        quantity: int = 1,
        steam_net_factor: float = 0.869,
    ) -> dict[str, Any]:
        """List an item on the Steam market.

        Args:
            price: The buyer's listing price (what appears on the market page).
                   Steam's API internally expects the seller's net amount;
                   this method converts automatically using steam_net_factor.
            steam_net_factor: Seller's take rate (default 0.869 = 86.9% for CS2).
        """
        if price <= 0:
            raise SteamMarketError("price must be positive")
        if quantity <= 0:
            raise SteamMarketError("quantity must be positive")

        # Steam's sellitem API 'price' field = seller's net amount in cents.
        # Caller passes buyer's listing price, so we convert here.
        seller_net_cents = int(
            (
                Decimal(str(float(price)))
                * Decimal(str(float(steam_net_factor)))
                * Decimal("100")
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        data = {
            "sessionid": self.sessionid,
            "appid": app_id,
            "contextid": context_id,
            "assetid": asset_id,
            "amount": quantity,
            "price": seller_net_cents,
        }
        profile_inventory_url = f"{self.base_url}/profiles/{self.steam_id64}/inventory"
        response = self._request(
            "POST",
            "/market/sellitem/",
            data=data,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": self.base_url,
                "Referer": profile_inventory_url,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam sellitem invalid JSON: {response.text}") from exc
        if payload.get("success") != 1:
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False), payload=payload)
        return payload

    def buy_listing(
        self,
        *,
        listing_id: str,
        app_id: int,
        subtotal: int,
        fee: int,
        total: int,
        currency: int = 23,
        country: str = "CN",
        tradefee_tax: int = 0,
        market_hash_name: str | None = None,
    ) -> dict[str, Any]:
        if not listing_id:
            raise SteamMarketError("listing_id is required")
        if subtotal < 0 or fee < 0 or total <= 0:
            raise SteamMarketError("subtotal, fee, total must be valid cents values")

        referer_name = quote(str(market_hash_name or "").strip(), safe="")
        referer = (
            f"{self.base_url}/market/listings/{app_id}/{referer_name}"
            if referer_name
            else f"{self.base_url}/market/listings/{app_id}"
        )

        def post(confirmation: str = "0") -> dict[str, Any]:
            data = {
                "sessionid": self.sessionid,
                "currency": int(currency),
                "subtotal": int(subtotal),
                "fee": int(fee),
                "total": int(total),
                "tradefee_tax": int(tradefee_tax),
                "quantity": 1,
                "first_name": "",
                "last_name": "",
                "billing_address": "",
                "billing_address_two": "",
                "billing_country": country,
                "billing_city": "",
                "billing_state": "",
                "billing_postal_code": "",
                "confirmation": confirmation,
                "save_my_address": "0",
            }
            files = [(key, (None, str(value))) for key, value in data.items()]
            response = self._session.post(
                f"{self.base_url}/market/buylisting/{listing_id}",
                files=files,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Origin": self.base_url,
                    "Referer": referer,
                    "X-Requested-With": "XMLHttpRequest",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    ),
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise SteamMarketError(f"Steam buylisting invalid JSON: {response.text}") from exc
            wallet_info = payload.get("wallet_info") or {}
            success = payload.get("success")
            wallet_success = wallet_info.get("success")
            if response.ok and (success in (1, True) or wallet_success in (1, True)):
                return payload
            if payload.get("need_confirmation"):
                return payload
            message = payload.get("message") or wallet_info.get("message") or payload
            raise SteamMarketError(json.dumps(message, ensure_ascii=False), payload=payload)

        first_payload = post("0")
        wallet_info = first_payload.get("wallet_info") or {}
        if first_payload.get("success") in (1, True) or wallet_info.get("success") in (1, True):
            return first_payload

        confirmation_id = _confirmation_id_from_payload(first_payload)
        if not confirmation_id:
            raise SteamMarketError(
                f"Steam buylisting requires confirmation but returned no confirmation_id: "
                f"{json.dumps(first_payload, ensure_ascii=False)}",
                payload=first_payload,
            )
        self._allow_confirmation_creator_id(confirmation_id, action="Steam buylisting")
        second_payload = post(confirmation_id)
        second_wallet_info = second_payload.get("wallet_info") or {}
        if second_payload.get("success") in (1, True) or second_wallet_info.get("success") in (1, True):
            return second_payload
        raise SteamMarketError(json.dumps(second_payload, ensure_ascii=False), payload=second_payload)

    def create_buy_order(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        price_total: int,
        quantity: int = 1,
        currency: int = 23,
        country: str = "CN",
        return_uncertain_after_confirmation: bool = False,
    ) -> dict[str, Any]:
        if not market_hash_name:
            raise SteamMarketError("market_hash_name is required")
        if price_total <= 0 or quantity <= 0:
            raise SteamMarketError("price_total and quantity must be positive")

        def post(confirmation: str = "0", *, return_error_payload: bool = False) -> dict[str, Any]:
            data = {
                "sessionid": self.sessionid,
                "currency": int(currency),
                "appid": int(app_id),
                "market_hash_name": market_hash_name,
                "price_total": int(price_total),
                "tradefee_tax": 0,
                "quantity": int(quantity),
                "first_name": "",
                "last_name": "",
                "billing_address": "",
                "billing_address_two": "",
                "billing_country": country,
                "billing_city": "",
                "billing_state": "",
                "billing_postal_code": "",
                "confirmation": confirmation,
                "save_my_address": "0",
            }
            response = self._session.post(
                f"{self.base_url}/market/createbuyorder/",
                data=data,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Origin": self.base_url,
                    "Referer": (
                        f"{self.base_url}/market/listings/{app_id}/"
                        f"{quote(market_hash_name, safe='')}"
                    ),
                    "X-Requested-With": "XMLHttpRequest",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    ),
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise SteamMarketError(
                    f"Steam createbuyorder invalid JSON: {response.text}"
                ) from exc
            if response.ok and payload.get("success") == 1:
                return payload
            if payload.get("need_confirmation"):
                return payload
            if return_error_payload:
                payload["_steam_http_status"] = response.status_code
                payload["_outcome_uncertain_after_confirmation"] = True
                return payload
            message = payload.get("message") or payload.get("error") or payload
            raise SteamMarketError(json.dumps(message, ensure_ascii=False), payload=payload)

        first_payload = post("0")
        if first_payload.get("success") == 1:
            return first_payload

        confirmation_id = _confirmation_id_from_payload(first_payload)
        if not confirmation_id:
            raise SteamMarketError(
                f"Steam createbuyorder requires confirmation but returned no confirmation_id: "
                f"{json.dumps(first_payload, ensure_ascii=False)}",
                payload=first_payload,
            )
        self._allow_confirmation_creator_id(confirmation_id, action="Steam createbuyorder")
        second_payload = post(
            confirmation_id,
            return_error_payload=return_uncertain_after_confirmation,
        )
        if second_payload.get("_outcome_uncertain_after_confirmation"):
            return second_payload
        if second_payload.get("success") != 1:
            raise SteamMarketError(json.dumps(second_payload, ensure_ascii=False), payload=second_payload)
        return second_payload

    def cancel_buy_order(self, *, buy_order_id: str) -> dict[str, Any]:
        buy_order_id = str(buy_order_id or "").strip()
        if not buy_order_id:
            raise SteamMarketError("buy_order_id is required")
        response = self._session.post(
            f"{self.base_url}/market/cancelbuyorder/",
            data={
                "sessionid": self.sessionid,
                "buy_orderid": buy_order_id,
            },
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/market/",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                ),
            },
            timeout=self.timeout,
            allow_redirects=False,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam cancelbuyorder invalid JSON: {response.text}") from exc
        if response.ok and payload.get("success") in (1, True):
            return payload
        message = payload.get("message") or payload.get("error") or payload
        raise SteamMarketError(json.dumps(message, ensure_ascii=False), payload=payload)

    def search_listings(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        start: int = 0,
        count: int = 10,
        currency: int = 23,
        country: str = "CN",
        language: str = "schinese",
    ) -> dict[str, Any]:
        encoded_name = quote(market_hash_name, safe="")
        params = {
            "currency": int(currency),
            "language": language,
            "country": country,
        }
        query = {
            "appid": int(app_id),
            "strItemName": market_hash_name,
            "filters": {},
            "accessoryFilters": {},
            "propertyFilters": {},
            "disableGrouping": True,
            "start": int(start),
        }
        # Steam's current market UI no longer returns the legacy JSON from
        # /render/. The React route action behind the new page returns concrete
        # listing ids with unPrice/unFee, which are the values buylisting needs.
        response = self._request(
            "POST",
            f"/market/listings/{app_id}/{encoded_name}",
            params=params,
            data=json.dumps([query], ensure_ascii=False, separators=(",", ":")),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json; charset=utf-8",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/market/listings/{app_id}/{encoded_name}",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                ),
                "X-Valve-Request-Type": "routeAction",
                "X-Valve-Action-Type": "4OPT6VBA:Search",
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam listings search invalid JSON: {response.text}") from exc
        success = payload.get("success")
        if success not in (1, True, None):
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return _normalize_market_search_payload(
            payload,
            market_hash_name=market_hash_name,
            limit=max(1, int(count)) if count else None,
        )

    def price_overview(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        country: str = "CN",
        currency: int = 23,
    ) -> dict[str, Any]:
        params = {
            "country": country,
            "currency": currency,
            "appid": app_id,
            "market_hash_name": market_hash_name,
        }
        response = self._request(
            "GET",
            "/market/priceoverview/",
            params=params,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam priceoverview invalid JSON: {response.text}") from exc
        if payload.get("success") not in (1, True):
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return payload

    def order_book(
        self,
        *,
        app_id: int,
        market_hash_name: str,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/market/orderbook",
            params={
                "q": "Load",
                "qp": json.dumps([app_id, market_hash_name], separators=(",", ":")),
            },
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": (
                    f"{self.base_url}/market/listings/{app_id}/"
                    f"{quote(market_hash_name, safe='')}"
                ),
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                ),
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam orderbook invalid JSON: {response.text}") from exc
        if payload.get("success") not in (None, 1, True):
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return payload

    def price_history(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        currency: int = 23,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/market/pricehistory/",
            params={
                "appid": app_id,
                "market_hash_name": market_hash_name,
                "currency": currency,
            },
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": (
                    f"{self.base_url}/market/listings/{app_id}/"
                    f"{quote(market_hash_name, safe='')}"
                ),
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                ),
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam pricehistory invalid JSON: {response.text}") from exc
        if payload.get("success") not in (None, 1, True):
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return payload

    def my_listings(self, *, start: int = 0, count: int = 100) -> dict[str, Any]:
        params = {"start": start, "count": count, "norender": 1}
        response = self._request("GET", "/market/mylistings", params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam mylistings invalid JSON: {response.text}") from exc

    def market_history(self, *, start: int = 0, count: int = 100) -> dict[str, Any]:
        params = {"query": "", "start": start, "count": count, "norender": 1}
        response = self._request("GET", "/market/myhistory/render/", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam market history invalid JSON: {response.text}") from exc
        if payload.get("success") not in (1, True, None):
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return payload

    def find_purchase_receipt(
        self,
        *,
        market_hash_name: str,
        expected_total: float | None = None,
        earliest_time: int | float | None = None,
        total_tolerance: float = 0.02,
        count: int = 100,
        max_pages: int = 2,
    ) -> dict[str, Any] | None:
        """Find an official Steam Market purchase event (event_type=4).

        This is intentionally a narrow reconciliation helper.  It matches the
        exact market hash name and, when supplied, the paid total and earliest
        event time so an unrelated historical purchase cannot prove a current
        profit-trade buy.
        """

        target_name = str(market_hash_name or "").strip()
        if not target_name:
            return None
        expected_total_cents = (
            int(round(float(expected_total) * 100.0))
            if expected_total is not None
            else None
        )
        tolerance_cents = max(0, int(round(float(total_tolerance) * 100.0)))
        earliest = int(float(earliest_time)) if earliest_time is not None else None

        def purchase_asset_row(
            assets: Any,
            purchase_asset: dict[str, Any],
        ) -> dict[str, Any]:
            if not isinstance(assets, dict):
                return {}
            app_id = str(purchase_asset.get("appid") or "730")
            app_assets = assets.get(app_id) or assets.get(_safe_int(app_id)) or {}
            if not isinstance(app_assets, dict):
                return {}
            asset_ids = {
                str(value)
                for value in (purchase_asset.get("id"), purchase_asset.get("new_id"))
                if value not in (None, "")
            }
            preferred_contexts = [
                str(value)
                for value in (
                    purchase_asset.get("new_contextid"),
                    "2",
                    purchase_asset.get("contextid"),
                )
                if value not in (None, "")
            ]
            context_items = list(app_assets.items())
            context_items.sort(
                key=lambda item: (
                    preferred_contexts.index(str(item[0]))
                    if str(item[0]) in preferred_contexts
                    else len(preferred_contexts)
                )
            )
            for _, rows in context_items:
                if not isinstance(rows, dict):
                    continue
                for key, row in rows.items():
                    if not isinstance(row, dict):
                        continue
                    row_asset_id = str(row.get("id") or key or "").strip()
                    if row_asset_id in asset_ids:
                        return row
            return {}

        start = 0
        page_size = max(1, int(count))
        for _ in range(max(1, int(max_pages))):
            payload = self.market_history(start=start, count=page_size)
            events = payload.get("events") or []
            purchases = payload.get("purchases") or {}
            assets = payload.get("assets") or {}
            if not isinstance(events, list) or not events:
                return None
            for event in events:
                if not isinstance(event, dict) or int(event.get("event_type") or 0) != 4:
                    continue
                event_time = _safe_int(event.get("time_event"))
                if earliest is not None and (event_time is None or event_time < earliest):
                    continue
                listing_id = str(event.get("listingid") or "").strip()
                purchase_id = str(event.get("purchaseid") or "").strip()
                purchase: dict[str, Any] = {}
                if isinstance(purchases, dict):
                    candidate = purchases.get(f"{listing_id}_{purchase_id}") or purchases.get(listing_id)
                    if isinstance(candidate, dict):
                        purchase = candidate
                paid_amount_cents = _safe_int(purchase.get("paid_amount")) or 0
                paid_fee_cents = _safe_int(purchase.get("paid_fee")) or 0
                paid_total_cents = paid_amount_cents + paid_fee_cents
                if (
                    expected_total_cents is not None
                    and abs(paid_total_cents - expected_total_cents) > tolerance_cents
                ):
                    continue
                purchase_asset = purchase.get("asset") if isinstance(purchase.get("asset"), dict) else {}
                asset_row = purchase_asset_row(assets, purchase_asset)
                receipt_name = str(
                    asset_row.get("market_hash_name")
                    or purchase.get("market_hash_name")
                    or purchase_asset.get("market_hash_name")
                    or ""
                ).strip()
                if receipt_name != target_name:
                    continue
                return {
                    "listingId": listing_id,
                    "purchaseId": purchase_id,
                    "timePurchased": event_time,
                    "paidAmount": round(paid_amount_cents / 100.0, 2),
                    "paidFee": round(paid_fee_cents / 100.0, 2),
                    "paidTotal": round(paid_total_cents / 100.0, 2),
                    "currencyId": purchase.get("currencyid"),
                    "marketHashName": receipt_name,
                    "assetId": str(purchase_asset.get("id") or asset_row.get("id") or "") or None,
                    "newAssetId": str(purchase_asset.get("new_id") or "") or None,
                }
            start += page_size
            total = _safe_int(payload.get("total_count"))
            if total is not None and start >= total:
                return None
        return None

    def find_sale_receipt(
        self,
        listing_id: str,
        *,
        count: int = 100,
        max_pages: int = 3,
    ) -> dict[str, Any] | None:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return None
        start = 0
        for _ in range(max(1, int(max_pages))):
            payload = self.market_history(start=start, count=count)
            events = payload.get("events") or []
            purchases = payload.get("purchases") or {}
            if not isinstance(events, list) or not events:
                return None
            for event in events:
                if not isinstance(event, dict):
                    continue
                if str(event.get("listingid") or "") != listing_id:
                    continue
                if int(event.get("event_type") or 0) != 3:
                    continue
                purchase_id = str(event.get("purchaseid") or "")
                purchase = {}
                if isinstance(purchases, dict):
                    purchase = purchases.get(f"{listing_id}_{purchase_id}") or purchases.get(listing_id) or {}
                received_amount = purchase.get("received_amount") if isinstance(purchase, dict) else None
                try:
                    received_value = float(received_amount) / 100.0 if received_amount is not None else None
                except (TypeError, ValueError):
                    received_value = None
                return {
                    "listingId": listing_id,
                    "purchaseId": purchase_id,
                    "timeSold": purchase.get("time_sold") or event.get("time_event"),
                    "receivedAmount": received_value,
                    "receivedCurrencyId": purchase.get("received_currencyid") if isinstance(purchase, dict) else None,
                }
            start += int(count)
            total = payload.get("total_count")
            if total is not None and start >= int(total):
                return None
        return None

    def find_sale_receipt_by_asset(
        self,
        asset_id: str,
        *,
        count: int = 100,
        max_pages: int = 3,
    ) -> dict[str, Any] | None:
        asset_id = str(asset_id or "").strip()
        if not asset_id:
            return None
        start = 0
        for _ in range(max(1, int(max_pages))):
            payload = self.market_history(start=start, count=count)
            events = payload.get("events") or []
            purchases = payload.get("purchases") or {}
            if not isinstance(events, list) or not events:
                return None
            for event in events:
                if not isinstance(event, dict):
                    continue
                if int(event.get("event_type") or 0) != 3:
                    continue
                listing_id = str(event.get("listingid") or "")
                purchase_id = str(event.get("purchaseid") or "")
                purchase = {}
                if isinstance(purchases, dict):
                    purchase = purchases.get(f"{listing_id}_{purchase_id}") or purchases.get(listing_id) or {}
                purchase_asset = purchase.get("asset") if isinstance(purchase, dict) else None
                event_asset_id = str(
                    event.get("assetid")
                    or event.get("asset_id")
                    or (event.get("asset") or {}).get("id")
                    or (purchase_asset or {}).get("id")
                    or ""
                ).strip()
                if event_asset_id != asset_id:
                    continue
                received_amount = purchase.get("received_amount") if isinstance(purchase, dict) else None
                try:
                    received_value = float(received_amount) / 100.0 if received_amount is not None else None
                except (TypeError, ValueError):
                    received_value = None
                return {
                    "listingId": listing_id,
                    "purchaseId": purchase_id,
                    "timeSold": purchase.get("time_sold") or event.get("time_event"),
                    "receivedAmount": received_value,
                    "receivedCurrencyId": purchase.get("received_currencyid") if isinstance(purchase, dict) else None,
                }
            start += int(count)
            total = payload.get("total_count")
            if total is not None and start >= int(total):
                return None
        return None

    def _parse_my_listing_rows(self, payload: dict[str, Any], listings_raw: Any) -> list[SteamListing]:
        assets: dict[str, Any] = payload.get("assets") or {}
        if isinstance(listings_raw, dict):
            listing_items = list(listings_raw.items())
        elif isinstance(listings_raw, list):
            listing_items = []
            for entry in listings_raw:
                if not isinstance(entry, dict):
                    continue
                listing_id = (
                    entry.get("listingid")
                    or entry.get("listing_id")
                    or entry.get("id")
                    or ""
                )
                listing_items.append((str(listing_id), entry))
        else:
            listing_items = []
        parsed: list[SteamListing] = []
        for listing_id, listing in listing_items:
            asset = None
            asset_id = None
            mhn = None
            if isinstance(listing, dict):
                asset_info = listing.get("asset") or {}
                asset_id = str(asset_info.get("id") or listing.get("assetid") or listing.get("asset_id") or "") or None
                mhn = str(
                    listing.get("asset", {}).get("market_hash_name")
                    or listing.get("market_hash_name")
                    or listing.get("hash_name")
                    or ""
                ) or None
                if not mhn and asset_id:
                    asset = assets.get(str(listing.get("appid") or 730), {}).get(
                        str(listing.get("contextid") or 2),
                        {},
                    ).get(asset_id)
                    if isinstance(asset, dict):
                        mhn = str(asset.get("market_hash_name") or "") or None
            price = None
            if isinstance(listing, dict):
                price = listing.get("price") or listing.get("price_amount")
                try:
                    price = float(price) / 100 if price is not None else None
                except (TypeError, ValueError):
                    price = None
            status = listing.get("status") if isinstance(listing, dict) else None
            parsed.append(
                SteamListing(
                    listing_id=str(listing_id),
                    asset_id=asset_id,
                    market_hash_name=mhn,
                    price=price,
                    status=status if isinstance(status, int) else None,
                )
            )
        return parsed

    def list_active_listings(self, *, start: int = 0, count: int = 100) -> list[SteamListing]:
        payload = self.my_listings(start=start, count=count)
        return self._parse_my_listing_rows(payload, payload.get("listings") or {})

    def list_confirmation_pending_listings(self, *, start: int = 0, count: int = 100) -> list[SteamListing]:
        payload = self.my_listings(start=start, count=count)
        return self._parse_my_listing_rows(payload, payload.get("listings_to_confirm") or [])

    def fetch_confirmations(self) -> list[dict[str, Any]]:
        if not self.identity_secret or not self.device_id:
            raise SteamMarketError("missing identity_secret or device_id")
        now = int(time.time())
        key = _steam_confirmation_key(self.identity_secret, "conf", now)
        params = {
            "p": self.device_id,
            "a": self.steam_id64,
            "k": key,
            "t": now,
            "m": "react",
            "tag": "conf",
        }
        response = self._request("GET", "/mobileconf/getlist", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam mobileconf invalid JSON: {response.text}") from exc
        if not payload.get("success"):
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return payload.get("conf") or []

    def _allow_confirmations(self, confirmations: Iterable[dict[str, Any]]) -> int:
        selected = list(confirmations)
        if not selected:
            return 0
        if not self.identity_secret or not self.device_id:
            raise SteamMarketError("missing identity_secret or device_id")

        now = int(time.time())
        key = _steam_confirmation_key(self.identity_secret, "accept", now)
        params = {
            "p": self.device_id,
            "a": self.steam_id64,
            "k": key,
            "t": now,
            "m": "react",
            "tag": "accept",
            "op": "allow",
        }
        multipart: list[tuple[str, tuple[None, str]]] = []
        for conf in selected:
            multipart.append(("cid[]", (None, str(conf.get("id")))))
            multipart.append(("ck[]", (None, str(conf.get("nonce")))))
        url = f"{self.base_url}/mobileconf/multiajaxop"
        response = self._session.post(url, params=params, files=multipart, timeout=self.timeout)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam confirm invalid JSON: {response.text}") from exc
        if not payload.get("success"):
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return len(selected)

    def _allow_confirmation_creator_id(self, confirmation_id: str, *, action: str) -> int:
        expected = str(confirmation_id or "").strip()
        if not expected:
            raise SteamMarketError(f"{action} confirmation_id is empty")
        confirmations = [
            confirmation
            for confirmation in self.fetch_confirmations()
            if str(confirmation.get("creator_id") or "").strip() == expected
        ]
        if len(confirmations) != 1:
            raise SteamMarketError(f"{action} confirmation not found or ambiguous: {expected}")
        return self._allow_confirmations(confirmations)

    @staticmethod
    def _normalize_id_set(values: Iterable[Any] | None) -> set[str]:
        if values is None:
            return set()
        return {str(value or "").strip() for value in values if str(value or "").strip()}

    @staticmethod
    def _confirmation_listing_id(confirmation: dict[str, Any]) -> str | None:
        for key in ("creator_id", "creatorid", "listing_id", "listingid"):
            value = str(confirmation.get(key) or "").strip()
            if value:
                return value
        return None

    def confirm_listing_ids(self, listing_ids: Iterable[Any]) -> int:
        expected_listing_ids = self._normalize_id_set(listing_ids)
        if not expected_listing_ids:
            return 0
        confirmations = self.fetch_confirmations()
        selected = [
            confirmation
            for confirmation in confirmations
            if self._confirmation_listing_id(confirmation) in expected_listing_ids
        ]
        return self._allow_confirmations(selected)

    def confirm_listing_assets(
        self,
        *,
        asset_ids: Iterable[Any],
        listing_ids: Iterable[Any] | None = None,
    ) -> int:
        expected_asset_ids = self._normalize_id_set(asset_ids)
        if not expected_asset_ids:
            return 0
        expected_listing_ids = self._normalize_id_set(listing_ids)
        pending_listing_ids: set[str] = set()
        for listing in self.list_confirmation_pending_listings():
            pending_asset_id = str(getattr(listing, "asset_id", "") or "").strip()
            pending_listing_id = str(getattr(listing, "listing_id", "") or "").strip()
            if not pending_asset_id or not pending_listing_id:
                continue
            if pending_asset_id not in expected_asset_ids:
                continue
            if expected_listing_ids and pending_listing_id not in expected_listing_ids:
                continue
            pending_listing_ids.add(pending_listing_id)
        return self.confirm_listing_ids(pending_listing_ids)

    def confirm_all(self) -> int:
        raise SteamMarketError(
            "refusing to confirm all Steam confirmations; use confirm_listing_assets with explicit asset_id targets"
        )
