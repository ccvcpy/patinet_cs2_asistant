from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

import requests

from cs2_assistant.accounts import AccountStore
from cs2_assistant.accounts.steam_auth import try_steam_auto_relogin
from cs2_assistant.config import PROJECT_ROOT


class SteamMarketError(RuntimeError):
    pass


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
        data: dict[str, Any] | None = None,
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
        seller_net_cents = int(round(price * steam_net_factor * 100))
        data = {
            "sessionid": self.sessionid,
            "appid": app_id,
            "contextid": context_id,
            "assetid": asset_id,
            "amount": quantity,
            "price": seller_net_cents,
        }
        response = self._request(
            "POST",
            "/market/sellitem/",
            data=data,
            headers={"Referer": f"{self.base_url}/market"},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam sellitem invalid JSON: {response.text}") from exc
        if payload.get("success") != 1:
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return payload

    def buy_listing(
        self,
        *,
        listing_id: str,
        app_id: int,
        subtotal: int,
        fee: int,
        total: int,
    ) -> dict[str, Any]:
        if not listing_id:
            raise SteamMarketError("listing_id is required")
        if subtotal < 0 or fee < 0 or total <= 0:
            raise SteamMarketError("subtotal, fee, total must be valid cents values")

        data = {
            "sessionid": self.sessionid,
            "currency": 23,
            "subtotal": int(subtotal),
            "fee": int(fee),
            "total": int(total),
            "quantity": 1,
        }
        response = self._request(
            "POST",
            f"/market/buylisting/{listing_id}",
            data=data,
            headers={"Referer": f"{self.base_url}/market/listings/{app_id}"},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam buylisting invalid JSON: {response.text}") from exc
        wallet_info = payload.get("wallet_info") or {}
        success = wallet_info.get("success")
        if success not in (1, True):
            message = payload.get("message") or wallet_info.get("message") or payload
            raise SteamMarketError(json.dumps(message, ensure_ascii=False))
        return payload

    def search_listings(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        start: int = 0,
        count: int = 10,
    ) -> dict[str, Any]:
        encoded_name = quote(market_hash_name, safe="")
        params = {
            "start": start,
            "count": count,
            "currency": 23,
            "language": "schinese",
            "country": "CN",
            "norender": 1,
        }
        response = self._request(
            "GET",
            f"/market/listings/{app_id}/{encoded_name}/render/",
            params=params,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam listings render invalid JSON: {response.text}") from exc
        success = payload.get("success")
        if success not in (1, True, None):
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return payload

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

    def my_listings(self, *, start: int = 0, count: int = 100) -> dict[str, Any]:
        params = {"start": start, "count": count, "norender": 1}
        response = self._request("GET", "/market/mylistings", params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam mylistings invalid JSON: {response.text}") from exc

    def list_active_listings(self, *, start: int = 0, count: int = 100) -> list[SteamListing]:
        payload = self.my_listings(start=start, count=count)
        listings_raw = payload.get("listings") or {}
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

    def confirm_all(self) -> int:
        confirmations = self.fetch_confirmations()
        if not confirmations:
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
        for conf in confirmations:
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
        return len(confirmations)
