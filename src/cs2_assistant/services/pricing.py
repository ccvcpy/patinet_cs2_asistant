from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from cs2_assistant.clients.steam_market import SteamMarketClient, SteamMarketError
from cs2_assistant.utils import safe_float


@dataclass(slots=True)
class PricingDecision:
    list_price: float
    wall_price: float | None
    reason: str


_STEAM_PRICE_TTL = 60.0
_price_cache: dict[tuple[Any, ...], tuple[float, PricingDecision]] = {}
_cache_lock = threading.Lock()


def clear_pricing_cache(market_hash_name: str | None = None) -> None:
    with _cache_lock:
        if market_hash_name is None:
            _price_cache.clear()
            return
        keys_to_remove = [key for key in _price_cache if key[0] == market_hash_name]
        for key in keys_to_remove:
            _price_cache.pop(key, None)


def get_pricing_cache_snapshot() -> list[dict[str, Any]]:
    now = time.time()
    snapshot: list[dict[str, Any]] = []
    with _cache_lock:
        for key, (ts, decision) in _price_cache.items():
            market_hash_name = str(key[0])
            snapshot.append(
                {
                    "market_hash_name": market_hash_name,
                    "age_sec": max(0.0, now - ts),
                    "list_price": decision.list_price,
                    "wall_price": decision.wall_price,
                    "reason": decision.reason,
                }
            )
    snapshot.sort(key=lambda row: (row["market_hash_name"], row["age_sec"]))
    return snapshot


def _parse_price_text(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    return safe_float(cleaned)


def _normalize_orderbook_rows(value: Any) -> list[list[Any]]:
    if not isinstance(value, list):
        return []
    if not value:
        return []
    if all(isinstance(row, list) for row in value):
        return value
    rows: list[list[Any]] = []
    index = 0
    while index + 1 < len(value):
        rows.append([value[index], value[index + 1]])
        index += 2
    return rows


def _extract_orderbook_sell_rows(payload: dict[str, Any]) -> list[list[Any]]:
    for key in (
        "rgCompactSellOrders",
        "sell_orderbook",
        "sell_orders",
        "sell",
        "asks",
        "ask",
        "sell_order_graph",
    ):
        rows = _normalize_orderbook_rows(payload.get(key))
        if rows:
            return rows
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_orderbook_sell_rows(data)
    return []


def _parse_orderbook_price(value: Any) -> float | None:
    price = _parse_price_text(str(value))
    if price is None:
        return None
    # Steam's new orderbook preload uses integer micro-units:
    # 237400 -> CNY 2.374. Keep already-decimal values intact for tests
    # and for any future JSON shape that returns display prices.
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        if price >= 10000:
            return price / 100000.0
        if price >= 100:
            return price / 100.0
    return price


def choose_orderbook_price(
    payload: dict[str, Any],
    *,
    wall_min_count: int = 20,
    price_offset: float = 0.01,
    min_price: float | None = None,
) -> PricingDecision | None:
    rows = _extract_orderbook_sell_rows(payload)
    if not rows:
        return None

    wall_price: float | None = None
    cumulative = 0.0
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        price = _parse_orderbook_price(row[0])
        count = safe_float(row[1])
        if price is None or count is None:
            continue
        cumulative += count
        if cumulative >= wall_min_count:
            wall_price = price
            break

    if wall_price is None:
        for row in rows:
            if not isinstance(row, list) or not row:
                continue
            wall_price = _parse_orderbook_price(row[0])
            if wall_price is not None:
                break

    if wall_price is None:
        return None

    list_price = max(0.01, wall_price - price_offset)
    if min_price is not None and list_price < min_price:
        list_price = min_price

    return PricingDecision(
        list_price=list_price,
        wall_price=wall_price,
        reason="orderbook_wall",
    )


def fetch_listing_price(
    client: SteamMarketClient,
    *,
    app_id: int,
    market_hash_name: str,
    wall_min_count: int = 20,
    price_offset: float = 0.01,
    min_price: float | None = None,
    country: str = "CN",
    language: str = "schinese",
    currency: int = 23,
    force_refresh: bool = False,
    cache_ttl: float = _STEAM_PRICE_TTL,
    debug: bool = False,
) -> PricingDecision | None:
    cache_key = (
        market_hash_name,
        app_id,
        country,
        language,
        currency,
        wall_min_count,
        price_offset,
        min_price,
    )
    now = time.time()
    if not force_refresh:
        with _cache_lock:
            cached = _price_cache.get(cache_key)
        if cached:
            ts, decision = cached
            if now - ts <= cache_ttl:
                return decision

    try:
        orderbook = client.order_book(
            app_id=app_id,
            market_hash_name=market_hash_name,
        )
    except SteamMarketError:
        if debug:
            raise
        return None

    decision = choose_orderbook_price(
        orderbook,
        wall_min_count=wall_min_count,
        price_offset=price_offset,
        min_price=min_price,
    )

    if decision is None:
        return None
    with _cache_lock:
        _price_cache[cache_key] = (now, decision)
    return decision
