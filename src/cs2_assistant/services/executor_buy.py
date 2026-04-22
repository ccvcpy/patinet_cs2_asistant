from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from cs2_assistant.clients import C5GameClient, C5GameError, SteamMarketClient
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


def _parse_c5_error(exc: Exception) -> dict[str, Any] | None:
    message = str(exc).strip()
    if not message.startswith("{"):
        return None
    try:
        payload = json.loads(message)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


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
    force_refresh: bool = True,
    pricing_kwargs: dict[str, Any] | None = None,
    steam_net_factor: float = 0.869,
    guadao_max_listing_ratio: float | None = None,
    trade_url: str | None = None,
) -> RebuyResult:
    live_price = fetch_c5_price(client, market_hash_name, app_id)
    if live_price is None:
        return RebuyResult(False, False, "missing_price")

    steam_price_now = None
    # ratio 用实际卖出价计算：C5补仓价 / (Steam实际卖出价 × 税后系数)
    listing_ratio_now = None
    if expected_steam_list_price and expected_steam_list_price > 0:
        listing_ratio_now = live_price / (expected_steam_list_price * steam_net_factor)

    if guadao_max_listing_ratio is not None and listing_ratio_now is not None:
        if listing_ratio_now > guadao_max_listing_ratio:
            return RebuyResult(
                False,
                True,
                "ratio_no_longer_profitable",
                actual_price=live_price,
                steam_price_now=steam_price_now,
                listing_ratio_now=listing_ratio_now,
            )

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

    pricing_steam_list_price = steam_price_now if steam_price_now and steam_price_now > 0 else expected_steam_list_price
    if (
        guadao_max_listing_ratio is not None
        and pricing_steam_list_price is not None
        and pricing_steam_list_price > 0
    ):
        max_price = float(pricing_steam_list_price) * float(steam_net_factor) * float(guadao_max_listing_ratio)
    else:
        max_price = float(expected_price) * (1.0 + float(tolerance_pct) / 100.0)

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
    try:
        payload = client.quick_buy(
            app_id=app_id,
            market_hash_name=market_hash_name,
            max_price=max_price,
            low_price=1,
            out_trade_no=out_trade_no,
            trade_url=trade_url,
        )
    except C5GameError as exc:
        payload = _parse_c5_error(exc)
        if payload and payload.get("errorCode") == 1317:
            return RebuyResult(
                False,
                True,
                "no_matching_listing",
                actual_price=live_price,
                steam_price_now=steam_price_now,
                listing_ratio_now=listing_ratio_now,
                payload=payload,
            )
        return RebuyResult(
            False,
            False,
            f"c5_api_error: {exc}",
            actual_price=live_price,
            steam_price_now=steam_price_now,
            listing_ratio_now=listing_ratio_now,
            payload=payload,
        )
    except Exception as exc:
        return RebuyResult(
            False,
            False,
            f"c5_api_error: {exc}",
            actual_price=live_price,
            steam_price_now=steam_price_now,
            listing_ratio_now=listing_ratio_now,
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
