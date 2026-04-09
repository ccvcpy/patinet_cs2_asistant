from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from cs2_assistant.clients import C5GameClient
from cs2_assistant.utils import safe_float


@dataclass(slots=True)
class RebuyResult:
    success: bool
    skipped: bool
    reason: str
    actual_price: float | None = None
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
    market_hash_name: str,
    expected_price: float,
    app_id: int,
    tolerance_pct: float,
    dry_run: bool,
) -> RebuyResult:
    live_price = fetch_c5_price(client, market_hash_name, app_id)
    if live_price is None:
        return RebuyResult(False, False, "missing_price")

    max_price = expected_price * (1.0 + max(0.0, tolerance_pct) / 100.0)
    if live_price > max_price:
        return RebuyResult(False, True, "price_too_high", actual_price=live_price)

    if dry_run:
        return RebuyResult(True, True, "dry_run", actual_price=live_price)

    out_trade_no = uuid.uuid4().hex
    payload = client.quick_buy(
        app_id=app_id,
        market_hash_name=market_hash_name,
        max_price=max_price,
        out_trade_no=out_trade_no,
    )
    return RebuyResult(True, False, "ok", actual_price=live_price, payload=payload)
