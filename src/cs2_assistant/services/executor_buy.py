from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from cs2_assistant.clients import C5GameClient, C5GameError, SteamMarketClient
from cs2_assistant.utils import safe_float


@dataclass(slots=True)
class RebuyResult:
    success: bool
    skipped: bool
    reason: str
    actual_price: float | None = None
    max_price: float | None = None
    steam_price_now: float | None = None
    steam_reference_price: float | None = None
    listing_ratio_now: float | None = None
    payload: dict[str, Any] | None = None
    out_trade_no: str | None = None


def _parse_c5_error(exc: Exception) -> dict[str, Any] | None:
    message = str(exc).strip()
    if not message.startswith("{"):
        return None
    try:
        payload = json.loads(message)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def is_retryable_c5_network_error(exc: Exception) -> bool:
    return str(exc).strip().startswith("C5 request failed:")


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
    steam_net_factor: float = 0.869,
    guadao_max_listing_ratio: float | None = None,
    trade_url: str | None = None,
    use_live_price_as_max: bool = False,
    max_price_override: float | None = None,
) -> RebuyResult:
    try:
        live_price = fetch_c5_price(client, market_hash_name, app_id)
    except C5GameError as exc:
        if is_retryable_c5_network_error(exc):
            return RebuyResult(
                False,
                True,
                "c5_network_error",
                payload={"error": str(exc)},
            )
        return RebuyResult(False, False, f"c5_api_error: {exc}", payload=_parse_c5_error(exc))
    except Exception as exc:
        return RebuyResult(False, False, f"c5_api_error: {exc}", payload={"error": str(exc)})
    if live_price is None:
        return RebuyResult(False, False, "missing_price")

    steam_reference_price = (
        float(expected_steam_list_price)
        if expected_steam_list_price is not None and expected_steam_list_price > 0
        else None
    )
    # ratio 用实际卖出时记录的 Steam 挂价计算：C5补仓价 / (Steam卖出价 * 税后系数)
    steam_price_now = steam_reference_price
    listing_ratio_now = None
    if steam_reference_price and steam_reference_price > 0:
        listing_ratio_now = live_price / (steam_reference_price * steam_net_factor)

    pricing_steam_list_price = expected_steam_list_price
    if max_price_override is not None and float(max_price_override) > 0:
        max_price = float(max_price_override)
    elif use_live_price_as_max:
        max_price = float(live_price) * (1.0 + float(tolerance_pct) / 100.0)
    elif (
        guadao_max_listing_ratio is not None
        and pricing_steam_list_price is not None
        and pricing_steam_list_price > 0
    ):
        max_price = float(pricing_steam_list_price) * float(steam_net_factor) * float(guadao_max_listing_ratio)
    else:
        max_price = float(expected_price) * (1.0 + float(tolerance_pct) / 100.0)

    if guadao_max_listing_ratio is not None and listing_ratio_now is not None:
        if listing_ratio_now > guadao_max_listing_ratio:
            return RebuyResult(
                False,
                True,
                "ratio_no_longer_profitable",
                actual_price=live_price,
                max_price=max_price,
                steam_price_now=steam_price_now,
                steam_reference_price=steam_reference_price,
                listing_ratio_now=listing_ratio_now,
                out_trade_no=None,
            )

    if dry_run:
        return RebuyResult(
            True,
            True,
            "dry_run",
            actual_price=live_price,
            max_price=max_price,
            steam_price_now=steam_price_now,
            steam_reference_price=steam_reference_price,
            listing_ratio_now=listing_ratio_now,
            out_trade_no=None,
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
        if payload is None and is_retryable_c5_network_error(exc):
            return RebuyResult(
                False,
                True,
                "c5_network_error",
                actual_price=live_price,
                max_price=max_price,
                steam_price_now=steam_price_now,
                steam_reference_price=steam_reference_price,
                listing_ratio_now=listing_ratio_now,
                payload={"error": str(exc)},
                out_trade_no=out_trade_no,
            )
        if payload and payload.get("errorCode") in {1317, 1014452}:
            return RebuyResult(
                False,
                True,
                "no_matching_listing",
                actual_price=live_price,
                max_price=max_price,
                steam_price_now=steam_price_now,
                steam_reference_price=steam_reference_price,
                listing_ratio_now=listing_ratio_now,
                payload=payload,
                out_trade_no=out_trade_no,
            )
        return RebuyResult(
            False,
            False,
            f"c5_api_error: {exc}",
            actual_price=live_price,
            max_price=max_price,
            steam_price_now=steam_price_now,
            steam_reference_price=steam_reference_price,
            listing_ratio_now=listing_ratio_now,
            payload=payload,
            out_trade_no=out_trade_no,
        )
    except Exception as exc:
        return RebuyResult(
            False,
            False,
            f"c5_api_error: {exc}",
            actual_price=live_price,
            max_price=max_price,
            steam_price_now=steam_price_now,
            steam_reference_price=steam_reference_price,
            listing_ratio_now=listing_ratio_now,
            out_trade_no=out_trade_no,
        )
    return RebuyResult(
        True,
        False,
        "ok",
        actual_price=live_price,
        max_price=max_price,
        steam_price_now=steam_price_now,
        steam_reference_price=steam_reference_price,
        listing_ratio_now=listing_ratio_now,
        payload=payload,
        out_trade_no=out_trade_no,
    )
