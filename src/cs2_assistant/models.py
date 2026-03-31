from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CatalogItem:
    market_hash_name: str
    name_cn: str
    c5_item_id: str | None = None
    steam_item_id: str | None = None
    raw_json: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketState:
    market_hash_name: str
    name_cn: str | None = None
    c5_sell_price: float | None = None
    c5_sell_count: int | None = None
    c5_bid_price: float | None = None
    c5_bid_count: int | None = None
    c5_item_id: str | None = None
    c5_website: str | None = None
    steam_sell_price: float | None = None
    steam_sell_count: int | None = None
    steam_bid_price: float | None = None
    steam_bid_count: int | None = None
    c5_price_source: str | None = None
    steam_price_source: str | None = None
    ratio: float | None = None
    raw_json: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BasketState:
    name: str
    total_value: float
    components: list[dict[str, Any]]


@dataclass(slots=True)
class TriggeredAlert:
    rule_id: int
    target_type: str
    target_key: str
    metric: str
    observed_value: float
    threshold: float
    message: str


@dataclass(slots=True)
class NotificationMessage:
    title: str
    body: str
