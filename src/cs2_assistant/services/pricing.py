from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cs2_assistant.clients.steam_market import SteamMarketClient, SteamMarketError
from cs2_assistant.utils import safe_float


@dataclass(slots=True)
class PricingDecision:
    list_price: float
    wall_price: float | None
    reason: str


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
) -> PricingDecision | None:
    try:
        item_nameid = client.get_item_nameid(app_id=app_id, market_hash_name=market_hash_name)
        payload = client.item_orders_histogram(
            item_nameid=item_nameid,
            country=country,
            language=language,
            currency=currency,
        )
    except SteamMarketError:
        return None
    return choose_list_price(
        payload,
        wall_min_count=wall_min_count,
        price_offset=price_offset,
        min_price=min_price,
    )
