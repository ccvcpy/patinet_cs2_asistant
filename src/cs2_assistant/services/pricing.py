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
_STEAM_NAMEID_TTL = 86400.0
_price_cache: dict[tuple[Any, ...], tuple[float, PricingDecision]] = {}
_nameid_cache: dict[tuple[Any, ...], tuple[float, str]] = {}
_cache_lock = threading.Lock()


def clear_pricing_cache(market_hash_name: str | None = None) -> None:
    with _cache_lock:
        if market_hash_name is None:
            _price_cache.clear()
            _nameid_cache.clear()
            return
        keys_to_remove = [key for key in _price_cache if key[0] == market_hash_name]
        for key in keys_to_remove:
            _price_cache.pop(key, None)
        keys_to_remove = [key for key in _nameid_cache if key[0] == market_hash_name]
        for key in keys_to_remove:
            _nameid_cache.pop(key, None)


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


def _extract_sell_order_graph(payload: dict[str, Any]) -> list[list[Any]]:
    graph = payload.get("sell_order_graph")
    if isinstance(graph, list):
        return graph
    return []


def choose_list_price(
    payload: dict[str, Any],
    *,
    wall_min_count: int = 20,
    price_offset: float = 0.01,
    min_price: float | None = None,
) -> PricingDecision | None:
    graph = _extract_sell_order_graph(payload)
    if not graph:
        return None

    wall_price: float | None = None
    for row in graph:
        if not isinstance(row, list) or len(row) < 2:
            continue
        price = _parse_price_text(str(row[0]))
        if price is None:
            continue
        cumulative = None
        if len(row) >= 3:
            cumulative = safe_float(row[2])
        if cumulative is None:
            cumulative = safe_float(row[1])
        if cumulative is None:
            continue
        if cumulative >= wall_min_count:
            wall_price = price
            break

    if wall_price is None:
        last_row = graph[-1]
        price = _parse_price_text(str(last_row[0])) if isinstance(last_row, list) else None
        if price is None:
            return None
        wall_price = price

    list_price = max(0.01, wall_price - price_offset)
    if min_price is not None and list_price < min_price:
        list_price = min_price

    return PricingDecision(
        list_price=list_price,
        wall_price=wall_price,
        reason="wall",
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
        name_key = (market_hash_name, app_id)
        item_nameid = None
        with _cache_lock:
            cached_nameid = _nameid_cache.get(name_key)
        if cached_nameid:
            ts, value = cached_nameid
            if now - ts <= _STEAM_NAMEID_TTL:
                item_nameid = value
        if not item_nameid:
            item_nameid = client.get_item_nameid(app_id=app_id, market_hash_name=market_hash_name)
            if item_nameid:
                with _cache_lock:
                    _nameid_cache[name_key] = (now, item_nameid)
        payload = client.item_orders_histogram(
            item_nameid=item_nameid,
            country=country,
            language=language,
            currency=currency,
        )
    except SteamMarketError:
        if debug:
            raise
        return None
    decision = choose_list_price(
        payload,
        wall_min_count=wall_min_count,
        price_offset=price_offset,
        min_price=min_price,
    )
    if decision is None:
        return None
    with _cache_lock:
        _price_cache[cache_key] = (now, decision)
    return decision
