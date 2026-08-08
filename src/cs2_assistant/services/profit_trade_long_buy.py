from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Iterable

from cs2_assistant.models import StrategyConfig


LONG_BUY_LIVE_STATES = frozenset(
    {
        "creating",
        "active",
        "partial",
        "cancel_pending",
        "terminal_uncertain",
    }
)
LONG_BUY_MUTABLE_STATES = frozenset({"active", "partial"})
LONG_BUY_TERMINAL_STATES = frozenset(
    {
        "filled",
        "auto_cancelled",
        "cancelled",
        "failed",
    }
)
LONG_BUY_PREVIOUS_PRICE_EXCLUSION_SECONDS = 30


def price_to_cents(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(Decimal(str(number)).scaleb(2).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_price(value: Any) -> float | None:
    try:
        cents = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if cents <= 0:
        return None
    return round(cents / 100.0, 2)


def floor_price_to_cents(value: Any) -> int | None:
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return int((number * Decimal("100")).to_integral_value(rounding=ROUND_FLOOR))


def normalize_roi_four_decimals(value: Any) -> float | None:
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return float(number.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def remembered_own_price_cents(
    order: Any | None,
    *,
    now: datetime | None = None,
) -> list[int]:
    if order is None:
        return []
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    keys = set(order.keys()) if hasattr(order, "keys") else set()

    def value(key: str) -> Any:
        if isinstance(order, dict):
            return order.get(key)
        if key in keys:
            return order[key]
        return None

    result: list[int] = []
    current = value("bid_price_cents")
    try:
        current_cents = int(current)
    except (TypeError, ValueError, OverflowError):
        current_cents = 0
    if current_cents > 0:
        result.append(current_cents)

    previous = value("previous_bid_price_cents")
    expires_at = _parse_utc(value("previous_price_expires_at"))
    try:
        previous_cents = int(previous)
    except (TypeError, ValueError, OverflowError):
        previous_cents = 0
    if previous_cents > 0 and expires_at is not None and expires_at > now_utc:
        result.append(previous_cents)
    return list(dict.fromkeys(result))


def competitor_buy_reference(
    orderbook_snapshot: dict[str, Any] | None,
    *,
    own_price_cents: Iterable[int] = (),
) -> dict[str, Any]:
    snapshot = dict(orderbook_snapshot or {})
    currency_id = snapshot.get("currencyId")
    try:
        parsed_currency_id = int(currency_id)
    except (TypeError, ValueError, OverflowError):
        parsed_currency_id = None
    if snapshot.get("currencyValid") is False or parsed_currency_id != 23:
        return {
            "priceCents": None,
            "price": None,
            "count": None,
            "status": "currency_invalid",
            "excludedOwnPrices": [],
        }

    own_prices: set[int] = set()
    for value in own_price_cents:
        try:
            cents = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if cents > 0:
            own_prices.add(cents)
    excluded: list[int] = []
    levels = [
        row
        for row in list(snapshot.get("buyLevels") or [])[:5]
        if isinstance(row, dict)
    ]
    if not levels:
        fallback_cents = price_to_cents(snapshot.get("buyerMaxPrice"))
        if fallback_cents is None:
            return {
                "priceCents": None,
                "price": None,
                "count": None,
                "status": "missing_buy_book",
                "excludedOwnPrices": [],
            }
        levels = [
            {
                "price": snapshot.get("buyerMaxPrice"),
                "count": snapshot.get("buyerMaxCount"),
            }
        ]

    for level in levels:
        level_cents = price_to_cents(level.get("price"))
        if level_cents is None:
            continue
        if level_cents in own_prices:
            excluded.append(level_cents)
            continue
        return {
            "priceCents": level_cents,
            "price": cents_to_price(level_cents),
            "count": level.get("count"),
            "status": "self_price_excluded" if excluded else "raw",
            "excludedOwnPrices": [
                cents_to_price(value) for value in dict.fromkeys(excluded)
            ],
        }

    return {
        "priceCents": None,
        "price": None,
        "count": None,
        "status": "missing_external_level",
        "excludedOwnPrices": [
            cents_to_price(value) for value in dict.fromkeys(excluded)
        ],
    }


def build_long_buy_proposal(
    config: StrategyConfig,
    *,
    c5_price_batch: Any,
    orderbook_snapshot: dict[str, Any] | None,
    quantity: int,
    own_price_cents: Iterable[int] = (),
) -> dict[str, Any] | None:
    try:
        c5_reference = float(c5_price_batch)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(c5_reference) or c5_reference <= 0:
        return None

    quantity = max(0, int(quantity))
    if quantity <= 0:
        return None
    balance_discount = float(config.profit_trade_balance_discount)
    standard_roi = max(0.0, float(config.profit_trade_min_roi))
    aggressive_roi = max(
        0.0,
        standard_roi - max(0.0, float(config.profit_trade_long_buy_aggressive_roi_delta)),
    )
    # A long buy must be safe against the price we will actually use when its
    # fill is listed on C5.  Profit Trade initially undercuts the authoritative
    # price_batch reference by initialListingDiscountPct and floors that C5
    # listing price to a cent before applying the C5 net factor.  Using the raw
    # price_batch price here would overstate proceeds and permit an unsafe bid.
    initial_listing_discount_pct = min(
        100.0,
        max(0.0, float(config.profit_trade_initial_listing_discount_pct)),
    )
    initial_listing_cents = floor_price_to_cents(
        Decimal(str(c5_reference))
        * (
            Decimal("1")
            - Decimal(str(initial_listing_discount_pct)) / Decimal("100")
        )
    )
    initial_listing_price = cents_to_price(initial_listing_cents)
    if initial_listing_price is None:
        return None
    c5_expected_net = initial_listing_price * float(
        config.profit_trade_c5_current_sale_net_factor
    )
    standard_denominator = balance_discount + standard_roi
    aggressive_denominator = balance_discount + aggressive_roi
    if (
        c5_expected_net <= 0
        or standard_denominator <= 0
        or aggressive_denominator <= 0
    ):
        return None

    standard_safe_cents = floor_price_to_cents(
        c5_expected_net / standard_denominator
    )
    aggressive_safe_cents = floor_price_to_cents(
        c5_expected_net / aggressive_denominator
    )
    if standard_safe_cents is None or aggressive_safe_cents is None:
        return None

    competitor = competitor_buy_reference(
        orderbook_snapshot,
        own_price_cents=own_price_cents,
    )
    competitor_cents = competitor.get("priceCents")
    min_advantage_cents = max(
        1,
        price_to_cents(config.profit_trade_long_buy_min_price_advantage) or 1,
    )
    max_advantage_cents = max(
        min_advantage_cents,
        price_to_cents(config.profit_trade_long_buy_max_price_advantage)
        or min_advantage_cents,
    )
    decision = "standard_no_competitor"
    target_cents = standard_safe_cents
    if competitor_cents is not None:
        competitor_cents = int(competitor_cents)
        if standard_safe_cents >= competitor_cents + min_advantage_cents:
            target_cents = standard_safe_cents
            decision = "standard_safe_price"
        elif aggressive_safe_cents >= competitor_cents + min_advantage_cents:
            target_cents = min(
                aggressive_safe_cents,
                competitor_cents + max_advantage_cents,
            )
            decision = "aggressive_competitor_advantage"
        else:
            target_cents = standard_safe_cents
            decision = "standard_low_queue"

    target_price = cents_to_price(target_cents)
    standard_safe_price = cents_to_price(standard_safe_cents)
    aggressive_safe_price = cents_to_price(aggressive_safe_cents)
    if target_price is None or standard_safe_price is None or aggressive_safe_price is None:
        return None
    worst_roi = normalize_roi_four_decimals(
        c5_expected_net / target_price - balance_discount
    )
    competitor_price = competitor.get("price")
    competitor_roi = (
        normalize_roi_four_decimals(
            c5_expected_net / float(competitor_price) - balance_discount
        )
        if competitor_price is not None and float(competitor_price) > 0
        else None
    )
    competitor_profit = (
        round(c5_expected_net - float(competitor_price) * balance_discount, 2)
        if competitor_price is not None and float(competitor_price) > 0
        else None
    )
    return {
        "c5PriceBatch": round(c5_reference, 2),
        "c5ExpectedNetPrice": round(c5_expected_net, 2),
        "balanceDiscount": balance_discount,
        "standardRoi": normalize_roi_four_decimals(standard_roi),
        "aggressiveRoi": normalize_roi_four_decimals(aggressive_roi),
        "standardSafePrice": standard_safe_price,
        "standardSafePriceCents": standard_safe_cents,
        "aggressiveSafePrice": aggressive_safe_price,
        "aggressiveSafePriceCents": aggressive_safe_cents,
        "competitorBuyPrice": competitor_price,
        "competitorBuyPriceCents": competitor_cents,
        "competitorBuyCount": competitor.get("count"),
        "competitorBuyStatus": competitor.get("status"),
        "competitorBuyRoi": competitor_roi,
        "competitorBuyProfit": competitor_profit,
        "excludedOwnBuyPrices": competitor.get("excludedOwnPrices") or [],
        "targetPrice": target_price,
        "targetPriceCents": target_cents,
        "worstCaseRoi": worst_roi,
        "quantity": quantity,
        "decision": decision,
    }


def long_buy_order_public(order: Any | None) -> dict[str, Any] | None:
    if order is None:
        return None
    keys = set(order.keys()) if hasattr(order, "keys") else set()

    def value(key: str) -> Any:
        if isinstance(order, dict):
            return order.get(key)
        return order[key] if key in keys else None

    bid_price = cents_to_price(value("bid_price_cents"))
    return {
        "id": value("id"),
        "state": value("state"),
        "accountId": value("steam_account_id"),
        "steamId": value("steam_id"),
        "buyOrderId": value("buy_order_id"),
        "bidPrice": bid_price,
        "quantity": value("quantity"),
        "filledQuantity": value("filled_quantity"),
        "remainingQuantity": value("remaining_quantity"),
        "standardSafePrice": cents_to_price(value("standard_safe_price_cents")),
        "aggressiveSafePrice": cents_to_price(value("aggressive_safe_price_cents")),
        "worstCaseRoi": value("worst_case_roi"),
        "createdAt": value("created_at"),
        "lastCheckedAt": value("last_checked_at"),
        "lastFilledAt": value("last_filled_at"),
        "reason": value("terminal_reason"),
        "replacesOrderId": value("replaces_order_id"),
        "replacedByOrderId": value("replaced_by_order_id"),
    }
