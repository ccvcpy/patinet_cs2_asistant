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
        cookies: str,
        steam_id64: str | None = None,
        identity_secret: str | None = None,
        device_id: str | None = None,
        base_url: str = "https://steamcommunity.com",
        timeout: int = 30,
    ) -> None:
        if not cookies:
            raise SteamMarketError("missing Steam cookies")

        self.base_url = base_url.rstrip("/")
        self.identity_secret = _normalize_identity_secret(identity_secret) if identity_secret else identity_secret
        self.device_id = device_id
        self.timeout = timeout

        self._session = requests.Session()
        cookie_map = _parse_cookie_string(cookies)
        self._session.cookies.update(cookie_map)
        self._session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Steam Mobile/10372190 CFNetwork/3860.100.1 Darwin/25.0.0",
            }
        )
        self._sessionid = _extract_sessionid(cookie_map)
        if not self._sessionid:
            raise SteamMarketError("Steam cookies missing sessionid")
        resolved_id64 = steam_id64 or _extract_steam_id64(cookie_map)
        if not resolved_id64:
            raise SteamMarketError("missing Steam ID64 in cookies")
        self.steam_id64 = resolved_id64

    @property
    def sessionid(self) -> str:
        return self._sessionid

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        merged_headers = dict(headers or {})
        response = self._session.request(
            method=method,
            url=url,
            params=params,
            data=data,
            headers=merged_headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response

    def sell_item(
        self,
        *,
        app_id: int,
        context_id: str,
        asset_id: str,
        price: float,
        quantity: int = 1,
    ) -> dict[str, Any]:
        if price <= 0:
            raise SteamMarketError("price must be positive")
        if quantity <= 0:
            raise SteamMarketError("quantity must be positive")

        price_cents = int(round(price * 100))
        data = {
            "sessionid": self.sessionid,
            "appid": app_id,
            "contextid": context_id,
            "assetid": asset_id,
            "amount": quantity,
            "price": price_cents,
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

    def get_item_nameid(self, *, app_id: int, market_hash_name: str) -> str:
        encoded_name = quote(market_hash_name)
        response = self._request("GET", f"/market/listings/{app_id}/{encoded_name}")
        match = re.search(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)", response.text)
        if match:
            return match.group(1)
        match = re.search(r"Market_LoadOrderSpread\(\s*(\d+)\s*,", response.text)
        if match:
            return match.group(1)
        match = re.search(r"item_nameid\s*[:=]\s*\"?(\d+)\"?", response.text)
        if match:
            return match.group(1)
        raise SteamMarketError("cannot find item_nameid")

    def item_orders_histogram(
        self,
        *,
        item_nameid: str,
        country: str = "CN",
        language: str = "schinese",
        currency: int = 23,
    ) -> dict[str, Any]:
        params = {
            "country": country,
            "language": language,
            "currency": currency,
            "item_nameid": item_nameid,
        }
        response = self._request("GET", "/market/itemordershistogram", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamMarketError(f"Steam histogram invalid JSON: {response.text}") from exc
        if payload.get("success") != 1:
            raise SteamMarketError(json.dumps(payload, ensure_ascii=False))
        return payload

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
