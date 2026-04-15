from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from cs2_assistant.clients import C5GameClient, SteamMarketClient
from cs2_assistant.services.pricing import fetch_listing_price
from cs2_assistant.utils import safe_float


@dataclass(slots=True)
class RebuyResult:
    success: bool
    skipped: bool
    reason: str
    actual_price: float | None = None
    steam_price_now: float | None = None
    listing_ratio_now: float | None = None
    payload: dict[str, Any] | None = None


def fetch_c5_price(client: C5GameClient, market_hash_name: str, app_id: int) -> float | None:
    data = client.price_batch([market_hash_name], app_id=app_id)
    payload = data.get(market_hash_name)
    if not isinstance(payload, dict):
        return None
    return safe_float(payload.get("price"))


def execute_rebuy(
    *,
    client: C5GameClient,
    steam_client: SteamMarketClient | None,
    market_hash_name: str,
    expected_price: float,
    expected_steam_list_price: float | None,
    app_id: int,
    tolerance_pct: float,
    dry_run: bool,
    verify_steam: bool = False,
    steam_drop_tolerance_pct: float = 5.0,
    force_refresh: bool = True,
    pricing_kwargs: dict[str, Any] | None = None,
    steam_net_factor: float = 0.869,
    guadao_max_listing_ratio: float | None = None,
) -> RebuyResult:
    live_price = fetch_c5_price(client, market_hash_name, app_id)
    if live_price is None:
        return RebuyResult(False, False, "missing_price")

    max_price = expected_price * (1.0 + max(0.0, tolerance_pct) / 100.0)
    if live_price > max_price:
        return RebuyResult(False, True, "price_too_high", actual_price=live_price)

    steam_price_now = None
    listing_ratio_now = None
    if verify_steam:
        if not steam_client:
            return RebuyResult(False, True, "steam_price_unavailable", actual_price=live_price)
        kwargs = dict(pricing_kwargs or {})
        kwargs.setdefault("force_refresh", force_refresh)
        decision = fetch_listing_price(
            steam_client,
            app_id=app_id,
            market_hash_name=market_hash_name,
            **kwargs,
        )
        if decision is None:
            return RebuyResult(False, True, "steam_price_unavailable", actual_price=live_price)
        steam_price_now = decision.list_price
        if steam_price_now and steam_price_now > 0:
            listing_ratio_now = live_price / (steam_price_now * steam_net_factor)
        if (
            expected_steam_list_price
            and expected_steam_list_price > 0
            and steam_price_now is not None
        ):
            drop_pct = (expected_steam_list_price - steam_price_now) / expected_steam_list_price * 100.0
            if drop_pct > steam_drop_tolerance_pct:
                return RebuyResult(
                    False,
                    True,
                    "steam_crashed",
                    actual_price=live_price,
                    steam_price_now=steam_price_now,
                    listing_ratio_now=listing_ratio_now,
                )
        if (
            guadao_max_listing_ratio is not None
            and listing_ratio_now is not None
            and listing_ratio_now > guadao_max_listing_ratio
        ):
            return RebuyResult(
                False,
                True,
                "ratio_no_longer_profitable",
                actual_price=live_price,
                steam_price_now=steam_price_now,
                listing_ratio_now=listing_ratio_now,
            )

    if dry_run:
        return RebuyResult(
            True,
            True,
            "dry_run",
            actual_price=live_price,
            steam_price_now=steam_price_now,
            listing_ratio_now=listing_ratio_now,
        )

    out_trade_no = uuid.uuid4().hex
    payload = client.quick_buy(
        app_id=app_id,
        market_hash_name=market_hash_name,
        max_price=max_price,
        out_trade_no=out_trade_no,
    )
    return RebuyResult(
        True,
        False,
        "ok",
        actual_price=live_price,
        steam_price_now=steam_price_now,
        listing_ratio_now=listing_ratio_now,
        payload=payload,
    )
