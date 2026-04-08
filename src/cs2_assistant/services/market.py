from __future__ import annotations

from typing import Any

from cs2_assistant.clients import C5GameClient, CSQAQClient, SteamDTClient
from cs2_assistant.models import MarketState
from cs2_assistant.utils import chunked, safe_float, safe_int

DEFAULT_C5_SETTLEMENT_FACTOR = 0.869
DEFAULT_STEAM_BALANCE_DISCOUNT = 0.73
DEFAULT_STEAM_NET_FACTOR = 0.85  # CS2: seller receives ~85% after Steam 15% fee (5% Steam + 10% game)


def _normalize_platform_name(name: str) -> str:
    return name.strip().lower().replace(" ", "").replace("-", "")


def _pick(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


def _safe_positive_float(value: Any) -> float | None:
    number = safe_float(value)
    if number is None or number <= 0:
        return None
    return number


def calculate_ratio(
    c5_sell_price: float | None,
    steam_sell_price: float | None,
    *,
    c5_settlement_factor: float = DEFAULT_C5_SETTLEMENT_FACTOR,
) -> float | None:
    if c5_sell_price is None or steam_sell_price is None:
        return None
    denominator = c5_settlement_factor * steam_sell_price
    if denominator <= 0:
        return None
    return c5_sell_price / denominator


def calculate_t_yield_rate(
    ratio: float | None,
    *,
    steam_balance_discount: float = DEFAULT_STEAM_BALANCE_DISCOUNT,
    c5_settlement_factor: float = DEFAULT_C5_SETTLEMENT_FACTOR,
) -> float | None:
    if ratio is None:
        return None
    return ratio * c5_settlement_factor - steam_balance_discount


# ---------------------------------------------------------------------------
# Strategy formulas (inventory-pool based T-tool)
# ---------------------------------------------------------------------------


def calculate_steam_after_tax(
    steam_sell_price: float | None,
    *,
    steam_net_factor: float = DEFAULT_STEAM_NET_FACTOR,
) -> float | None:
    """Steam 卖出后实际到手余额 = steam_sell_price × steam_net_factor"""
    if steam_sell_price is None or steam_sell_price <= 0:
        return None
    return steam_sell_price * steam_net_factor


def calculate_listing_ratio(
    rebuy_price: float | None,
    steam_sell_price: float | None,
    *,
    steam_net_factor: float = DEFAULT_STEAM_NET_FACTOR,
) -> float | None:
    """listing_ratio = rebuy_price / steam_after_tax_price

    LOW → 挂刀做T 有利（Steam 卖出后，外部平台低价补仓）
    """
    if rebuy_price is None or steam_sell_price is None:
        return None
    steam_after_tax = steam_sell_price * steam_net_factor
    if steam_after_tax <= 0:
        return None
    return rebuy_price / steam_after_tax


def calculate_transfer_real_ratio(
    listing_ratio: float | None,
    *,
    c5_settlement_factor: float = DEFAULT_C5_SETTLEMENT_FACTOR,
    balance_discount: float = DEFAULT_STEAM_BALANCE_DISCOUNT,
) -> float | None:
    """transfer_real_ratio = listing_ratio × c5_settlement_factor - balance_discount

    HIGH → 导余额做T 有利（利用低价余额赚钱）
    """
    if listing_ratio is None:
        return None
    return listing_ratio * c5_settlement_factor - balance_discount


class MarketService:
    def __init__(
        self,
        *,
        steamdt_client: SteamDTClient | None = None,
        csqaq_client: CSQAQClient | None = None,
        c5_client: C5GameClient | None = None,
        app_id: int = 730,
        include_c5_purchase_prices: bool = True,
    ):
        self.steamdt_client = steamdt_client
        self.csqaq_client = csqaq_client
        self.c5_client = c5_client
        self.app_id = app_id
        self.include_c5_purchase_prices = include_c5_purchase_prices

    def refresh_items(self, items: list[dict[str, Any]]) -> list[MarketState]:
        states = {
            row["market_hash_name"]: MarketState(
                market_hash_name=row["market_hash_name"],
                name_cn=row.get("name_cn"),
                c5_item_id=row.get("c5_item_id"),
            )
            for row in items
        }

        market_hash_names = list(states.keys())
        if self.csqaq_client:
            self._load_csqaq_prices(states, market_hash_names)

        if self.steamdt_client:
            self._load_steamdt_prices(states, market_hash_names)

        if self.c5_client:
            for batch in chunked(market_hash_names, 100):
                data = self.c5_client.price_batch(batch, app_id=self.app_id)
                self._apply_c5_batch(states, data)
            if self.include_c5_purchase_prices:
                self._apply_c5_purchase_prices(states)

        for state in states.values():
            state.ratio = calculate_ratio(
                state.c5_sell_price,
                state.steam_sell_price,
            )
        return list(states.values())

    def _load_steamdt_prices(
        self,
        states: dict[str, MarketState],
        market_hash_names: list[str],
    ) -> None:
        if not market_hash_names:
            return
        if len(market_hash_names) == 1:
            data_list = self.steamdt_client.price_single(market_hash_names[0])
            self._apply_steamdt_single(states[market_hash_names[0]], data_list)
            return

        try:
            for batch in chunked(market_hash_names, 100):
                data = self.steamdt_client.price_batch(batch)
                self._apply_steamdt_batch(states, data)
            return
        except Exception:
            pass

        for market_hash_name in market_hash_names:
            try:
                data_list = self.steamdt_client.price_single(market_hash_name)
            except Exception:
                continue
            self._apply_steamdt_single(states[market_hash_name], data_list)

    def _load_csqaq_prices(
        self,
        states: dict[str, MarketState],
        market_hash_names: list[str],
    ) -> None:
        for batch in chunked(market_hash_names, 50):
            try:
                data = self.csqaq_client.price_by_market_hash_names(batch)
                self._apply_csqaq_batch(states, data)
                continue
            except Exception:
                pass

            for market_hash_name in batch:
                try:
                    data = self.csqaq_client.price_by_market_hash_names([market_hash_name])
                except Exception:
                    continue
                self._apply_csqaq_batch(states, data)

    def _apply_steamdt_batch(
        self,
        states: dict[str, MarketState],
        data: list[dict[str, Any]],
    ) -> None:
        for item in data:
            market_hash_name = str(item.get("marketHashName") or "")
            state = states.get(market_hash_name)
            if state is None:
                continue
            self._apply_steamdt_single(state, item.get("dataList") or [])

    def _apply_steamdt_single(self, state: MarketState, data_list: list[dict[str, Any]]) -> None:
        state.raw_json.setdefault("steamdt", data_list)
        for record in data_list:
            platform = _normalize_platform_name(str(record.get("platform") or ""))
            if "c5" in platform:
                sell_price = _safe_positive_float(record.get("sellPrice"))
                state.c5_sell_price = _pick(sell_price, state.c5_sell_price)
                state.c5_sell_count = _pick(safe_int(record.get("sellCount")), state.c5_sell_count)
                platform_item_id = str(record.get("platformItemId") or "").strip() or None
                state.c5_item_id = _pick(platform_item_id, state.c5_item_id)
                if sell_price is not None:
                    state.c5_price_source = _pick("steamdt", state.c5_price_source)
            elif "steam" in platform:
                sell_price = _safe_positive_float(record.get("sellPrice"))
                if state.steam_sell_price is None and sell_price is not None:
                    state.steam_sell_price = sell_price
                    state.steam_price_source = "steamdt"
                if state.steam_sell_count is None:
                    state.steam_sell_count = _pick(safe_int(record.get("sellCount")), state.steam_sell_count)
                if state.steam_bid_price is None:
                    state.steam_bid_price = _pick(_safe_positive_float(record.get("biddingPrice")), state.steam_bid_price)
                if state.steam_bid_count is None:
                    state.steam_bid_count = _pick(safe_int(record.get("biddingCount")), state.steam_bid_count)

    def _apply_csqaq_batch(
        self,
        states: dict[str, MarketState],
        data: dict[str, Any],
    ) -> None:
        for market_hash_name, payload in data.items():
            state = states.get(market_hash_name)
            if state is None or not isinstance(payload, dict):
                continue
            state.raw_json["csqaq_batch"] = payload
            if not state.name_cn:
                state.name_cn = str(payload.get("name") or "").strip() or state.name_cn
            steam_sell_price = _safe_positive_float(payload.get("steamSellPrice"))
            if state.steam_sell_price is None and steam_sell_price is not None:
                state.steam_sell_price = steam_sell_price
                state.steam_price_source = "csqaq_batch"
            if state.steam_sell_count is None:
                state.steam_sell_count = _pick(safe_int(payload.get("steamSellNum")), state.steam_sell_count)

    def _apply_c5_batch(self, states: dict[str, MarketState], data: dict[str, Any]) -> None:
        for market_hash_name, payload in data.items():
            state = states.get(market_hash_name)
            if state is None:
                continue
            state.raw_json["c5_batch"] = payload
            sell_price = _safe_positive_float(payload.get("price"))
            state.c5_sell_price = _pick(sell_price, state.c5_sell_price)
            state.c5_sell_count = _pick(safe_int(payload.get("count")), state.c5_sell_count)
            item_id = str(payload.get("itemId") or "").strip() or None
            state.c5_item_id = _pick(item_id, state.c5_item_id)
            website = payload.get("website")
            if website:
                state.c5_website = str(website)
            if sell_price is not None:
                state.c5_price_source = "c5_batch"

    def _apply_c5_purchase_prices(self, states: dict[str, MarketState]) -> None:
        for market_hash_name, state in states.items():
            try:
                payload = self.c5_client.purchase_max_price(market_hash_name, app_id=self.app_id)
            except Exception:
                continue
            state.raw_json["c5_purchase_max_price"] = payload
            state.c5_bid_price = _pick(
                _safe_positive_float(
                    payload.get("maxPrice")
                    or payload.get("price")
                    or payload.get("purchasePrice")
                ),
                state.c5_bid_price,
            )
            state.c5_bid_count = _pick(
                safe_int(payload.get("count") or payload.get("purchaseCount")),
                state.c5_bid_count,
            )
