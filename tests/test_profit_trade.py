from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.accounts import Account
from cs2_assistant.clients import SteamMarketError
from cs2_assistant.config import Settings
from cs2_assistant.models import CatalogItem, MarketState, StrategyConfig
import cs2_assistant.services.profit_trade as profit_trade_module
from cs2_assistant.services.profit_trade import (
    build_profit_trade_dashboard_payload,
    build_profit_trade_interruptions_payload,
    build_profit_trade_roi_watch_payload,
    create_manual_profit_trade_record,
    dismiss_profit_trade,
    execute_profit_trade_buy,
    execute_profit_trade_list_c5,
    lock_profit_trade,
    manual_settle_profit_trade,
    refresh_profit_trade_sales,
    refresh_profit_trade_listings,
    recover_unverified_profit_trade_steam_buys,
    run_profit_trade_once,
    scan_profit_trade_opportunities,
    update_profit_trade_protection,
    update_manual_profit_trade_record,
)
from cs2_assistant.services.market import MarketService
from cs2_assistant.services.strategy import save_strategy_config
from cs2_assistant.services.steam_balances import (
    load_steam_account_balances,
    update_steam_account_balance_snapshot,
)
from cs2_assistant.services.steam_request_scheduler import (
    SteamRequestGuardRejected,
    SteamRequestTimeout,
)
from cs2_assistant.db import Database


class FakeProfitMarketService:
    def __init__(self, *, c5_sell_price: float = 90.0, steam_sell_price: float = 100.0) -> None:
        self.c5_sell_price = c5_sell_price
        self.steam_sell_price = steam_sell_price

    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        return [
            MarketState(
                market_hash_name=str(item["market_hash_name"]),
                name_cn=str(item.get("name_cn") or item["market_hash_name"]),
                c5_sell_price=self.c5_sell_price,
                c5_price_source="c5_batch",
                steam_sell_price=self.steam_sell_price,
                steam_price_source="steam_orderbook",
            )
            for item in items
        ]


class StreamingProfitSteamClient:
    account_id = "stream-account"
    steam_id64 = "stream-steam"

    def __init__(self, slow_release: threading.Event) -> None:
        self.slow_release = slow_release
        self.slow_started = threading.Event()

    def order_book(self, *, app_id: int, market_hash_name: str, **_: object) -> dict:
        if market_hash_name == "Slow Item":
            self.slow_started.set()
            if not self.slow_release.wait(timeout=5):
                raise RuntimeError("test did not release the slow orderbook")
        return {
            "data": {
                "eCurrency": 23,
                "rgCompactSellOrders": [10000, 1],
            }
        }


class StreamingProfitC5Client:
    def __init__(self) -> None:
        self.price_batch_calls: list[list[str]] = []
        self.statistics_calls: list[list[str]] = []

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        self.price_batch_calls.append(list(market_hash_names))
        return {
            name: {"price": 90.0, "count": 10, "itemId": f"item-{index}"}
            for index, name in enumerate(market_hash_names)
        }

    def price_statistics_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        self.statistics_calls.append(list(market_hash_names))
        return {
            name: {
                "currentSellPrice": 90.0,
                "onSaleCount": 10,
                "purchaseMaxPrice": 80.0,
                "purchaseCount": 10,
            }
            for name in market_hash_names
        }


class FakeSteamBuyClient:
    steam_id64 = "steam-a"
    account_id = "account-a"

    def __init__(self, *, total: int = 10000, wallet_balance: float = 1000.0) -> None:
        self.total = total
        self._wallet_balance = wallet_balance
        self.buy_calls: list[dict] = []
        self.create_buy_order_calls: list[dict] = []
        self.cancel_buy_order_calls: list[dict] = []
        self.search_listing_calls: list[dict] = []
        self.order_book_calls: list[dict] = []
        self.wallet_balance_calls: list[dict] = []

    def search_listings(self, **kwargs: object) -> dict:
        self.search_listing_calls.append(dict(kwargs))
        return {
            "listinginfo": {
                "listing-1": {
                    "listingid": "listing-1",
                    "converted_price": self.total - 500,
                    "converted_fee": 500,
                    "converted_total": self.total,
                    "converted_currencyid": 23,
                    "description": {
                        "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    },
                }
            }
        }

    def buy_listing(self, **kwargs: object) -> dict:
        self.buy_calls.append(dict(kwargs))
        self._wallet_balance -= self.total / 100.0
        return {"wallet_info": {"success": 1}}

    def order_book(self, **kwargs: object) -> dict:
        self.order_book_calls.append(dict(kwargs))
        return {
            "success": True,
            "data": {
                "eCurrency": 23,
                "amtMinSellOrder": self.total,
                "cSellOrders": 1,
                "rgCompactSellOrders": [self.total, 1],
                "rgCompactBuyOrders": [],
            },
        }

    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_buy_order_calls.append(dict(kwargs))
        self._wallet_balance -= int(kwargs.get("price_total") or self.total) / 100.0
        return {"success": 1, "buy_orderid": "buy-order-1"}

    def wallet_balance(self, **kwargs: object) -> dict:
        self.wallet_balance_calls.append(dict(kwargs))
        return {"balance": self._wallet_balance, "delayed_balance": 0.0, "currency": "CNY"}

    def cancel_buy_order(self, **kwargs: object) -> dict:
        self.cancel_buy_order_calls.append(dict(kwargs))
        return {"success": 1}

    def my_listings(self, **kwargs: object) -> dict:
        return {"buy_orders": []}


class FakeStaleListingThenNextSteamBuyClient(FakeSteamBuyClient):
    def search_listings(self, **kwargs: object) -> dict:
        self.search_listing_calls.append(dict(kwargs))
        return {
            "listinginfo": {
                "stale-listing": {
                    "listingid": "stale-listing",
                    "converted_price": self.total - 500,
                    "converted_fee": 500,
                    "converted_total": self.total,
                    "converted_currencyid": 23,
                    "description": {
                        "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    },
                },
                "fresh-listing": {
                    "listingid": "fresh-listing",
                    "converted_price": self.total - 499,
                    "converted_fee": 500,
                    "converted_total": self.total + 1,
                    "converted_currencyid": 23,
                    "description": {
                        "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    },
                },
            }
        }

    def buy_listing(self, **kwargs: object) -> dict:
        self.buy_calls.append(dict(kwargs))
        if kwargs.get("listing_id") == "stale-listing":
            raise SteamMarketError('"您不能购买此物品，因为其他人已经购买了此物品。"')
        self._wallet_balance -= int(kwargs.get("total") or self.total) / 100.0
        return {"wallet_info": {"success": 1}}


class SignalingSteamBuyClient(FakeSteamBuyClient):
    def __init__(self, *, total: int = 10000, wallet_balance: float = 1000.0) -> None:
        super().__init__(total=total, wallet_balance=wallet_balance)
        self.buy_started = threading.Event()

    def buy_listing(self, **kwargs: object) -> dict:
        self.buy_started.set()
        return super().buy_listing(**kwargs)


class BlockingSteamBuyClient(SignalingSteamBuyClient):
    def __init__(self, release: threading.Event, *, total: int = 10000) -> None:
        super().__init__(total=total)
        self.release = release

    def buy_listing(self, **kwargs: object) -> dict:
        self.buy_started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test did not release the blocked Steam buy")
        return FakeSteamBuyClient.buy_listing(self, **kwargs)


class SequencedStreamingProfitMarketService(FakeProfitMarketService):
    """Exposes a barrier after every scan state exists but before scan return."""

    def __init__(self) -> None:
        super().__init__()
        self.all_states_published = threading.Event()
        self.release_full_scan = threading.Event()

    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        states = super().refresh_items(items)
        self.all_states_published.set()
        if not self.release_full_scan.wait(timeout=5):
            raise RuntimeError("test did not release the completed scan")
        return states

    def refresh_items_stream(
        self,
        items: list[dict],
        *,
        on_state_ready: object,
    ) -> list[MarketState]:
        states = self.refresh_items(items)
        for state in states:
            on_state_ready(state)  # type: ignore[operator]
        self.all_states_published.set()
        return states


class FakeRemovedListingThenNextSteamBuyClient(FakeStaleListingThenNextSteamBuyClient):
    def buy_listing(self, **kwargs: object) -> dict:
        self.buy_calls.append(dict(kwargs))
        if kwargs.get("listing_id") == "stale-listing":
            raise SteamMarketError('"购买您的物品时出现问题。该物品可能已被移除。请刷新页面并重试。"')
        self._wallet_balance -= int(kwargs.get("total") or self.total) / 100.0
        return {"wallet_info": {"success": 1}}


class FakeStaleListingOnlyThenBuyOrderClient(FakeSteamBuyClient):
    def search_listings(self, **kwargs: object) -> dict:
        self.search_listing_calls.append(dict(kwargs))
        return {
            "listinginfo": {
                "stale-listing": {
                    "listingid": "stale-listing",
                    "converted_price": self.total - 500,
                    "converted_fee": 500,
                    "converted_total": self.total,
                    "converted_currencyid": 23,
                    "description": {
                        "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    },
                }
            }
        }

    def buy_listing(self, **kwargs: object) -> dict:
        self.buy_calls.append(dict(kwargs))
        raise SteamMarketError('"您不能购买此物品，因为其他人已经购买了此物品。"')

class FakeOrderbookFailClient(FakeSteamBuyClient):
    def order_book(self, **kwargs: object) -> dict:
        self.order_book_calls.append(dict(kwargs))
        raise RuntimeError("orderbook unavailable")


class FakeSearchListingsFailClient(FakeSteamBuyClient):
    def search_listings(self, **kwargs: object) -> dict:
        self.search_listing_calls.append(dict(kwargs))
        raise SteamMarketError("429 Too Many Requests while searching listings")


class FakeSearchListings429Client(FakeSteamBuyClient):
    def __init__(
        self,
        *,
        failures: int = 1,
        total: int = 10000,
        total_after_first_429: int | None = None,
    ) -> None:
        super().__init__(total=total)
        self.failures_remaining = failures
        self.total_after_first_429 = total_after_first_429

    def search_listings(self, **kwargs: object) -> dict:
        self.search_listing_calls.append(dict(kwargs))
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            if self.total_after_first_429 is not None:
                self.total = self.total_after_first_429
            raise SteamMarketError(
                "Steam listings search returned HTTP 429 Too Many Requests",
                status_code=429,
                retry_after="0",
            )
        return {
            "listinginfo": {
                "listing-after-429": {
                    "listingid": "listing-after-429",
                    "converted_price": self.total - 500,
                    "converted_fee": 500,
                    "converted_total": self.total,
                    "converted_currencyid": 23,
                    "description": {
                        "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    },
                }
            }
        }


class FakeSearchListings400Client(FakeSteamBuyClient):
    def search_listings(self, **kwargs: object) -> dict:
        self.search_listing_calls.append(dict(kwargs))
        raise SteamMarketError(
            "Steam listings search returned HTTP 400 after account relogin",
            status_code=400,
        )


class FakeCommoditySteamBuyClient(FakeSteamBuyClient):
    def search_listings(self, **kwargs: object) -> dict:
        self.search_listing_calls.append(dict(kwargs))
        return {"listinginfo": {}}


class FakeCheaperFilledBuyOrderClient(FakeCommoditySteamBuyClient):
    def __init__(
        self,
        *,
        total: int = 10000,
        actual_total: int = 9440,
        wallet_balance: float = 1000.0,
    ) -> None:
        super().__init__(total=total, wallet_balance=wallet_balance)
        self.actual_total = actual_total

    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_buy_order_calls.append(dict(kwargs))
        self._wallet_balance -= self.actual_total / 100.0
        return {"success": 1, "buy_orderid": "buy-order-cheaper-fill"}



class FakeStickerListingSteamBuyClient(FakeSteamBuyClient):
    def search_listings(self, **kwargs: object) -> dict:
        self.search_listing_calls.append(dict(kwargs))
        return {
            "listinginfo": {
                "sticker-listing-1": {
                    "listingid": "sticker-listing-1",
                    "converted_price": self.total - 500,
                    "converted_fee": 500,
                    "converted_total": self.total,
                    "converted_currencyid": 23,
                    "description": {
                        "market_hash_name": "Sticker | Cluck",
                    },
                }
            }
        }
class FakePendingBuyOrderClient(FakeCommoditySteamBuyClient):
    def __init__(self, *, total: int = 10000, wallet_balance: float = 1000.0) -> None:
        super().__init__(total=total, wallet_balance=wallet_balance)
        self.buy_order_active = False

    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_buy_order_calls.append(dict(kwargs))
        self.buy_order_active = True
        return {"success": 1, "buy_orderid": "buy-order-1"}

    def cancel_buy_order(self, **kwargs: object) -> dict:
        self.cancel_buy_order_calls.append(dict(kwargs))
        self.buy_order_active = False
        return {"success": 1}

    def my_listings(self, **kwargs: object) -> dict:
        if not self.buy_order_active:
            return {"buy_orders": []}
        return {
            "buy_orders": [
                {
                    "buy_orderid": "buy-order-1",
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "price": self.total,
                    "quantity": 1,
                    "quantity_remaining": 1,
                }
            ]
        }


class FakeCancelResponseButOrderRemainsActiveClient(FakePendingBuyOrderClient):
    def cancel_buy_order(self, **kwargs: object) -> dict:
        self.cancel_buy_order_calls.append(dict(kwargs))
        return {"success": 1}


class FakeBuyOrderFillsDuringCancelClient(FakePendingBuyOrderClient):
    def cancel_buy_order(self, **kwargs: object) -> dict:
        self.cancel_buy_order_calls.append(dict(kwargs))
        self.buy_order_active = False
        self._wallet_balance -= self.total / 100.0
        return {"success": 1}


class FakeHistoricalPurchaseClient(FakePendingBuyOrderClient):
    steam_id64 = "steam-a"
    account_id = "account-a"

    def __init__(
        self,
        *,
        total: int = 10000,
        actual_total: float = 100.0,
        wallet_balance: float = 900.0,
    ) -> None:
        super().__init__(total=total, wallet_balance=wallet_balance)
        self.actual_total = actual_total
        self.purchase_receipt_calls: list[dict] = []

    def find_purchase_receipt(self, **kwargs: object) -> dict:
        self.purchase_receipt_calls.append(dict(kwargs))
        return {
            "listingId": "history-listing-1",
            "purchaseId": "history-purchase-1",
            "timePurchased": 1783636423,
            "paidAmount": round(self.actual_total / 1.0526315789, 2),
            "paidFee": round(self.actual_total - self.actual_total / 1.0526315789, 2),
            "paidTotal": self.actual_total,
            "marketHashName": "AK-47 | Redline (Field-Tested)",
            "assetId": "history-old-asset",
            "newAssetId": "asset-b-history",
        }



class FakeFilledButListingStaleBuyOrderClient(FakePendingBuyOrderClient):
    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_buy_order_calls.append(dict(kwargs))
        self.buy_order_active = True
        self._wallet_balance -= int(kwargs.get("price_total") or self.total) / 100.0
        return {"success": 1, "buy_orderid": "buy-order-1"}



class FakeDelayedBuyOrderClearsClient(FakeCommoditySteamBuyClient):
    def __init__(self, *, total: int = 10000, wallet_balance: float = 1000.0) -> None:
        super().__init__(total=total, wallet_balance=wallet_balance)
        self.my_listings_calls = 0

    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_buy_order_calls.append(dict(kwargs))
        self._wallet_balance -= int(kwargs.get("price_total") or self.total) / 100.0
        return {"success": 1, "buy_orderid": "buy-order-1"}

    def my_listings(self, **kwargs: object) -> dict:
        self.my_listings_calls += 1
        if self.my_listings_calls == 1:
            return {"buy_orders": []}
        if self.my_listings_calls == 2:
            return {
                "buy_orders": [
                    {
                        "buy_orderid": "buy-order-1",
                        "market_hash_name": "AK-47 | Redline (Field-Tested)",
                        "price": self.total,
                        "quantity": 1,
                        "quantity_remaining": 1,
                    }
                ]
            }
        return {"buy_orders": []}


class FakeOldBuyOrderOnlyAfterCreateClient(FakeCommoditySteamBuyClient):
    def __init__(self, *, total: int = 10000, wallet_balance: float = 1000.0) -> None:
        super().__init__(total=total, wallet_balance=wallet_balance)
        self.old_order = {
            "buy_orderid": "old-buy-order",
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "price": self.total - 100,
            "quantity": 1,
            "quantity_remaining": 1,
        }

    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_buy_order_calls.append(dict(kwargs))
        self._wallet_balance -= int(kwargs.get("price_total") or self.total) / 100.0
        return {"success": 1, "buy_orderid": "new-buy-order"}

    def my_listings(self, **kwargs: object) -> dict:
        return {"buy_orders": [dict(self.old_order)]}
class FakeC5InventoryClient:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.inventory_calls: list[dict] = []

    def inventory(self, steam_id: str, app_id: int = 730) -> dict:
        self.inventory_calls.append({"steam_id": steam_id, "app_id": app_id})
        return {"list": [dict(item) for item in self.items], "total": len(self.items)}
class FakeAccountStore:
    def __init__(self, accounts: list[Account]) -> None:
        self._accounts = accounts

    def list_accounts(self) -> list[Account]:
        return list(self._accounts)

    def get_current(self) -> Account | None:
        return self._accounts[0] if self._accounts else None


class FakeC5SaleClient:
    def __init__(
        self,
        *,
        active_product_ids: list[str] | None = None,
        price_batch_price: float = 90.0,
        price_batch_count: int = 3,
        require_steam_id_for_sale_search: bool = False,
        bound_steam_ids: list[str] | None = None,
    ) -> None:
        self.sale_calls: list[dict] = []
        self.modify_calls: list[dict] = []
        self.sale_search_calls: list[dict] = []
        self.price_batch_calls: list[dict[str, object]] = []
        self.seller_order_list_calls: list[dict] = []
        self.seller_order_detail_calls: list[str] = []
        self.active_product_ids = (
            list(active_product_ids)
            if active_product_ids is not None
            else ["c5-product-1"]
        )
        self.require_steam_id_for_sale_search = bool(require_steam_id_for_sale_search)
        self.bound_steam_ids = list(bound_steam_ids or ["steam-a"])
        self.price = float(price_batch_price)
        self.on_sale_count = int(price_batch_count)
        self.statistics: dict[str, dict] = {}
        self.price_batch_error: Exception | None = None
        self.sale_error: Exception | None = None
        self.sale_payload: dict | None = None
        self.modify_error: Exception | None = None
        self.modify_payload: dict | None = None
        self.seller_order_error: Exception | None = None
        self.seller_orders: dict[str, list[dict]] = {}
        self.seller_order_details: dict[str, dict] = {}

    def steam_info(self) -> dict:
        return {
            "steamList": [
                {"steamId": steam_id, "autoType": 2}
                for steam_id in self.bound_steam_ids
            ]
        }

    def sale_create(self, **kwargs: object) -> dict:
        self.sale_calls.append(dict(kwargs))
        if self.sale_error is not None:
            raise self.sale_error
        if self.sale_payload is not None:
            return dict(self.sale_payload)
        return {"successList": [{"productId": "c5-product-1"}]}

    def sale_search(self, **kwargs: object) -> dict:
        self.sale_search_calls.append(dict(kwargs))
        if self.require_steam_id_for_sale_search and not str(kwargs.get("steam_id") or "").strip():
            raise RuntimeError('C5 sale_search requires steam_id: errorCode=100000 "系统异常"')
        return {
            "list": [{"productId": product_id} for product_id in self.active_product_ids],
            "total": len(self.active_product_ids),
        }

    def seller_order_list(self, **kwargs: object) -> dict:
        self.seller_order_list_calls.append(dict(kwargs))
        if self.seller_order_error is not None:
            raise self.seller_order_error
        steam_id = str(kwargs.get("steam_id") or "")
        status = kwargs.get("status")
        rows = list(self.seller_orders.get(steam_id, []))
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        return {"list": rows, "total": len(rows)}

    def seller_order_detail(self, order_id: str) -> dict:
        self.seller_order_detail_calls.append(str(order_id))
        return dict(self.seller_order_details.get(str(order_id), {}))

    def price_statistics_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        return {name: self.statistics.get(name, {}) for name in market_hash_names}

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        self.price_batch_calls.append(
            {"market_hash_names": list(market_hash_names), "app_id": app_id}
        )
        if self.price_batch_error is not None:
            raise self.price_batch_error
        return {
            name: {"price": self.price, "count": self.on_sale_count}
            for name in market_hash_names
        }

    def sale_modify(self, **kwargs: object) -> dict:
        self.modify_calls.append(dict(kwargs))
        if self.modify_error is not None:
            raise self.modify_error
        if self.modify_payload is not None:
            return self.modify_payload
        return {"successList": kwargs.get("data_list") or []}


class FakeC5PreBuyRefreshClient(FakeC5SaleClient):
    def __init__(
        self,
        *,
        price: float = 90.0,
        on_sale_count: int = 10,
        purchase_max_price: float = 80.0,
        purchase_count: int = 10,
    ) -> None:
        super().__init__(
            price_batch_price=price,
            price_batch_count=on_sale_count,
        )
        self.purchase_max_price = purchase_max_price
        self.purchase_count = purchase_count
        self.statistics_calls: list[dict[str, object]] = []

    def price_statistics_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        self.statistics_calls.append(
            {"market_hash_names": list(market_hash_names), "app_id": app_id}
        )
        return {
            name: {
                "currentSellPrice": self.price,
                "onSaleCount": self.on_sale_count,
                "purchaseMaxPrice": self.purchase_max_price,
                "purchaseCount": self.purchase_count,
            }
            for name in market_hash_names
        }


class FakeServerChan:
    messages: list[tuple[str, str]] = []

    def __init__(self, *_: object, **__: object) -> None:
        pass

    def send(self, title: str, body: str) -> None:
        self.messages.append((title, body))


def profit_config(**overrides: object) -> StrategyConfig:
    values: dict[str, object] = {
        "profit_trade_enabled": True,
        "profit_trade_min_roi": 0.07,
        "profit_trade_min_item_value": 50.0,
        "profit_trade_require_c5_recent_sales": False,
        "profit_trade_require_c5_market_depth": False,
        "profit_trade_manual_review_roi": 9_999.0,
        "profit_trade_sticker_slab_status": "active",
        "profit_trade_sticker_status": "active",
        "profit_trade_balance_discount": 0.69,
        "guadao_max_listing_ratio": 0.69,
    }
    values.update(overrides)
    return StrategyConfig(**values)


class ProfitTradeAccountSelectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.temp_dir.name) / "unused-profit-trade-selection.db",
            steam_cookies="sessionid=x; steamLoginSecure=y",
        )
        self.accounts = [
            Account(id="a", name="self", steam_id64="steam-a", cookies="cookies-a"),
            Account(id="b", name="small", steam_id64="steam-b", cookies="cookies-b"),
            Account(id="c", name="large", steam_id64="steam-c", cookies="cookies-c"),
        ]
        self.original_builder = profit_trade_module._build_steam_client_for_account
        self.built_account_ids: list[str] = []
        self.wallet_clients: dict[str, FakeSteamBuyClient] = {}

    def tearDown(self) -> None:
        profit_trade_module._build_steam_client_for_account = self.original_builder
        self.temp_dir.cleanup()

    def _install_wallets(self, wallets: dict[str, float]) -> None:
        def fake_builder(_: Settings, account: Account) -> FakeSteamBuyClient:
            self.built_account_ids.append(str(account.id))
            client = self.wallet_clients.get(str(account.id))
            if client is None:
                client = FakeSteamBuyClient(wallet_balance=wallets[str(account.id)])
                self.wallet_clients[str(account.id)] = client
            client.account_id = account.id
            client.steam_id64 = str(account.steam_id64)
            return client

        profit_trade_module._build_steam_client_for_account = fake_builder

    def _seed_shared_balances(self, balances: dict[str, float]) -> None:
        for account in self.accounts:
            if account.id not in balances:
                continue
            update_steam_account_balance_snapshot(
                self.settings,
                account=account,
                wallet={
                    "balance": balances[account.id],
                    "delayed_balance": 0.0,
                    "currency": "CNY",
                    "currency_id": 23,
                },
            )

    def test_select_buy_account_prefers_a_asset_account_when_balance_is_enough(self) -> None:
        self._install_wallets({"a": 100.0, "b": 20.0, "c": 300.0})
        self._seed_shared_balances({"a": 100.0, "b": 20.0, "c": 300.0})

        selected = profit_trade_module._select_steam_buy_account(
            self.settings,
            required_balance=19.0,
            preferred_steam_id="steam-a",
            account_store=FakeAccountStore(self.accounts),
        )

        self.assertIsNotNone(selected.account)
        self.assertEqual("a", selected.account.id)
        self.assertFalse(selected.wallet_is_live)
        self.assertEqual([], self.wallet_clients["a"].wallet_balance_calls)

    def test_live_buy_account_checks_only_preferred_account_when_it_can_pay(self) -> None:
        self._install_wallets({"a": 100.0, "b": 200.0, "c": 300.0})
        self._seed_shared_balances({"a": 100.0, "b": 200.0, "c": 300.0})

        selected = profit_trade_module._select_live_steam_buy_account(
            self.settings,
            required_balance=19.0,
            preferred_steam_id="steam-a",
            account_store=FakeAccountStore([self.accounts[1], self.accounts[0], self.accounts[2]]),
        )

        self.assertEqual("a", selected.account.id)
        self.assertEqual(["a"], self.built_account_ids)
        self.assertEqual(
            [{"execution_priority": True}],
            self.wallet_clients["a"].wallet_balance_calls,
        )

    def test_select_buy_account_uses_shared_cache_to_pick_one_fallback(self) -> None:
        self._install_wallets({"a": 10.0, "b": 300.0, "c": 25.0})
        self._seed_shared_balances({"a": 10.0, "b": 300.0, "c": 25.0})

        selected = profit_trade_module._select_steam_buy_account(
            self.settings,
            required_balance=19.0,
            preferred_steam_id="steam-a",
            account_store=FakeAccountStore(self.accounts),
        )

        self.assertEqual("c", selected.account.id)
        self.assertEqual(["c"], self.built_account_ids)
        self.assertEqual([], self.wallet_clients["c"].wallet_balance_calls)
        self.assertNotIn("a", self.wallet_clients)
        self.assertNotIn("b", self.wallet_clients)
        self.assertFalse(selected.wallet_is_live)

    def test_live_buy_account_checks_a_then_cached_smallest_fallback(self) -> None:
        self._install_wallets({"a": 10.0, "b": 300.0, "c": 25.0})
        self._seed_shared_balances({"a": 10.0, "b": 300.0, "c": 25.0})

        selected = profit_trade_module._select_live_steam_buy_account(
            self.settings,
            required_balance=19.0,
            preferred_steam_id="steam-a",
            account_store=FakeAccountStore(self.accounts),
        )

        self.assertEqual("c", selected.account.id)
        self.assertEqual(["a", "c"], self.built_account_ids)
        self.assertEqual([{"execution_priority": True}], self.wallet_clients["a"].wallet_balance_calls)
        self.assertEqual([{"execution_priority": True}], self.wallet_clients["c"].wallet_balance_calls)
        self.assertNotIn("b", self.wallet_clients)
        self.assertTrue(selected.wallet_is_live)

    def test_live_check_starts_with_the_account_selected_by_cache(self) -> None:
        self._install_wallets({"a": 10.0, "b": 300.0, "c": 25.0})
        self._seed_shared_balances({"a": 10.0, "b": 300.0, "c": 25.0})
        store = FakeAccountStore(self.accounts)

        cached_selection = profit_trade_module._select_steam_buy_account(
            self.settings,
            required_balance=19.0,
            preferred_steam_id="steam-a",
            account_store=store,
        )
        live_selection = profit_trade_module._select_live_steam_buy_account(
            self.settings,
            required_balance=19.0,
            preferred_steam_id=str(cached_selection.account.steam_id64),
            account_store=store,
        )

        self.assertEqual("c", cached_selection.account.id)
        self.assertEqual("c", live_selection.account.id)
        self.assertEqual(["c", "c"], self.built_account_ids)
        self.assertEqual([{"execution_priority": True}], self.wallet_clients["c"].wallet_balance_calls)
        self.assertNotIn("a", self.wallet_clients)
        self.assertNotIn("b", self.wallet_clients)

    def test_select_buy_account_falls_back_to_smallest_sufficient_balance(self) -> None:
        self._install_wallets({"a": 10.0, "b": 25.0, "c": 300.0})
        self._seed_shared_balances({"a": 10.0, "b": 25.0, "c": 300.0})

        selected = profit_trade_module._select_steam_buy_account(
            self.settings,
            required_balance=19.0,
            preferred_steam_id="steam-a",
            account_store=FakeAccountStore(self.accounts),
        )

        self.assertIsNotNone(selected.account)
        self.assertEqual("b", selected.account.id)

    def test_reserved_balance_is_not_spendable_for_account_selection(self) -> None:
        self._install_wallets({"a": 500.0, "b": 260.0, "c": 1000.0})
        self._seed_shared_balances({"a": 500.0, "b": 260.0, "c": 1000.0})

        selected = profit_trade_module._select_steam_buy_account(
            self.settings,
            required_balance=250.0,
            preferred_steam_id="steam-a",
            account_store=FakeAccountStore(self.accounts),
            account_reserved_balances={"steam-a": 300.0},
        )

        self.assertIsNotNone(selected.account)
        self.assertEqual("b", selected.account.id)
        self.assertEqual(0.0, selected.reserved_balance)
        self.assertEqual(260.0, selected.spendable_balance)

    def test_reserved_balance_config_round_trips(self) -> None:
        config = profit_config(
            profit_trade_account_reserved_balances={"steam-a": 300.0}
        )

        restored = StrategyConfig.from_dict(config.to_dict())

        self.assertEqual(
            {"steam-a": 300.0},
            restored.profit_trade_account_reserved_balances,
        )

    def test_completed_buy_verification_updates_shared_balance_snapshot(self) -> None:
        account = self.accounts[0]
        client = FakeSteamBuyClient(total=1000, wallet_balance=100.0)
        client.account_id = account.id
        client.steam_id64 = str(account.steam_id64)
        client.buy_listing()

        verification = profit_trade_module._verify_steam_buy_completed_with_inventory(
            client,
            self.settings,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            method="buylisting",
            expected_total=1000,
            wallet_before_balance=100.0,
            buy_order_id=None,
            before_asset_ids=[],
            steam_id=str(account.steam_id64),
            attempts=1,
            delay_seconds=0.0,
            check_purchase_history=False,
        )

        cached = load_steam_account_balances(
            self.settings,
            account_store=FakeAccountStore([account]),
        )
        self.assertTrue(verification.confirmed)
        self.assertEqual(90.0, cached["accounts"][0]["realBalance"])
        self.assertEqual("ok", cached["accounts"][0]["status"])


class ProfitTradeScanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.original_verify_attempts = profit_trade_module.STEAM_BUY_VERIFY_ATTEMPTS
        self.original_verify_delay_seconds = profit_trade_module.STEAM_BUY_VERIFY_DELAY_SECONDS
        profit_trade_module.STEAM_BUY_VERIFY_ATTEMPTS = 1
        profit_trade_module.STEAM_BUY_VERIFY_DELAY_SECONDS = 0.0
        profit_trade_module._STEAM_BUY_FAILED_LISTING_BLACKLIST.clear()
        self.original_c5_builder = profit_trade_module._build_profit_trade_c5_client
        self.default_c5_client = FakeC5SaleClient()
        profit_trade_module._build_profit_trade_c5_client = (
            lambda _settings, **_context: self.default_c5_client
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.temp_dir.name) / "assistant.db",
            c5_api_key="c5-key",
            steam_cookies="sessionid=x; steamLoginSecure=y",
        )
        self.config = profit_config()
        save_strategy_config(self.settings, self.config)
        self.inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-a",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "name": "AK-47 红线（略有磨损）",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 88.0,
                    "token": "token-a",
                    "styleToken": "style-a",
                }
            ],
        }

    def tearDown(self) -> None:
        profit_trade_module.STEAM_BUY_VERIFY_ATTEMPTS = self.original_verify_attempts
        profit_trade_module.STEAM_BUY_VERIFY_DELAY_SECONDS = self.original_verify_delay_seconds
        profit_trade_module._STEAM_BUY_FAILED_LISTING_BLACKLIST.clear()
        profit_trade_module._build_profit_trade_c5_client = self.original_c5_builder
        self.temp_dir.cleanup()

    def test_scan_profit_trade_opportunity_uses_discounted_steam_cost(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
        )

        self.assertEqual(1, report.opportunity_count)
        opportunity = report.opportunities[0]
        self.assertEqual("asset-a", opportunity.asset_id)
        self.assertAlmostEqual(100.0, opportunity.steam_buy_price)
        self.assertAlmostEqual(69.0, opportunity.steam_real_cost)
        self.assertAlmostEqual(88.803, opportunity.c5_expected_net_price)
        self.assertAlmostEqual(19.803, opportunity.expected_profit)
        self.assertAlmostEqual(88.803 / 100.0 - 0.69, opportunity.expected_roi)
        self.assertEqual("disabled", opportunity.liquidity_status)

    def test_scan_evaluates_all_eligible_item_types_regardless_of_legacy_scan_cap(self) -> None:
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-high",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "name": "High reference price",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 100.0,
                },
                {
                    "assetId": "asset-low",
                    "marketHashName": "USP-S | Tropical Breeze (Factory New)",
                    "name": "Low reference price",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 50.0,
                },
            ],
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=inventory_payload,
            market_service=FakeProfitMarketService(),
            scan_max_items=1,
            limit=20,
        )

        self.assertEqual(2, report.evaluated_count)
        self.assertFalse(any("Prefiltered" in note for note in report.notes))

    def test_streaming_scan_locks_fast_opportunity_before_slow_orderbook_finishes(self) -> None:
        slow_release = threading.Event()
        callback_seen = threading.Event()
        c5_client = StreamingProfitC5Client()
        market_service = MarketService(
            steam_market_client=StreamingProfitSteamClient(slow_release),
            c5_client=c5_client,
            include_c5_purchase_prices=False,
            fallback_max_workers=2,
        )
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-fast",
                    "marketHashName": "Fast Item",
                    "name": "快速物品",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 100.0,
                    "token": "token-fast",
                    "styleToken": "style-fast",
                },
                {
                    "assetId": "asset-slow",
                    "marketHashName": "Slow Item",
                    "name": "慢速物品",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 90.0,
                    "token": "token-slow",
                    "styleToken": "style-slow",
                },
            ],
        }
        callback_trade_ids: list[int] = []
        result_holder: dict[str, object] = {}

        def on_locked_trade_ready(trade_id: int, _: object) -> None:
            callback_trade_ids.append(trade_id)
            callback_seen.set()

        def run_scan() -> None:
            try:
                result_holder["report"] = scan_profit_trade_opportunities(
                    self.settings,
                    self.config,
                    inventory_payload=inventory_payload,
                    market_service=market_service,
                    c5_client=c5_client,
                    record=True,
                    lock_asset=True,
                    limit=1,
                    on_locked_trade_ready=on_locked_trade_ready,
                )
            except BaseException as exc:  # surfaced in the main test thread
                result_holder["error"] = exc

        scan_thread = threading.Thread(target=run_scan, daemon=True)
        scan_thread.start()
        try:
            self.assertTrue(callback_seen.wait(timeout=2), result_holder.get("error"))
            self.assertTrue(scan_thread.is_alive(), "full scan finished before the slow item was released")
            self.assertEqual(1, len(callback_trade_ids))
            self.assertEqual(1, len(c5_client.price_batch_calls))
            self.assertEqual(1, len(c5_client.statistics_calls))
        finally:
            slow_release.set()
            scan_thread.join(timeout=5)

        self.assertFalse(scan_thread.is_alive())
        self.assertNotIn("error", result_holder)
        report = result_holder["report"]
        self.assertEqual(2, report.evaluated_count)
        self.assertEqual(callback_trade_ids, report.locked_trade_ids)

    def test_profit_trade_uses_own_balance_discount_not_guadao_ratio(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_balance_discount=0.66,
                guadao_max_listing_ratio=0.90,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
        )

        self.assertEqual(1, report.opportunity_count)
        opportunity = report.opportunities[0]
        self.assertAlmostEqual(66.0, opportunity.steam_real_cost)
        self.assertAlmostEqual(88.803 - 66.0, opportunity.expected_profit)
        self.assertAlmostEqual(88.803 / 100.0 - 0.66, opportunity.expected_roi)

    def test_c5_initial_and_reprice_discounts_floor_to_cent_without_amount_caps(self) -> None:
        config = profit_config(
            profit_trade_initial_listing_discount_pct=0.33,
            profit_trade_reprice_discount_pct=1.0,
        )

        self.assertAlmostEqual(
            99.67,
            profit_trade_module._profit_trade_initial_listing_price(
                config,
                competitor_reference_price=100.01,
                fallback_price=1.0,
            ),
        )
        self.assertAlmostEqual(
            9.96,
            profit_trade_module._profit_trade_initial_listing_price(
                config,
                competitor_reference_price=10.0,
                fallback_price=1.0,
            ),
        )
        self.assertAlmostEqual(
            4983.5,
            profit_trade_module._profit_trade_initial_listing_price(
                config,
                competitor_reference_price=5000.0,
                fallback_price=1.0,
            ),
        )
        self.assertAlmostEqual(
            99.0,
            profit_trade_module._profit_trade_competitive_listing_price(
                config,
                current_lowest_price=100.01,
                fallback_price=100.01,
            ),
        )
        self.assertAlmostEqual(
            9.9,
            profit_trade_module._profit_trade_competitive_listing_price(
                config,
                current_lowest_price=10.0,
                fallback_price=10.0,
            ),
        )
        self.assertAlmostEqual(
            4950.0,
            profit_trade_module._profit_trade_competitive_listing_price(
                config,
                current_lowest_price=5000.0,
                fallback_price=5000.0,
            ),
        )

    def test_market_evaluation_uses_price_batch_lowest_and_cannot_be_overridden_by_filtered_rows(self) -> None:
        evaluation = profit_trade_module._build_market_evaluation(
            config=profit_config(
                profit_trade_balance_discount=0.69,
                profit_trade_initial_listing_discount_pct=0.33,
            ),
            item_type={
                "market_hash_name": "M4A4 | Hellish (Field-Tested)",
                "name_cn": "M4A4 | 炼狱之火（久经沙场）",
                "inventory_count": 1,
                "tradable_count": 1,
            },
            state=MarketState(
                market_hash_name="M4A4 | Hellish (Field-Tested)",
                name_cn="M4A4 | 炼狱之火（久经沙场）",
                c5_sell_price=134.99,
                c5_sell_count=731,
                c5_price_source="c5_batch",
                steam_sell_price=185.57,
                steam_price_source="steam_orderbook",
            ),
            c5_pricing={
                # This reproduces the incomplete filtered listing result caused by
                # acceptBargain=false.  It must never override price_batch.
                "effectiveReferencePrice": 139.54,
                "source": "filtered_market_products_search",
            },
        )

        self.assertIsNotNone(evaluation)
        self.assertAlmostEqual(134.54, evaluation.c5_listing_price)
        self.assertAlmostEqual(0.027759336099585, evaluation.expected_roi)
        self.assertEqual("c5_batch", evaluation.c5_price_source)

    def test_scan_can_record_candidate_without_locking_asset(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
        )

        self.assertEqual(1, len(report.created_trade_ids))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(report.created_trade_ids[0])
            self.assertIsNotNone(trade)
            self.assertEqual("candidate", trade["status"])
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_scan_skips_protected_asset_id(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_protected_asset_ids=["asset-a"]),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
        )

        self.assertEqual(0, report.opportunity_count)

    def test_scan_skips_protected_market_hash_name(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_protected_market_hash_names=["AK-47 | Redline (Field-Tested)"]),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
        )

        self.assertEqual(0, report.opportunity_count)

    def test_scan_does_not_builtin_block_sticker_slab(self) -> None:
        payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-slab",
                    "marketHashName": "Sticker Slab | Evil Geniuses (Holo) | Stockholm 2021",
                    "name": "印花板 | Evil Geniuses （全息） | 2021年斯德哥尔摩锦标赛",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 500.0,
                    "token": "token-slab",
                    "styleToken": "style-slab",
                }
            ],
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=payload,
            market_service=FakeProfitMarketService(),
            record=True,
        )

        self.assertEqual(1, report.opportunity_count)
        self.assertEqual(1, len(report.created_trade_ids))

    def test_scan_blocks_sticker_slab_when_status_is_blocked(self) -> None:
        payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-slab",
                    "marketHashName": "Sticker Slab | Evil Geniuses (Holo) | Stockholm 2021",
                    "name": "印花板 | Evil Geniuses （全息） | 2021年斯德哥尔摩锦标赛",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 500.0,
                    "token": "token-slab",
                    "styleToken": "style-slab",
                }
            ],
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_sticker_slab_status="blocked"),
            inventory_payload=payload,
            market_service=FakeProfitMarketService(),
            record=True,
        )

        self.assertEqual(0, report.opportunity_count)
        self.assertEqual([], report.created_trade_ids)

    def test_scan_blocks_sticker_when_status_is_blocked(self) -> None:
        payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-sticker",
                    "marketHashName": "Sticker | MOUZ (Holo) | Budapest 2025",
                    "name": "印花 | MOUZ（全息）| 2025年布达佩斯锦标赛",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 90.0,
                    "token": "token-sticker",
                    "styleToken": "style-sticker",
                }
            ],
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_sticker_status="blocked"),
            inventory_payload=payload,
            market_service=FakeProfitMarketService(),
            record=True,
        )

        self.assertEqual(0, report.opportunity_count)
        self.assertEqual([], report.created_trade_ids)

    def test_scan_requires_c5_recent_sale_risk_when_enabled(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_require_c5_recent_sales=True),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            c5_client=FakeC5SaleClient(),
        )

        self.assertEqual(0, report.opportunity_count)

    def test_scan_keeps_recent_sale_as_evidence_without_capping_current_listing_roi(self) -> None:
        c5_client = FakeC5SaleClient()
        c5_client.statistics = {
            "AK-47 | Redline (Field-Tested)": {
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "recentSoldNetPrice": 88.0,
                "recentSoldCount": 5,
            }
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_require_c5_recent_sales=True),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            c5_client=c5_client,
        )

        self.assertEqual(1, report.opportunity_count)
        opportunity = report.opportunities[0]
        self.assertEqual("passed", opportunity.liquidity_status)
        self.assertEqual(5, opportunity.c5_recent_sold_count)
        self.assertAlmostEqual(88.0, opportunity.c5_recent_sold_net_price)
        self.assertAlmostEqual(88.803, opportunity.c5_expected_net_price)
        self.assertAlmostEqual(88.803 / 100.0 - 0.69, opportunity.expected_roi)

    def test_c5_sell_count_is_not_recent_sold_count(self) -> None:
        c5_client = FakeC5SaleClient()
        c5_client.statistics = {
            "AK-47 | Redline (Field-Tested)": {
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "sellPrice": 88.0,
                "sellCount": 5,
            }
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_require_c5_recent_sales=True),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            c5_client=c5_client,
        )

        self.assertEqual(0, report.opportunity_count)

    def test_scan_requires_c5_market_depth_when_enabled(self) -> None:
        c5_client = FakeC5SaleClient(price_batch_count=2)
        c5_client.statistics = {
            "AK-47 | Redline (Field-Tested)": {
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "sellPrice": 90.0,
                "sellCount": 2,
                "purchaseMaxPrice": 80.0,
                "purchaseCount": 1,
            }
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_require_c5_market_depth=True,
                profit_trade_c5_min_on_sale_count=3,
                profit_trade_manual_review_roi=9_999.0,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            c5_client=c5_client,
        )

        self.assertEqual(0, report.opportunity_count)

    def test_scan_uses_c5_market_depth_when_recent_sales_are_disabled(self) -> None:
        c5_client = FakeC5SaleClient()
        c5_client.statistics = {
            "AK-47 | Redline (Field-Tested)": {
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "sellPrice": 90.0,
                "sellCount": 3,
                "purchaseMaxPrice": 80.0,
                "purchaseCount": 1,
            }
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_require_c5_market_depth=True,
                profit_trade_require_c5_recent_sales=False,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            c5_client=c5_client,
        )

        self.assertEqual(1, report.opportunity_count)
        opportunity = report.opportunities[0]
        self.assertEqual("passed", opportunity.liquidity_status)
        self.assertEqual(90.0, opportunity.c5_current_sell_price)
        self.assertEqual(3, opportunity.c5_on_sale_count)

    def test_scan_marks_high_roi_for_manual_review_and_sends_serverchan(self) -> None:
        c5_client = FakeC5SaleClient(price_batch_price=100.0)
        c5_client.statistics = {
            "AK-47 | Redline (Field-Tested)": {
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "sellPrice": 100.0,
                "sellCount": 3,
                "purchaseMaxPrice": 80.0,
                "purchaseCount": 1,
            }
        }
        original_serverchan = profit_trade_module.ServerChanClient
        FakeServerChan.messages = []
        profit_trade_module.ServerChanClient = FakeServerChan
        self.settings.serverchan_sendkey = "send-key"
        try:
            report = scan_profit_trade_opportunities(
                self.settings,
                profit_config(
                    profit_trade_require_c5_market_depth=True,
                    profit_trade_require_c5_recent_sales=False,
                    profit_trade_manual_review_roi=0.20,
                ),
                inventory_payload=self.inventory_payload,
                market_service=FakeProfitMarketService(c5_sell_price=100.0),
                c5_client=c5_client,
                record=True,
                lock_asset=True,
            )
        finally:
            profit_trade_module.ServerChanClient = original_serverchan

        self.assertEqual(1, report.opportunity_count)
        self.assertEqual([], report.locked_trade_ids)
        self.assertEqual(1, len(report.created_trade_ids))
        self.assertEqual(1, len(FakeServerChan.messages))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(report.created_trade_ids[0])
            self.assertEqual("manual_required", trade["status"])
            self.assertIn("manual review threshold", trade["error"])
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_scan_recent_sale_premium_does_not_cap_current_listing_price(self) -> None:
        c5_client = FakeC5SaleClient()
        c5_client.statistics = {
            "AK-47 | Redline (Field-Tested)": {
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "recentSoldNetPrice": 80.0,
                "recentSoldCount": 5,
            }
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_require_c5_recent_sales=True,
                profit_trade_c5_max_listing_premium_pct=3.0,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            c5_client=c5_client,
        )

        self.assertEqual(1, report.opportunity_count)
        self.assertAlmostEqual(88.803, report.opportunities[0].c5_expected_net_price)

    def test_dashboard_cancels_existing_candidate_after_asset_is_protected(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
        )
        trade_id = report.created_trade_ids[0]

        payload = build_profit_trade_dashboard_payload(
            self.settings,
            config=profit_config(profit_trade_protected_asset_ids=["asset-a"]),
        )

        self.assertEqual([], payload["trades"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("cancelled", trade["status"])
            self.assertIn("protected assetId", trade["note"])
        finally:
            db.close()

    def test_update_profit_trade_protection_persists_asset_id(self) -> None:
        updated = update_profit_trade_protection(
            self.settings,
            action="add",
            kind="asset",
            value="asset-a",
        )

        self.assertEqual(["asset-a"], updated.profit_trade_protected_asset_ids)
        report = scan_profit_trade_opportunities(
            self.settings,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
        )
        self.assertEqual(0, report.opportunity_count)

        updated = update_profit_trade_protection(
            self.settings,
            action="remove",
            kind="asset",
            value="asset-a",
        )
        self.assertEqual([], updated.profit_trade_protected_asset_ids)

    def test_dashboard_returns_chinese_item_name_when_available(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
        )

        payload = build_profit_trade_dashboard_payload(self.settings)

        self.assertEqual(report.created_trade_ids[0], payload["trades"][0]["id"])
        self.assertEqual("AK-47 红线（略有磨损）", payload["trades"][0]["name"])

    def test_dashboard_returns_chinese_names_for_protected_kinds(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.upsert_items(
                [
                    CatalogItem(
                        market_hash_name="AK-47 | Redline (Field-Tested)",
                        name_cn="AK-47 红线（略有磨损）",
                        c5_item_id=None,
                        steam_item_id=None,
                        raw_json={},
                    )
                ]
            )
        finally:
            db.close()
        config = profit_config(
            profit_trade_protected_market_hash_names=[
                "AK-47 | Redline (Field-Tested)",
            ]
        )

        payload = build_profit_trade_dashboard_payload(self.settings, config=config)

        self.assertEqual(
            [
                {
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "name": "AK-47 红线（略有磨损）",
                }
            ],
            payload["config"]["protectedMarketHashNameItems"],
        )

    def test_dashboard_exposes_steam_purchase_time_for_date_filtering(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
        )
        trade_id = report.created_trade_ids[0]
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(trade["note"])
            note["steamBuySucceededAt"] = "2026-07-03T01:02:03+00:00"
            db.update_profit_trade(
                trade_id,
                note=profit_trade_module._build_note(note),
            )
        finally:
            db.close()

        payload = build_profit_trade_dashboard_payload(self.settings)
        trade = next(row for row in payload["trades"] if row["id"] == trade_id)

        self.assertEqual("2026-07-03T01:02:03+00:00", trade["steamBoughtAt"])
        self.assertEqual("steamBuySucceededAt", trade["steamBoughtAtSource"])

    def test_steam_purchase_time_falls_back_to_unverified_request_time(self) -> None:
        bought_at, source = profit_trade_module._profit_trade_steam_bought_at(
            {
                "steamBuyUnverifiedAt": "2026-07-08T06:00:49+00:00",
                "steamBuyRecoveredAt": "2026-07-08T07:05:03+00:00",
            }
        )

        self.assertEqual("2026-07-08T06:00:49+00:00", bought_at)
        self.assertEqual("steamBuyUnverifiedAt", source)

    def test_lock_profit_trade_reserves_asset_and_moves_progress(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
        )
        trade_id = report.created_trade_ids[0]

        result = lock_profit_trade(self.settings, trade_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual("locked", result["trade"]["status"])
        self.assertEqual("asset_locked", result["trade"]["stepKey"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertIsNotNone(reservation)
            self.assertEqual("active", reservation["status"])
            self.assertEqual(trade_id, reservation["operation_id"])
        finally:
            db.close()

    def test_dashboard_cancels_and_hides_expired_pre_buy_lock(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
            lock_asset=True,
        )
        trade_id = report.locked_trade_ids[0]
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.release_asset_reservation(
                asset_id="asset-a",
                owner="profit_trade",
                reason="test expired",
            )
        finally:
            db.close()

        payload = build_profit_trade_dashboard_payload(self.settings)

        self.assertEqual([], payload["trades"])
        self.assertEqual(0, payload["summary"]["failedCount"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("cancelled", trade["status"])
            self.assertIsNone(trade["error"])
        finally:
            db.close()

    def test_dashboard_cancels_and_hides_stale_pre_buy_manual_trade(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.release_asset_reservation(
                asset_id="asset-a",
                owner="profit_trade",
                reason="test missing reservation",
            )
            existing = db.get_profit_trade(trade_id)
            existing_note = profit_trade_module._read_note(existing["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                error="A asset reservation is not active before Steam buy",
            )
        finally:
            db.close()

        payload = build_profit_trade_dashboard_payload(self.settings)

        self.assertEqual([], payload["trades"])
        self.assertEqual(0, payload["summary"]["failedCount"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("cancelled", trade["status"])
            self.assertIsNone(trade["error"])
        finally:
            db.close()

    def test_scan_record_refreshes_existing_candidate_for_same_asset(self) -> None:
        first = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
            lock_asset=False,
        )

        second = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
            lock_asset=False,
        )

        self.assertEqual(1, len(first.created_trade_ids))
        self.assertEqual(1, len(second.created_trade_ids))
        self.assertNotEqual(first.created_trade_ids[0], second.created_trade_ids[0])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            old_trade = db.get_profit_trade(first.created_trade_ids[0])
            new_trade = db.get_profit_trade(second.created_trade_ids[0])
            self.assertEqual("cancelled", old_trade["status"])
            self.assertEqual("candidate", new_trade["status"])
        finally:
            db.close()

    def _create_locked_trade(self) -> int:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
            lock_asset=True,
        )
        self.assertEqual(1, len(report.locked_trade_ids))
        return report.locked_trade_ids[0]

    def _create_legacy_unverified_createbuyorder_trade(self) -> int:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.release_asset_reservation(
                asset_id="asset-a",
                owner="profit_trade",
                reason="simulate old manual_required buy verification failure",
            )
            db.upsert_inventory_assets([
                {
                    "assetId": "asset-a",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "steamId": "steam-a",
                    "ifTradable": True,
                },
                {
                    "assetId": "asset-b",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "steamId": "steam-a",
                    "ifTradable": False,
                },
            ])
            existing = db.get_profit_trade(trade_id)
            existing_note = profit_trade_module._read_note(existing["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                steam_listing_id="buy-order-1",
                error="Steam buy request succeeded but purchase completion is not verified: matching Steam buy order is still active",
                note=profit_trade_module._build_note(
                    {
                        **existing_note,
                        "steamBuyMethod": "createbuyorder",
                        "steamBuyOrderId": "buy-order-1",
                        "steamBuyUnverifiedAt": "2026-07-08T06:00:49+00:00",
                        "walletDelta": 100.0,
                        "steamBuyVerifiedBy": ["wallet_balance_delta"],
                        "steamId": "steam-a",
                    }
                ),
            )
        finally:
            db.close()
        return trade_id

    def test_recover_unverified_createbuyorder_from_local_inventory_relocks_a(self) -> None:
        trade_id = self._create_legacy_unverified_createbuyorder_trade()

        result = recover_unverified_profit_trade_steam_buys(self.settings, config=self.config)

        self.assertEqual([trade_id], result["recoveredTradeIds"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("steam_bought", trade["status"])
            self.assertIsNone(trade["error"])
            self.assertEqual("asset-b", trade["b_asset_id"])
            self.assertIsNone(trade["completed_at"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual(["asset-b"], note["newInventoryAssetIds"])
            self.assertIn("local_inventory_reconciliation", note["steamBuyVerifiedBy"])
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertIsNotNone(reservation)
            self.assertEqual("active", reservation["status"])
            self.assertEqual(trade_id, reservation["operation_id"])
        finally:
            db.close()

    def test_recover_cancelled_legacy_buy_order_from_official_purchase_history(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            db.release_asset_reservation(
                asset_id="asset-a",
                owner="profit_trade",
                reason="simulate legacy unsafe dismiss",
            )
            db.update_profit_trade(
                trade_id,
                status="cancelled",
                step_key="steam_bought",
                step_index=3,
                steam_listing_id="buy-order-old",
                steam_buy_price=100.0,
                error="user dismissed old unverified order",
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "steamBuyMethod": "createbuyorder",
                        "steamBuyOrderId": "buy-order-old",
                        "steamBuyUnverifiedAt": "2026-07-08T09:37:22+00:00",
                        "steamId": "steam-a",
                        "steamAccountId": "account-a",
                        "beforeAssetIds": ["asset-a"],
                    }
                ),
            )
        finally:
            db.close()

        client = FakeHistoricalPurchaseClient(total=10000)
        result = recover_unverified_profit_trade_steam_buys(
            self.settings,
            config=self.config,
            steam_client=client,
            remote_audit=True,
        )

        self.assertEqual([trade_id], result["recoveredTradeIds"])
        self.assertEqual(1, len(client.purchase_receipt_calls))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("steam_bought", trade["status"])
            self.assertEqual("asset-b-history", trade["b_asset_id"])
            self.assertIsNone(trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("market_history_event_type_4", note["steamBuyRecoveredBy"])
            self.assertEqual("history-purchase-1", note["steamPurchaseReceipt"]["purchaseId"])
            self.assertIsNotNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_remote_recovery_skips_cancelled_order_with_confirmed_cancellation(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            db.release_asset_reservation(
                asset_id="asset-a",
                owner="profit_trade",
                reason="simulate confirmed cancellation",
            )
            db.update_profit_trade(
                trade_id,
                status="cancelled",
                step_key="steam_bought",
                step_index=3,
                steam_listing_id="buy-order-confirmed",
                steam_buy_price=100.0,
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "steamBuyMethod": "createbuyorder",
                        "steamBuyOrderId": "buy-order-confirmed",
                        "steamBuyUnverifiedAt": "2026-07-10T01:00:00+00:00",
                        "steamBuyOrderCancellationConfirmedAt": "2026-07-10T01:00:02+00:00",
                        "steamId": "steam-a",
                        "steamAccountId": "account-a",
                    }
                ),
            )
        finally:
            db.close()

        client = FakeHistoricalPurchaseClient(total=10000)
        result = recover_unverified_profit_trade_steam_buys(
            self.settings,
            config=self.config,
            steam_client=client,
            remote_audit=True,
        )

        self.assertEqual([], result["recoveredTradeIds"])
        self.assertEqual([], client.purchase_receipt_calls)

    def test_dashboard_recovers_legacy_unverified_createbuyorder_before_render(self) -> None:
        trade_id = self._create_legacy_unverified_createbuyorder_trade()

        payload = build_profit_trade_dashboard_payload(self.settings, config=self.config)

        trade = next(row for row in payload["trades"] if row["id"] == trade_id)
        self.assertEqual("steam_bought", trade["status"])
        self.assertIsNone(trade["error"])
        self.assertEqual("asset-b", trade["bAssetId"])
        self.assertFalse(trade["requiresManualAction"])

    def test_run_once_lists_c5_after_recovering_legacy_unverified_createbuyorder(self) -> None:
        trade_id = self._create_legacy_unverified_createbuyorder_trade()
        c5_client = FakeC5SaleClient()

        report = run_profit_trade_once(
            self.settings,
            profit_config(profit_trade_allow_real_execution=True, profit_trade_max_buy_per_cycle=1),
            inventory_payload={"source": "fixture", "list": []},
            market_service=FakeProfitMarketService(),
            steam_client=FakeSteamBuyClient(total=10000),
            c5_client=c5_client,
        )

        self.assertIn(trade_id, report.listed_trade_ids, report)
        self.assertEqual(1, len(c5_client.sale_calls), report)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
        finally:
            db.close()
    def test_buy_step_requires_real_execution_flag(self) -> None:
        trade_id = self._create_locked_trade()

        with self.assertRaisesRegex(RuntimeError, "allowRealExecution"):
            execute_profit_trade_buy(
                self.settings,
                trade_id,
                config=profit_config(profit_trade_allow_real_execution=False),
                steam_client=FakeSteamBuyClient(),
            )

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("locked", trade["status"])
        finally:
            db.close()

    def test_buy_step_updates_trade_and_keeps_a_asset_reserved(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("listing-1", result["trade"]["steamListingId"])
        self.assertAlmostEqual(100.0, result["trade"]["steamBuyPrice"])
        self.assertEqual(1, len(client.buy_calls))
        self.assertEqual(0, len(client.create_buy_order_calls))
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual(1, len(client.order_book_calls))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertIsNotNone(reservation)
            self.assertEqual("active", reservation["status"])
            self.assertIsNone(reservation["reserved_until"])
        finally:
            db.close()

    def test_buy_step_retries_next_listing_when_first_listing_was_already_purchased(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeStaleListingThenNextSteamBuyClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("fresh-listing", result["trade"]["steamListingId"])
        self.assertEqual(["stale-listing", "fresh-listing"], [call["listing_id"] for call in client.buy_calls])
        self.assertEqual(2, len(client.search_listing_calls))
        self.assertTrue(all(call.get("bounded_retry") is False for call in client.search_listing_calls))
        self.assertEqual(0, len(client.create_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("buylisting", note["steamBuyMethod"])
        self.assertEqual(["stale-listing"], note["failedSteamListingIds"])
        self.assertEqual("stale-listing", note["staleSteamListingAttempts"][0]["listingId"])

    def test_buy_step_retries_next_listing_when_first_listing_was_removed(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeRemovedListingThenNextSteamBuyClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("fresh-listing", result["trade"]["steamListingId"])
        self.assertEqual(["stale-listing", "fresh-listing"], [call["listing_id"] for call in client.buy_calls])
        note = result["trade"]["note"]
        self.assertEqual("buylisting", note["steamBuyMethod"])
        self.assertEqual(["stale-listing"], note["failedSteamListingIds"])
        self.assertIn("可能已被移除", note["staleSteamListingAttempts"][0]["error"])

    def test_buy_step_falls_back_to_createbuyorder_after_excluding_stale_listing(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeStaleListingOnlyThenBuyOrderClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("buy-order-1", result["trade"]["steamListingId"])
        self.assertEqual(1, len(client.buy_calls))
        self.assertEqual(1, len(client.create_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("createbuyorder", note["steamBuyMethod"])
        self.assertEqual(["stale-listing"], note["failedSteamListingIds"])
        self.assertIn("wallet_balance_delta", note["steamBuyVerifiedBy"])
    def test_buy_step_uses_createbuyorder_for_sticker_even_with_listing_rows(self) -> None:
        self.inventory_payload["list"][0]["marketHashName"] = "Sticker | Cluck"
        self.inventory_payload["list"][0]["name"] = "印花 | 咯咯"
        trade_id = self._create_locked_trade()
        client = FakeStickerListingSteamBuyClient(total=713)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("buy-order-1", result["trade"]["steamListingId"])
        self.assertEqual(0, len(client.buy_calls))
        self.assertEqual(0, len(client.search_listing_calls))
        self.assertEqual(1, len(client.create_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("createbuyorder", note["steamBuyMethod"])
        self.assertIsNone(note["steamListingId"])
        self.assertEqual("buy-order-1", note["steamBuyOrderId"])
    def test_buy_step_uses_createbuyorder_for_commodity_without_listing_rows(self) -> None:
        self.inventory_payload["list"][0]["marketHashName"] = "Revolution Case"
        self.inventory_payload["list"][0]["name"] = "变革武器箱"
        trade_id = self._create_locked_trade()
        client = FakeCommoditySteamBuyClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("buy-order-1", result["trade"]["steamListingId"])
        self.assertEqual(0, len(client.buy_calls))
        self.assertEqual(0, len(client.search_listing_calls))
        self.assertEqual(1, len(client.create_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("createbuyorder", note["steamBuyMethod"])
        self.assertIn("wallet_balance_delta", note["steamBuyVerifiedBy"])
        self.assertIn("no_active_matching_buy_order", note["steamBuyVerifiedBy"])

    def test_buy_step_accepts_createbuyorder_fill_below_maximum_and_records_actual_price(self) -> None:
        self.inventory_payload["list"][0]["marketHashName"] = "Revolution Case"
        self.inventory_payload["list"][0]["name"] = "Revolution Case"
        trade_id = self._create_locked_trade()
        client = FakeCheaperFilledBuyOrderClient(total=10000, actual_total=9440)
        c5_client = FakeC5InventoryClient([
            {
                "assetId": "asset-a",
                "marketHashName": "Revolution Case",
                "steamId": "steam-a",
                "ifTradable": True,
            },
            {
                "assetId": "asset-b-cheaper-fill",
                "marketHashName": "Revolution Case",
                "steamId": "steam-a",
                "ifTradable": False,
            },
        ])

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual(94.40, result["trade"]["steamBuyPrice"])
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(0, len(client.cancel_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual(94.40, note["walletDelta"])
        self.assertEqual(100.00, note["steamBuyMaximumPrice"])
        self.assertEqual(94.40, note["steamBuyActualPrice"])
        self.assertIn("wallet_balance_delta_within_buy_order_max", note["steamBuyVerifiedBy"])
        self.assertIn("c5_inventory_new_asset", note["steamBuyVerifiedBy"])
        self.assertIn("no_active_matching_buy_order", note["steamBuyVerifiedBy"])

    def test_buy_step_never_retries_lower_price_fill_while_item_evidence_is_delayed(self) -> None:
        self.inventory_payload["list"][0]["marketHashName"] = "Revolution Case"
        self.inventory_payload["list"][0]["name"] = "Revolution Case"
        trade_id = self._create_locked_trade()
        client = FakeCheaperFilledBuyOrderClient(total=10000, actual_total=9440)
        c5_client = FakeC5InventoryClient([
            {
                "assetId": "asset-a",
                "marketHashName": "Revolution Case",
                "steamId": "steam-a",
                "ifTradable": True,
            }
        ])

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
            c5_client=c5_client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("manual_required", result["trade"]["status"])
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(1, len(client.cancel_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("uncertain", note["unverifiedBuyOrderAttempts"][0]["resolution"])
        self.assertIn(
            "wallet_balance_delta_within_buy_order_max",
            note["steamBuyVerifiedBy"],
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertIsNotNone(reservation)
            self.assertIsNone(reservation["reserved_until"])
        finally:
            db.close()

    def test_buy_step_cancels_unverified_createbuyorder_before_stopping(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakePendingBuyOrderClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIsNone(result["trade"]["error"])
        self.assertEqual(0, len(client.buy_calls))
        self.assertEqual(3, len(client.create_buy_order_calls))
        self.assertEqual(3, len(client.cancel_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("profit_trade_buy_order_unverified_cancel", note["cancelSource"])
        self.assertEqual(3, len(note["unverifiedBuyOrderAttempts"]))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_buy_step_stops_retrying_when_cancel_response_does_not_remove_order(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeCancelResponseButOrderRemainsActiveClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("manual_required", result["trade"]["status"])
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(1, len(client.cancel_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("uncertain", note["unverifiedBuyOrderAttempts"][0]["resolution"])
        self.assertTrue(note["activeBuyOrdersAfterCancel"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertIsNotNone(reservation)
            self.assertIsNone(reservation["reserved_until"])
        finally:
            db.close()

    def test_buy_step_recovers_fill_that_happens_while_cancelling_order(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeBuyOrderFillsDuringCancelClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(1, len(client.cancel_buy_order_calls))
        note = result["trade"]["note"]
        self.assertIn("wallet_balance_delta", note["steamBuyVerifiedBy"])
        self.assertEqual("purchased", note["unverifiedBuyOrderAttempts"][0]["resolution"])
    def test_buy_step_accepts_inventory_new_asset_even_when_buy_order_still_listed(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeFilledButListingStaleBuyOrderClient(total=10000)
        c5_client = FakeC5InventoryClient([
            {
                "assetId": "asset-a",
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "steamId": "steam-a",
                "ifTradable": True,
            },
            {
                "assetId": "asset-b",
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "steamId": "steam-a",
                "ifTradable": False,
            },
        ])

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("buy-order-1", result["trade"]["steamListingId"])
        note = result["trade"]["note"]
        self.assertIn("wallet_balance_delta", note["steamBuyVerifiedBy"])
        self.assertIn("c5_inventory_new_asset", note["steamBuyVerifiedBy"])
        self.assertEqual(["asset-b"], note["newInventoryAssetIds"])
        self.assertEqual(1, len(note["activeBuyOrdersAfter"]))
        self.assertEqual(1, len(c5_client.inventory_calls))
    def test_buy_step_waits_for_createbuyorder_to_clear_before_manual_required(self) -> None:
        profit_trade_module.STEAM_BUY_VERIFY_ATTEMPTS = 3
        profit_trade_module.STEAM_BUY_VERIFY_DELAY_SECONDS = 0.0
        profit_trade_module._STEAM_BUY_FAILED_LISTING_BLACKLIST.clear()
        trade_id = self._create_locked_trade()
        client = FakeDelayedBuyOrderClearsClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("buy-order-1", result["trade"]["steamListingId"])
        note = result["trade"]["note"]
        self.assertIn("wallet_balance_delta", note["steamBuyVerifiedBy"])
        self.assertIn("no_active_matching_buy_order", note["steamBuyVerifiedBy"])
        self.assertEqual([], note["activeBuyOrdersBefore"])
        self.assertGreaterEqual(client.my_listings_calls, 3)

    def test_buy_step_records_old_buy_orders_but_does_not_treat_them_as_new_order_active(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeOldBuyOrderOnlyAfterCreateClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("new-buy-order", result["trade"]["steamListingId"])
        note = result["trade"]["note"]
        self.assertEqual("old-buy-order", note["activeBuyOrdersBefore"][0]["buyOrderId"])
        self.assertEqual([], note["activeBuyOrdersAfter"])
        self.assertIn("no_active_matching_buy_order", note["steamBuyVerifiedBy"])

    def test_run_once_lists_c5_in_same_cycle_after_createbuyorder_clears(self) -> None:
        profit_trade_module.STEAM_BUY_VERIFY_ATTEMPTS = 3
        profit_trade_module.STEAM_BUY_VERIFY_DELAY_SECONDS = 0.0
        profit_trade_module._STEAM_BUY_FAILED_LISTING_BLACKLIST.clear()
        steam_client = FakeDelayedBuyOrderClearsClient(total=10000)
        c5_client = FakeC5SaleClient()
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-a",
                    "marketHashName": "Sticker | Cluck",
                    "name": "啄鸡贴纸",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 88.0,
                    "token": "token-a",
                    "styleToken": "style-a",
                }
            ],
        }

        report = run_profit_trade_once(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_max_buy_per_cycle=1,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            inventory_payload=inventory_payload,
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=c5_client,
        )

        self.assertEqual([], report.errors)
        self.assertEqual(1, len(report.bought_trade_ids))
        self.assertEqual(report.bought_trade_ids, report.listed_trade_ids)
        self.assertEqual(0, len(steam_client.buy_calls))
        self.assertEqual(1, len(steam_client.create_buy_order_calls))
        self.assertEqual(1, len(c5_client.sale_calls), report)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(report.bought_trade_ids[0])
            self.assertEqual("c5_listed", trade["status"])
        finally:
            db.close()
    def test_buy_step_cancels_when_reservation_missing_before_steam_buy(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=10000)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.release_asset_reservation(
                asset_id="asset-a",
                owner="profit_trade",
                reason="test missing reservation",
            )
        finally:
            db.close()

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIsNone(result["trade"]["error"])
        self.assertEqual([], client.buy_calls)
        self.assertEqual([], client.order_book_calls)

    def test_buy_step_releases_lock_when_actual_roi_no_longer_meets_threshold(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=13000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIsNone(result["trade"]["error"])
        self.assertIn("ROI no longer meets threshold", result["trade"]["note"]["cancelReason"])
        self.assertEqual([], client.buy_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_manual_roi_floor_still_blocks_when_current_roi_remains_above_auto_minimum(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            db.update_profit_trade(
                trade_id,
                completed_at="2026-07-24T00:00:00+00:00",
                c5_sold_net_price=97.91,
                realized_profit=28.91,
                realized_roi=0.2891,
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "manualExecutionApproved": True,
                        "manualExecutionRoiFloor": 0.20,
                        "manualExecutionApprovalExpiresAt": (
                            datetime.now(timezone.utc) + timedelta(minutes=15)
                        ).isoformat(),
                    }
                ),
            )
        finally:
            db.close()

        client = FakeSteamBuyClient(total=10200)
        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIn("manually approved floor", result["trade"]["note"]["cancelReason"])
        self.assertEqual([], client.buy_calls)

    def test_final_purchase_price_cannot_cross_daily_steam_budget(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=10200)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_daily_steam_budget=101.0,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIn("daily Steam budget", result["trade"]["note"]["cancelReason"])
        self.assertEqual([], client.buy_calls)

    def test_direct_long_buy_gate_waits_for_claimed_daily_budget_guard(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=10000)
        gate_result = {
            "ok": True,
            "outcome": "not_present",
            "fillIds": [],
            "orderId": None,
            "buyOrderId": None,
        }

        with (
            patch.object(
                profit_trade_module,
                "_profit_trade_daily_steam_spent",
                return_value=0.0,
            ),
            patch.object(
                profit_trade_module,
                "_profit_trade_daily_steam_committed_through",
                return_value=102.0,
            ),
            patch.object(
                profit_trade_module,
                "_prepare_profit_trade_long_buy_for_direct_purchase",
                return_value=gate_result,
            ) as direct_gate,
        ):
            result = execute_profit_trade_buy(
                self.settings,
                trade_id,
                config=profit_config(
                    profit_trade_allow_real_execution=True,
                    profit_trade_daily_steam_budget=101.0,
                    profit_trade_steam_buy_price_tolerance_pct=50.0,
                ),
                steam_client=client,
            )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIn("daily Steam budget", result["trade"]["note"]["cancelReason"])
        direct_gate.assert_not_called()
        self.assertEqual([], client.buy_calls)

    def test_buy_step_switches_to_safe_buy_order_after_first_search_listings_429(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSearchListings429Client(failures=1)
        c5_client = FakeC5PreBuyRefreshClient()

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual(False, client.search_listing_calls[0].get("bounded_retry"))
        self.assertEqual(2, len(client.order_book_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(1, len(c5_client.price_batch_calls))
        self.assertEqual(1, len(c5_client.statistics_calls))
        note = result["trade"]["note"]
        self.assertEqual("createbuyorder", note["steamBuyMethod"])
        self.assertEqual(1, note["searchListings429Count"])
        self.assertIs(note["searchListings429Recovered"], True)
        self.assertIs(note["searchListings429FallbackToBuyOrder"], True)
        self.assertIs(note["purchaseRequestSent"], True)

    def test_buy_step_switches_to_safe_buy_order_after_search_listings_400(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSearchListings400Client()
        c5_client = FakeC5PreBuyRefreshClient()

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual(False, client.search_listing_calls[0].get("auth_retry"))
        self.assertEqual(2, len(client.order_book_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(1, len(c5_client.price_batch_calls))
        self.assertEqual(1, len(c5_client.statistics_calls))
        note = result["trade"]["note"]
        self.assertEqual("createbuyorder", note["steamBuyMethod"])
        self.assertEqual(1, note["searchListings400Count"])
        self.assertIs(note["searchListings400FallbackToBuyOrder"], True)
        self.assertIs(note["purchaseRequestSent"], True)
        self.assertEqual("closed", note["listingsCircuit"]["status"])

    def test_red_apple_five_percent_roi_uses_buy_order_after_one_listings_429(self) -> None:
        market_hash_name = "Glock-18 | Candy Apple (Factory New)"
        c5_sell_price = 75.0
        scan_c5_client = FakeC5SaleClient(price_batch_price=c5_sell_price)
        self.inventory_payload["list"][0].update(
            {
                "marketHashName": market_hash_name,
                "name": "格洛克18型 | 红苹果（崭新出厂）",
            }
        )
        config = profit_config(
            profit_trade_min_roi=0.03,
            profit_trade_allow_real_execution=True,
            profit_trade_steam_buy_price_tolerance_pct=50.0,
        )
        report = scan_profit_trade_opportunities(
            self.settings,
            config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(c5_sell_price=c5_sell_price),
            c5_client=scan_c5_client,
            record=True,
            lock_asset=True,
        )
        self.assertEqual(1, len(report.locked_trade_ids))
        self.assertAlmostEqual(0.050025, report.opportunities[0].expected_roi, places=6)

        client = FakeSearchListings429Client(failures=1, total=10000)
        result = execute_profit_trade_buy(
            self.settings,
            report.locked_trade_ids[0],
            config=config,
            steam_client=client,
            c5_client=FakeC5PreBuyRefreshClient(price=c5_sell_price),
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(
            market_hash_name,
            client.create_buy_order_calls[0]["market_hash_name"],
        )
        note = result["trade"]["note"]
        self.assertEqual("createbuyorder", note["steamBuyMethod"])
        self.assertEqual(1, note["searchListings429Count"])
        self.assertIs(note["searchListings429FallbackToBuyOrder"], True)
        self.assertIs(note["purchaseRequestSent"], True)

    def test_buy_step_opens_circuit_and_uses_safe_buy_order_after_first_search_listings_429(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSearchListings429Client(failures=99)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=client,
            c5_client=FakeC5PreBuyRefreshClient(),
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual(2, len(client.order_book_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual(1, len(client.create_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("createbuyorder", note["steamBuyMethod"])
        self.assertEqual(1, note["searchListings429Count"])
        self.assertIs(note["searchListings429FallbackToBuyOrder"], True)
        self.assertIs(note["purchaseRequestSent"], True)
        interruptions = build_profit_trade_interruptions_payload(self.settings)
        self.assertNotIn(trade_id, [item["id"] for item in interruptions["items"]])
        circuit = interruptions["listingsCircuit"]
        self.assertEqual("open", circuit["status"])
        self.assertTrue(circuit["isBlocking"])
        self.assertEqual(600, circuit["cooldownSeconds"])
        self.assertEqual(1, circuit["consecutive429Count"])
        self.assertEqual(trade_id, circuit["triggerTradeId"])
        self.assertEqual("account-a", circuit["triggerAccountId"])
        self.assertEqual("steam-a", circuit["triggerSteamId"])
        self.assertIn("listingsCircuit", note)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNotNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_open_listings_circuit_keeps_roi_watch_and_allows_safe_fallback_trade_lock(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.set_profit_trade_runtime_state(
                profit_trade_module.PROFIT_TRADE_LISTINGS_CIRCUIT_KEY,
                {
                    "status": "open",
                    "reason": "test HTTP 429 cooldown",
                    "first429At": now.isoformat(),
                    "last429At": now.isoformat(),
                    "cooldownUntil": (now + timedelta(minutes=10)).isoformat(),
                    "nextProbeAt": (now + timedelta(minutes=10)).isoformat(),
                    "consecutive429Count": 3,
                },
            )
        finally:
            db.close()

        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
            lock_asset=True,
        )

        self.assertEqual(1, report.opportunity_count)
        self.assertEqual(1, len(report.created_trade_ids))
        self.assertEqual(report.created_trade_ids, report.locked_trade_ids)
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual("open", watch["listingsCircuit"]["status"])
        self.assertEqual("listings_cooldown", watch["items"][0]["executionStatus"])
        self.assertIn("冷却结束", watch["items"][0]["executionReason"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNotNone(db.get_active_asset_reservation("asset-a"))
            self.assertEqual(1, len(db.list_profit_trades(limit=20)))
        finally:
            db.close()

    def test_open_listings_circuit_skips_search_and_executes_safe_buy_order(self) -> None:
        trade_id = self._create_locked_trade()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.set_profit_trade_runtime_state(
                profit_trade_module.PROFIT_TRADE_LISTINGS_CIRCUIT_KEY,
                {
                    "status": "open",
                    "reason": "test HTTP 429 cooldown",
                    "first429At": now.isoformat(),
                    "last429At": now.isoformat(),
                    "cooldownUntil": (now + timedelta(minutes=10)).isoformat(),
                    "nextProbeAt": (now + timedelta(minutes=10)).isoformat(),
                    "consecutive429Count": 1,
                },
            )
        finally:
            db.close()
        client = FakeSearchListings429Client(failures=99)
        c5_client = FakeC5PreBuyRefreshClient()

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=client,
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual([], client.search_listing_calls)
        self.assertEqual([], client.buy_calls)
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(1, len(client.order_book_calls))
        self.assertEqual(1, len(c5_client.price_batch_calls))
        self.assertEqual(1, len(c5_client.statistics_calls))
        note = result["trade"]["note"]
        self.assertEqual("createbuyorder", note["steamBuyMethod"])
        self.assertIs(note["listingsCircuitFallbackToBuyOrder"], True)
        self.assertEqual(0, note["searchListings429Count"])
        self.assertEqual(
            ["pre_buy"],
            [item["stage"] for item in note["executionOrderbookSnapshots"]],
        )

    def test_expired_listings_cooldown_closes_without_recovery_probe(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.set_profit_trade_runtime_state(
                profit_trade_module.PROFIT_TRADE_LISTINGS_CIRCUIT_KEY,
                {
                    "status": "open",
                    "reason": "test HTTP 429 cooldown",
                    "first429At": (now - timedelta(minutes=20)).isoformat(),
                    "last429At": (now - timedelta(minutes=10)).isoformat(),
                    "cooldownUntil": (now - timedelta(seconds=1)).isoformat(),
                    "nextProbeAt": (now - timedelta(seconds=1)).isoformat(),
                    "consecutive429Count": 6,
                },
            )
        finally:
            db.close()
        dashboard = build_profit_trade_dashboard_payload(self.settings)
        circuit = dashboard["listingsCircuit"]
        self.assertEqual("closed", circuit["status"])
        self.assertFalse(circuit["isBlocking"])
        self.assertFalse(circuit["probeAllowed"])
        self.assertIsNone(circuit["cooldownUntil"])
        self.assertIsNone(circuit["nextProbeAt"])

    def test_expired_cooldown_allows_next_real_trade_to_use_normal_listings(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.set_profit_trade_runtime_state(
                profit_trade_module.PROFIT_TRADE_LISTINGS_CIRCUIT_KEY,
                {
                    "status": "open",
                    "reason": "test HTTP 429 cooldown",
                    "first429At": (now - timedelta(minutes=10)).isoformat(),
                    "last429At": (now - timedelta(minutes=10)).isoformat(),
                    "cooldownUntil": (now - timedelta(seconds=1)).isoformat(),
                    "nextProbeAt": (now - timedelta(seconds=1)).isoformat(),
                    "consecutive429Count": 3,
                },
            )
        finally:
            db.close()
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient()

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
            c5_client=FakeC5PreBuyRefreshClient(),
        )

        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual(1, len(client.buy_calls))
        dashboard = build_profit_trade_dashboard_payload(self.settings)
        circuit = dashboard["listingsCircuit"]
        self.assertEqual("closed", circuit["status"])
        self.assertFalse(circuit["isBlocking"])
        self.assertFalse(circuit["probeAllowed"])
        self.assertIsNone(circuit["nextProbeAt"])

    def test_expired_cooldown_429_reopens_ten_minutes_and_uses_safe_buy_order(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.set_profit_trade_runtime_state(
                profit_trade_module.PROFIT_TRADE_LISTINGS_CIRCUIT_KEY,
                {
                    "status": "open",
                    "reason": "test long HTTP 429 cooldown",
                    "first429At": (now - timedelta(minutes=20)).isoformat(),
                    "last429At": (now - timedelta(minutes=10)).isoformat(),
                    "cooldownUntil": (now - timedelta(seconds=1)).isoformat(),
                    "nextProbeAt": (now - timedelta(seconds=1)).isoformat(),
                    "consecutive429Count": 6,
                },
            )
        finally:
            db.close()
        trade_id = self._create_locked_trade()
        client = FakeSearchListings429Client(failures=99)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=client,
            c5_client=FakeC5PreBuyRefreshClient(),
        )

        dashboard = build_profit_trade_dashboard_payload(self.settings)
        circuit = dashboard["listingsCircuit"]
        self.assertTrue(result["ok"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        self.assertEqual("open", circuit["status"])
        self.assertEqual(600, circuit["cooldownSeconds"])
        self.assertFalse(circuit["probeAllowed"])
        self.assertIsNone(circuit["nextProbeAt"])
        self.assertEqual(1, circuit["consecutive429Count"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual(1, len(client.create_buy_order_calls))
        self.assertEqual(2, len(client.order_book_calls))
        self.assertEqual(
            ["pre_buy", "after_listings_429"],
            [
                item["stage"]
                for item in result["trade"]["note"]["executionOrderbookSnapshots"]
            ],
        )

    def test_manual_lock_is_allowed_while_only_listings_route_is_cooling(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
            lock_asset=False,
        )
        trade_id = report.created_trade_ids[0]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.set_profit_trade_runtime_state(
                profit_trade_module.PROFIT_TRADE_LISTINGS_CIRCUIT_KEY,
                {
                    "status": "open",
                    "cooldownUntil": (now + timedelta(minutes=10)).isoformat(),
                    "nextProbeAt": (now + timedelta(minutes=10)).isoformat(),
                },
            )
        finally:
            db.close()

        result = lock_profit_trade(self.settings, trade_id)
        self.assertTrue(result["ok"])
        self.assertEqual("locked", result["trade"]["status"])

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNotNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_buy_step_cancels_when_price_rises_above_tolerance_after_429(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSearchListings429Client(
            failures=1,
            total=10000,
            total_after_first_429=10300,
        )

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=1.0,
            ),
            steam_client=client,
            c5_client=FakeC5PreBuyRefreshClient(),
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIn("Steam buy price moved too much", result["trade"]["note"]["cancelReason"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual([], client.create_buy_order_calls)

    def test_buy_step_cancels_when_roi_falls_below_minimum_after_429(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSearchListings429Client(
            failures=1,
            total=10000,
            total_after_first_429=12000,
        )

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=50.0,
            ),
            steam_client=client,
            c5_client=FakeC5PreBuyRefreshClient(),
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIn("ROI no longer meets threshold", result["trade"]["note"]["cancelReason"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual([], client.create_buy_order_calls)

    def test_buy_step_rechecks_c5_risk_after_search_listings_429(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            db.update_profit_trade(
                trade_id,
                note=profit_trade_module._build_note({**note, "liquidityStatus": "passed"}),
            )
        finally:
            db.close()
        client = FakeSearchListings429Client(failures=1)
        c5_client = FakeC5PreBuyRefreshClient(purchase_count=0)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_require_c5_market_depth=True,
                profit_trade_c5_min_on_sale_count=3,
                profit_trade_c5_min_purchase_count=1,
                profit_trade_c5_min_purchase_sell_ratio=0.7,
            ),
            steam_client=client,
            c5_client=c5_client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIn("C5 risk no longer passes", result["trade"]["note"]["cancelReason"])
        self.assertIn("purchase count", result["trade"]["note"]["cancelReason"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual([], client.create_buy_order_calls)

    def test_buy_step_rechecks_c5_risk_after_search_listings_400(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            db.update_profit_trade(
                trade_id,
                note=profit_trade_module._build_note({**note, "liquidityStatus": "passed"}),
            )
        finally:
            db.close()
        client = FakeSearchListings400Client()
        c5_client = FakeC5PreBuyRefreshClient(purchase_count=0)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_require_c5_market_depth=True,
                profit_trade_c5_min_on_sale_count=3,
                profit_trade_c5_min_purchase_count=1,
                profit_trade_c5_min_purchase_sell_ratio=0.7,
            ),
            steam_client=client,
            c5_client=c5_client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIn("C5 risk no longer passes", result["trade"]["note"]["cancelReason"])
        self.assertIn("HTTP 400", result["trade"]["note"]["cancelReason"])
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual([], client.create_buy_order_calls)

    def test_buy_step_releases_lock_when_orderbook_price_moves_too_much(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=10300)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_steam_buy_price_tolerance_pct=1.0,
            ),
            steam_client=client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertIsNone(result["trade"]["error"])
        self.assertIn("Steam buy price moved too much", result["trade"]["note"]["cancelReason"])
        self.assertEqual([], client.buy_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_buy_step_stops_before_market_lookup_when_runtime_is_disabled(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=10000)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=client,
            new_action_guard=lambda: False,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertEqual(
            "before_market_lookup",
            result["trade"]["note"]["runtimeDisabledStage"],
        )
        self.assertEqual([], client.order_book_calls)
        self.assertEqual([], client.search_listing_calls)
        self.assertEqual([], client.buy_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_buy_step_rechecks_runtime_immediately_before_purchase_request(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=10000)
        guard_results = iter((True, True, False))

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=client,
            new_action_guard=lambda: next(guard_results),
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertEqual(
            "before_purchase_request",
            result["trade"]["note"]["runtimeDisabledStage"],
        )
        self.assertEqual(1, len(client.order_book_calls))
        self.assertEqual(1, len(client.search_listing_calls))
        self.assertEqual([], client.buy_calls)
        self.assertEqual([], client.create_buy_order_calls)

    def test_buy_step_rechecks_runtime_after_scheduler_queue_before_http(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeSteamBuyClient(total=10000)
        guard_results = iter((True, True, True, False))
        http_sent: list[bool] = []

        def queued_buy_listing(**kwargs: object) -> dict[str, object]:
            execution_guard = kwargs.get("execution_guard")
            assert callable(execution_guard)
            if not execution_guard():
                raise SteamRequestGuardRejected("runtime disabled before HTTP callback")
            http_sent.append(True)
            return {"wallet_info": {"success": 1}}

        client.buy_listing = queued_buy_listing  # type: ignore[method-assign]

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=client,
            new_action_guard=lambda: next(guard_results),
        )

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertEqual(
            "scheduler_before_purchase_http",
            result["trade"]["note"]["runtimeDisabledStage"],
        )
        self.assertFalse(result["trade"]["note"]["purchaseRequestSent"])
        self.assertEqual([], http_sent)

    def test_normal_buy_step_does_not_repeat_c5_read_before_buying_b(self) -> None:
        trade_id = self._create_locked_trade()
        c5_client = FakeC5PreBuyRefreshClient(price=90.0)

        result = execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual([], c5_client.price_batch_calls)
        self.assertEqual([], c5_client.statistics_calls)

    def test_scan_does_not_create_or_lock_trade_after_runtime_is_disabled(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
            lock_asset=True,
            new_action_guard=lambda: False,
        )

        self.assertEqual(1, report.opportunity_count)
        self.assertEqual([], report.created_trade_ids)
        self.assertEqual([], report.locked_trade_ids)
        self.assertTrue(any("runtime was disabled" in note for note in report.notes))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertEqual([], db.list_profit_trades(limit=20))
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_executable_scan_does_not_query_own_listings_or_repeat_price_batch(self) -> None:
        c5_client = FakeC5SaleClient(
            active_product_ids=[],
            require_steam_id_for_sale_search=True,
        )
        inventory_payload = {
            **self.inventory_payload,
            "accounts": [{"steamId": "steam-a"}, {"steamId": "steam-b"}],
        }

        report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=inventory_payload,
            market_service=FakeProfitMarketService(),
            c5_client=c5_client,
            record=True,
            lock_asset=True,
        )

        self.assertEqual(1, report.opportunity_count)
        self.assertEqual(1, len(report.locked_trade_ids))
        self.assertEqual([], c5_client.sale_search_calls)
        self.assertEqual([], c5_client.price_batch_calls)

    def test_list_c5_step_consumes_a_asset_reservation_after_success(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        c5_client = FakeC5SaleClient(require_steam_id_for_sale_search=True)

        result = execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("c5_listed", result["trade"]["status"])
        self.assertEqual("c5-product-1", result["trade"]["c5ProductId"])
        self.assertEqual(1, len(c5_client.sale_calls))
        item = c5_client.sale_calls[0]["items"][0]
        self.assertEqual("asset-a", item["assetId"])
        self.assertEqual("token-a", item["token"])
        self.assertEqual("style-a", item["styleToken"])
        self.assertAlmostEqual(89.7, item["price"])
        self.assertEqual([], c5_client.sale_search_calls)
        self.assertEqual(1, len(c5_client.price_batch_calls))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertIsNotNone(reservation)
            self.assertEqual("consumed", reservation["status"])
        finally:
            db.close()

    def test_run_once_with_real_execution_disabled_records_candidates_only(self) -> None:
        report = run_profit_trade_once(
            self.settings,
            profit_config(profit_trade_allow_real_execution=False),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
        )

        self.assertTrue(report.enabled)
        self.assertFalse(report.allow_real_execution)
        self.assertIsNotNone(report.scanned)
        self.assertEqual(1, len(report.scanned.created_trade_ids))
        self.assertEqual([], report.bought_trade_ids)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(report.scanned.created_trade_ids[0])
            self.assertEqual("candidate", trade["status"])
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_run_once_with_real_execution_disabled_does_not_reprice_listed_trade(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        steam_client = FakeSteamBuyClient(total=10000)
        c5_client = self._c5_depth_client()

        report = run_profit_trade_once(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=False,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            inventory_payload={"source": "fixture", "list": []},
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=c5_client,
        )

        self.assertEqual([], report.bought_trade_ids)
        self.assertEqual([], report.listed_trade_ids)
        self.assertEqual([], steam_client.buy_calls)
        self.assertEqual([], c5_client.modify_calls)
        self.assertTrue(any("recorded candidates only" in error for error in report.errors))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertAlmostEqual(100.0, trade["c5_listing_price"])
        finally:
            db.close()

    def test_run_once_with_real_execution_buys_and_lists_one_trade(self) -> None:
        steam_client = FakeSteamBuyClient(total=10000)
        c5_client = FakeC5SaleClient()

        report = run_profit_trade_once(
            self.settings,
            profit_config(profit_trade_allow_real_execution=True, profit_trade_max_buy_per_cycle=1),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=c5_client,
        )

        self.assertEqual([], report.errors)
        self.assertEqual(1, len(report.bought_trade_ids))
        self.assertEqual(report.bought_trade_ids, report.listed_trade_ids)
        self.assertEqual(1, len(steam_client.buy_calls))
        self.assertTrue(steam_client.order_book_calls)
        self.assertTrue(
            all(call.get("execution_priority") is True for call in steam_client.order_book_calls)
        )
        self.assertEqual(1, len(c5_client.sale_calls), report)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(report.bought_trade_ids[0])
            self.assertEqual("c5_listed", trade["status"])
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertIsNotNone(reservation)
            self.assertEqual("consumed", reservation["status"])
        finally:
            db.close()

    def test_run_once_cancels_recorded_candidate_and_uses_fresh_opportunity(self) -> None:
        stale_report = scan_profit_trade_opportunities(
            self.settings,
            self.config,
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            record=True,
            lock_asset=False,
        )
        stale_trade_id = stale_report.created_trade_ids[0]
        steam_client = FakeSteamBuyClient(total=10000)
        c5_client = FakeC5SaleClient()

        report = run_profit_trade_once(
            self.settings,
            profit_config(profit_trade_allow_real_execution=True, profit_trade_max_buy_per_cycle=1),
            inventory_payload=self.inventory_payload,
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=c5_client,
        )

        self.assertEqual([], report.errors)
        self.assertEqual(1, len(report.bought_trade_ids))
        self.assertEqual(report.bought_trade_ids, report.listed_trade_ids)
        self.assertNotIn(stale_trade_id, report.bought_trade_ids)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            stale = db.get_profit_trade(stale_trade_id)
            fresh = db.get_profit_trade(report.bought_trade_ids[0])
            self.assertEqual("cancelled", stale["status"])
            self.assertEqual("c5_listed", fresh["status"])
        finally:
            db.close()

    def test_run_once_cancels_expired_locked_trade_before_buy_step(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.release_asset_reservation(
                asset_id="asset-a",
                owner="profit_trade",
                reason="test expired before scheduled run",
            )
        finally:
            db.close()

        steam_client = FakeSteamBuyClient(total=10000)
        report = run_profit_trade_once(
            self.settings,
            profit_config(profit_trade_allow_real_execution=True, profit_trade_max_buy_per_cycle=1),
            inventory_payload={"source": "fixture", "list": []},
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=FakeC5SaleClient(),
        )

        self.assertEqual([], report.bought_trade_ids)
        self.assertEqual([], steam_client.buy_calls)
        self.assertFalse(any("A asset reservation is not active" in error for error in report.errors))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("cancelled", trade["status"])
            self.assertIsNone(trade["error"])
        finally:
            db.close()

    def test_run_once_cancels_locked_trade_when_pre_buy_orderbook_fails(self) -> None:
        trade_id = self._create_locked_trade()
        steam_client = FakeOrderbookFailClient(total=10000)

        report = run_profit_trade_once(
            self.settings,
            profit_config(profit_trade_allow_real_execution=True, profit_trade_max_buy_per_cycle=1),
            inventory_payload={"source": "fixture", "list": []},
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=FakeC5SaleClient(),
        )

        self.assertEqual([], report.bought_trade_ids)
        self.assertEqual([], steam_client.buy_calls)
        self.assertTrue(any("orderbook unavailable" in error for error in report.errors))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("cancelled", trade["status"])
            self.assertIsNone(trade["error"])
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_market_service_build_reuses_saved_cookies_without_proactive_validation(self) -> None:
        account = Account(
            id="saved-cookie-account",
            name="saved-cookie-account",
            username="username",
            password="password",
            steam_id64="steam-a",
            cookies="sessionid=x; steamLoginSecure=y",
        )
        original_store = profit_trade_module.AccountStore
        original_client = profit_trade_module.SteamMarketClient

        class SavedCookieStore:
            def __init__(self, _path: Path) -> None:
                pass

            def list_accounts(self) -> list[Account]:
                return [account]

        class SavedCookieClient:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = dict(kwargs)

        try:
            profit_trade_module.AccountStore = SavedCookieStore
            profit_trade_module.SteamMarketClient = SavedCookieClient
            service = profit_trade_module._build_profit_trade_market_service(
                Settings(
                    db_path=Path(self.temp_dir.name) / "cookie-reuse.db",
                    c5_api_key=None,
                )
            )
        finally:
            profit_trade_module.AccountStore = original_store
            profit_trade_module.SteamMarketClient = original_client

        self.assertEqual(1, len(service.steam_market_clients))
        self.assertEqual(
            "saved-cookie-account",
            service.steam_market_clients[0].kwargs["account_id"],
        )

    def test_run_once_retries_pre_buy_queue_timeout_once_and_continues_purchase(self) -> None:
        trade_id = self._create_locked_trade()
        original_buy = profit_trade_module.execute_profit_trade_buy
        original_list = profit_trade_module.execute_profit_trade_list_c5
        attempts: list[int] = []

        def flaky_buy(*_args: object, **_kwargs: object) -> dict[str, object]:
            attempts.append(trade_id)
            if len(attempts) == 1:
                raise SteamRequestTimeout("Steam request test timed out in queue")
            return {"ok": True, "trade": {"steamBuyPrice": 100.0}}

        try:
            profit_trade_module.execute_profit_trade_buy = flaky_buy
            profit_trade_module.execute_profit_trade_list_c5 = (
                lambda *_args, **_kwargs: {"ok": True}
            )
            report = run_profit_trade_once(
                self.settings,
                profit_config(
                    profit_trade_allow_real_execution=True,
                    profit_trade_max_buy_per_cycle=1,
                ),
                inventory_payload={"source": "fixture", "list": []},
                market_service=FakeProfitMarketService(),
                c5_client=FakeC5SaleClient(),
            )
        finally:
            profit_trade_module.execute_profit_trade_buy = original_buy
            profit_trade_module.execute_profit_trade_list_c5 = original_list

        self.assertEqual([trade_id, trade_id], attempts)
        self.assertEqual([trade_id], report.bought_trade_ids)
        self.assertEqual([trade_id], report.listed_trade_ids)

    def test_run_once_keeps_lock_after_two_pre_buy_queue_timeouts(self) -> None:
        trade_id = self._create_locked_trade()
        original_buy = profit_trade_module.execute_profit_trade_buy
        attempts: list[int] = []

        def timed_out_buy(*_args: object, **_kwargs: object) -> dict[str, object]:
            attempts.append(trade_id)
            raise SteamRequestTimeout("Steam request test timed out in queue")

        try:
            profit_trade_module.execute_profit_trade_buy = timed_out_buy
            report = run_profit_trade_once(
                self.settings,
                profit_config(
                    profit_trade_allow_real_execution=True,
                    profit_trade_max_buy_per_cycle=1,
                    profit_trade_reservation_seconds=60,
                ),
                inventory_payload={"source": "fixture", "list": []},
                market_service=FakeProfitMarketService(),
                c5_client=FakeC5SaleClient(),
            )
        finally:
            profit_trade_module.execute_profit_trade_buy = original_buy

        self.assertEqual([trade_id, trade_id], attempts)
        self.assertEqual([], report.bought_trade_ids)
        self.assertTrue(any("timed out in queue" in error for error in report.errors))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            reservation = db.get_active_asset_reservation("asset-a")
            note = profit_trade_module._read_note(trade["note"])
        finally:
            db.close()
        self.assertEqual("locked", trade["status"])
        self.assertIsNotNone(reservation)
        self.assertGreater(
            datetime.fromisoformat(str(reservation["reserved_until"])),
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        self.assertEqual(2, note["preBuyQueueTimeoutCount"])
        self.assertEqual("retry_next_profit_trade_cycle", note["preBuyQueueRetryPolicy"])

    def test_list_c5_after_steam_buy_allows_positive_roi_below_open_floor(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        c5_client = FakeC5SaleClient(price_batch_price=70.0)

        result = execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_min_roi=0.07,
            ),
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("c5_listed", result["trade"]["status"])
        self.assertEqual(1, len(c5_client.sale_calls))
        self.assertGreater(result["trade"]["expectedRoi"], 0.0)
        self.assertLess(result["trade"]["expectedRoi"], 0.07)
        note = result["trade"]["note"]
        self.assertEqual(0.0, note["preListRoiFloor"])
        self.assertEqual("post_steam_buy_non_negative", note["preListRoiFloorSource"])

    def test_list_c5_after_steam_buy_blocks_negative_roi_with_precise_message(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        c5_client = FakeC5SaleClient(price_batch_price=68.0)

        result = execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=c5_client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("manual_required", result["trade"]["status"])
        self.assertEqual("steam_bought", result["trade"]["stepKey"])
        self.assertEqual([], c5_client.sale_calls)
        self.assertIn("post-Steam-buy minimum 0.0000%", result["trade"]["error"])
        self.assertRegex(result["trade"]["error"], r"pre-list ROI -\d+\.\d{4}%")
        note = result["trade"]["note"]
        self.assertEqual(0.0, note["preListRoiFloor"])
        self.assertEqual("post_steam_buy_non_negative", note["preListRoiFloorSource"])

    def test_list_c5_refreshes_rising_market_and_persists_request_price_before_http(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        c5_client = FakeC5SaleClient(price_batch_price=100.0)
        observed_before_http: dict[str, object] = {}
        original_sale_create = c5_client.sale_create

        def inspect_then_create(**kwargs: object) -> dict:
            db = Database(self.settings.db_path)
            try:
                db.initialize()
                pending = db.get_profit_trade(trade_id)
                observed_before_http["status"] = pending["status"]
                observed_before_http["price"] = pending["c5_listing_price"]
                observed_before_http["note"] = profit_trade_module._read_note(pending["note"])
            finally:
                db.close()
            return original_sale_create(**kwargs)

        c5_client.sale_create = inspect_then_create  # type: ignore[method-assign]
        result = execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=c5_client,
        )

        self.assertTrue(result["ok"])
        requested_price = c5_client.sale_calls[0]["items"][0]["price"]
        self.assertAlmostEqual(99.67, requested_price)
        self.assertGreater(requested_price, 89.7)
        self.assertEqual("listing_c5", observed_before_http["status"])
        self.assertAlmostEqual(99.67, observed_before_http["price"])
        pending_note = observed_before_http["note"]
        self.assertAlmostEqual(99.67, pending_note["c5ListingRequestedPrice"])
        self.assertAlmostEqual(99.67, result["trade"]["c5ListingPrice"])
        self.assertIn("c5FirstListedAt", result["trade"]["note"])

    def test_list_c5_sale_create_failure_preserves_pre_persisted_request_evidence(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        c5_client = FakeC5SaleClient()
        c5_client.sale_error = RuntimeError("sale_create transport timeout")

        result = execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=c5_client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(1, len(c5_client.sale_calls))
        self.assertAlmostEqual(89.7, c5_client.sale_calls[0]["items"][0]["price"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("manual_required", trade["status"])
            self.assertAlmostEqual(89.7, trade["c5_listing_price"])
            self.assertIsNone(trade["c5_product_id"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertAlmostEqual(89.7, note["c5ListingRequestedPrice"])
            self.assertIn("c5ListingRequestedAt", note)
            self.assertIn("c5ListingFailedAt", note)
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertIsNotNone(reservation)
            self.assertEqual("active", reservation["status"])
        finally:
            db.close()

    def test_list_c5_sale_create_failed_list_keeps_request_evidence_and_requires_manual(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        c5_client = FakeC5SaleClient()
        c5_client.sale_payload = {
            "successList": [],
            "failedList": [{"assetId": "asset-a", "message": "price rejected"}],
        }

        result = execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=c5_client,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("manual_required", result["trade"]["status"])
        self.assertIn("price rejected", result["trade"]["error"])
        self.assertAlmostEqual(89.7, result["trade"]["c5ListingPrice"])
        self.assertAlmostEqual(89.7, result["trade"]["note"]["c5ListingRequestedPrice"])
        self.assertIn("c5ListingFailedAt", result["trade"]["note"])

    def test_run_once_does_not_take_locked_trade_owned_by_manual_execution_batch(self) -> None:
        """Automatic cycles must not steal work from a persistent manual batch."""

        trade_id = self._create_locked_trade()
        request_id = "PTMAN-owned-locked-trade"
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(trade["note"])
            db.update_profit_trade(
                trade_id,
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "manualExecutionApproved": True,
                        "manualExecutionRequestId": request_id,
                        "manualExecutionRoiFloor": 0.08,
                        "manualExecutionApprovalExpiresAt": (
                            datetime.now(timezone.utc) + timedelta(minutes=15)
                        ).isoformat(),
                    }
                ),
            )
        finally:
            db.close()

        steam_client = FakeSteamBuyClient(total=10000)
        report = run_profit_trade_once(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_max_buy_per_cycle=1,
            ),
            inventory_payload={"source": "fixture", "list": []},
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=FakeC5SaleClient(),
        )

        self.assertEqual([], report.bought_trade_ids)
        self.assertEqual([], steam_client.buy_calls)
        self.assertEqual([], steam_client.create_buy_order_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            reservation = db.get_active_asset_reservation("asset-a")
        finally:
            db.close()
        self.assertEqual("locked", trade["status"])
        self.assertIsNotNone(reservation)
        self.assertEqual(request_id, profit_trade_module._read_note(trade["note"])["manualExecutionRequestId"])

    def test_run_once_waits_for_complete_scan_before_buying_fast_opportunity(self) -> None:
        slow_release = threading.Event()
        scan_steam_client = StreamingProfitSteamClient(slow_release)
        buy_client = SignalingSteamBuyClient(total=10000)
        c5_client = FakeC5PreBuyRefreshClient(price=90.0)
        market_service = MarketService(
            steam_market_client=scan_steam_client,
            c5_client=c5_client,
            include_c5_purchase_prices=False,
            fallback_max_workers=2,
        )
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-fast",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "name": "AK-47 红线（略有磨损）",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 100.0,
                    "token": "token-fast",
                    "styleToken": "style-fast",
                },
                {
                    "assetId": "asset-slow",
                    "marketHashName": "Slow Item",
                    "name": "慢速物品",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 90.0,
                    "token": "token-slow",
                    "styleToken": "style-slow",
                },
            ],
        }
        result_holder: dict[str, object] = {}

        def run_once() -> None:
            try:
                result_holder["report"] = run_profit_trade_once(
                    self.settings,
                    profit_config(
                        profit_trade_allow_real_execution=True,
                        profit_trade_max_buy_per_cycle=1,
                    ),
                    inventory_payload=inventory_payload,
                    market_service=market_service,
                    steam_client=buy_client,
                    c5_client=c5_client,
                )
            except BaseException as exc:
                result_holder["error"] = exc

        run_thread = threading.Thread(target=run_once, daemon=True)
        run_thread.start()
        try:
            self.assertTrue(scan_steam_client.slow_started.wait(timeout=2))
            self.assertFalse(
                buy_client.buy_started.wait(timeout=0.5),
                "direct purchase started before the complete scan finished",
            )
            self.assertTrue(run_thread.is_alive(), "full scan ended before the slow orderbook was released")
        finally:
            slow_release.set()
            run_thread.join(timeout=5)

        self.assertFalse(run_thread.is_alive())
        self.assertNotIn("error", result_holder)
        self.assertTrue(buy_client.buy_started.is_set())
        report = result_holder["report"]
        self.assertEqual(1, len(report.bought_trade_ids))
        self.assertEqual(report.bought_trade_ids, report.listed_trade_ids)

    def test_run_once_does_not_start_purchase_until_scan_returns(self) -> None:
        release_buy = threading.Event()
        market_service = SequencedStreamingProfitMarketService()
        buy_client = BlockingSteamBuyClient(release_buy, total=10000)
        c5_client = FakeC5PreBuyRefreshClient(price=90.0)
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-fast",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "name": "Fast",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 100.0,
                    "token": "token-fast",
                    "styleToken": "style-fast",
                },
                {
                    "assetId": "asset-slow",
                    "marketHashName": "Slow Item",
                    "name": "Slow",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 90.0,
                    "token": "token-slow",
                    "styleToken": "style-slow",
                },
            ],
        }
        result_holder: dict[str, object] = {}

        def run_once() -> None:
            try:
                result_holder["report"] = run_profit_trade_once(
                    self.settings,
                    profit_config(
                        profit_trade_allow_real_execution=True,
                        profit_trade_max_buy_per_cycle=1,
                    ),
                    inventory_payload=inventory_payload,
                    market_service=market_service,
                    steam_client=buy_client,
                    c5_client=c5_client,
                )
            except BaseException as exc:
                result_holder["error"] = exc

        run_thread = threading.Thread(target=run_once, daemon=True)
        run_thread.start()
        try:
            self.assertTrue(
                market_service.all_states_published.wait(timeout=1),
                "the complete scan did not reach its return barrier",
            )
            self.assertFalse(
                buy_client.buy_started.is_set(),
                "purchase began while the complete scan was still blocked",
            )
            market_service.release_full_scan.set()
            self.assertTrue(buy_client.buy_started.wait(timeout=2), result_holder.get("error"))
        finally:
            market_service.release_full_scan.set()
            release_buy.set()
            run_thread.join(timeout=5)

        self.assertFalse(run_thread.is_alive())
        self.assertNotIn("error", result_holder)
        report = result_holder["report"]
        self.assertEqual(1, len(report.bought_trade_ids))

    def test_run_once_persists_not_sent_evidence_when_search_listings_fails(self) -> None:
        trade_id = self._create_locked_trade()
        steam_client = FakeSearchListingsFailClient(total=10000)

        report = run_profit_trade_once(
            self.settings,
            profit_config(profit_trade_allow_real_execution=True, profit_trade_max_buy_per_cycle=1),
            inventory_payload={"source": "fixture", "list": []},
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=FakeC5SaleClient(),
        )

        self.assertEqual([], report.bought_trade_ids)
        self.assertEqual([], steam_client.buy_calls)
        self.assertEqual([], steam_client.create_buy_order_calls)
        self.assertTrue(any("listings search failed" in error.lower() for error in report.errors))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(trade["note"])
        finally:
            db.close()
        self.assertEqual("cancelled", trade["status"])
        self.assertEqual("profit_trade_pre_buy_cancel", note["cancelSource"])
        self.assertIs(note["purchaseRequestSent"], False)
        self.assertIs(note["listingIdObtained"], False)
        self.assertEqual("search_listings_failed_before_steam_buy", note["purchaseRequestEvidence"])

    def test_run_once_respects_daily_steam_budget_before_buying_locked_trade(self) -> None:
        trade_id = self._create_locked_trade()
        steam_client = FakeSteamBuyClient(total=10000)

        report = run_profit_trade_once(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_max_buy_per_cycle=1,
                profit_trade_daily_steam_budget=50.0,
            ),
            inventory_payload={"source": "fixture", "list": []},
            market_service=FakeProfitMarketService(),
            steam_client=steam_client,
            c5_client=FakeC5SaleClient(),
        )

        self.assertEqual([], report.bought_trade_ids)
        self.assertEqual([], steam_client.buy_calls)
        self.assertIn(trade_id, report.skipped_trade_ids)
        self.assertTrue(any("daily Steam budget would be exceeded" in error for error in report.errors))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("locked", trade["status"])
        finally:
            db.close()

    def test_refresh_sales_does_not_settle_missing_listing_without_seller_order(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=FakeC5SaleClient(active_product_ids=["c5-product-1"]),
        )

        result = refresh_profit_trade_sales(
            self.settings,
            profit_config(
                profit_trade_sale_sync_initial_grace_seconds=0,
                profit_trade_c5_current_sale_net_factor=0.99,
            ),
            c5_client=FakeC5SaleClient(active_product_ids=[]),
        )

        self.assertEqual([], result["settledTradeIds"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertIn("productId/assetId", trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("c5-product-1", note["missingSellerOrderProductId"])
            self.assertEqual(1, note["c5SaleSyncPendingProbeCount"])
        finally:
            db.close()

    def test_refresh_sales_does_not_settle_when_seller_order_lookup_is_unavailable(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=FakeC5SaleClient(active_product_ids=["c5-product-1"]),
        )
        c5_client = FakeC5SaleClient(active_product_ids=[])
        c5_client.seller_order_error = RuntimeError("seller order endpoint unavailable")

        result = refresh_profit_trade_sales(
            self.settings,
            profit_config(
                profit_trade_sale_sync_initial_grace_seconds=0,
                profit_trade_c5_current_sale_net_factor=0.99,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["settledTradeIds"])
        self.assertIn(trade_id, result["skippedTradeIds"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertEqual("c5_listed", trade["step_key"])
            self.assertIsNone(trade["completed_at"])
            self.assertIsNone(trade["c5_sold_net_price"])
            self.assertIsNone(trade["realized_profit"])
            self.assertIsNone(trade["realized_roi"])
            self.assertIn("seller sold-order lookup did not cover", trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("c5-product-1", note["activeSaleMissingProductId"])
            self.assertEqual("asset-a", note["activeSaleMissingAssetId"])
            self.assertIn("settlement evidence unavailable", note["settlementBlockedReason"])
        finally:
            db.close()

    def test_refresh_sales_escalates_missing_listing_to_manual_after_sync_window(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=FakeC5SaleClient(active_product_ids=["c5-product-1"]),
        )
        first_missing_at = datetime.now(timezone.utc) - timedelta(hours=3, minutes=1)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(trade["note"])
            db.update_profit_trade(
                trade_id,
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "c5SaleSyncPendingFirstAt": first_missing_at.isoformat(),
                        "c5SaleSyncPendingProbeCount": 2,
                    }
                ),
            )
        finally:
            db.close()

        result = refresh_profit_trade_sales(
            self.settings,
            profit_config(
                profit_trade_sale_sync_initial_grace_seconds=0,
                profit_trade_c5_current_sale_net_factor=0.99,
            ),
            c5_client=FakeC5SaleClient(active_product_ids=[]),
        )

        self.assertEqual([], result["settledTradeIds"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("manual_required", trade["status"])
            self.assertIn("more than 3 hours", trade["error"])
            self.assertIsNone(trade["completed_at"])
            self.assertIsNone(trade["c5_sold_net_price"])
            self.assertIsNone(trade["realized_profit"])
            self.assertIsNone(trade["realized_roi"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual(3, note["c5SaleSyncPendingProbeCount"])
        finally:
            db.close()

    def test_refresh_sales_rechecks_manual_required_c5_listed_trade(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=FakeC5SaleClient(active_product_ids=["c5-product-1"]),
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(trade["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                error="legacy manual state before sale sync retry",
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "settlementBlockedAt": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            )
        finally:
            db.close()

        c5_client = FakeC5SaleClient(active_product_ids=[])
        c5_client.seller_orders = {
            "steam-a": [
                {
                    "orderId": "seller-order-1",
                    "productId": "c5-product-1",
                    "price": 97.91,
                    "status": 10,
                    "statusName": "success",
                }
            ]
        }
        c5_client.seller_order_details = {
            "seller-order-1": {"orderId": "seller-order-1", "getMoney": 97.91, "actualPay": 98.9}
        }

        second_result = refresh_profit_trade_sales(
            self.settings,
            profit_config(
                profit_trade_sale_sync_initial_grace_seconds=0,
                profit_trade_c5_current_sale_net_factor=0.99,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([trade_id], second_result["settledTradeIds"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("completed", trade["status"])
            self.assertAlmostEqual(97.91, trade["c5_sold_net_price"])
            self.assertIsNone(trade["error"])
        finally:
            db.close()

    def test_refresh_sales_uses_c5_seller_order_before_active_listing_fallback(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=FakeC5SaleClient(active_product_ids=["c5-product-1"]),
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.update_profit_trade(trade_id, error="old reprice error")
        finally:
            db.close()
        c5_client = FakeC5SaleClient(active_product_ids=["c5-product-1"])
        c5_client.seller_orders = {
            "steam-a": [
                {
                    "orderId": "seller-order-1",
                    "productId": "c5-product-1",
                    "price": 88.0,
                    "status": 10,
                    "statusName": "出售成功",
                }
            ]
        }
        c5_client.seller_order_details = {
            "seller-order-1": {"orderId": "seller-order-1", "getMoney": 88.8, "actualPay": 89.7}
        }

        result = refresh_profit_trade_sales(
            self.settings,
            profit_config(
                profit_trade_sale_sync_initial_grace_seconds=0,
                profit_trade_c5_current_sale_net_factor=0.99,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([trade_id], result["settledTradeIds"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("completed", trade["status"])
            self.assertAlmostEqual(88.8, trade["c5_sold_net_price"])
            self.assertAlmostEqual(19.8, trade["realized_profit"])
            self.assertAlmostEqual(88.8 / 100.0 - 0.69, trade["realized_roi"])
            self.assertIsNone(trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("seller-order-1", note["c5SellerOrderId"])
            self.assertEqual("seller_order_detail_get_money", note["c5SoldNetPriceSource"])
        finally:
            db.close()

    def test_refresh_sales_matches_seller_order_by_asset_id_when_product_id_differs(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=10000),
        )
        execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=FakeC5SaleClient(active_product_ids=["c5-product-1"]),
        )
        c5_client = FakeC5SaleClient(active_product_ids=[])
        c5_client.seller_orders = {
            "steam-a": [
                {
                    "orderId": "seller-order-asset",
                    "productId": "different-product-id",
                    "price": 97.91,
                    "status": 10,
                    "statusName": "success",
                    "marketHashName": "AK-47 | Redline (Field-Tested)",
                    "assetInfo": {
                        "assetId": "asset-a",
                        "originalAssetId": "asset-a",
                    },
                }
            ]
        }
        c5_client.seller_order_details = {
            "seller-order-asset": {"orderId": "seller-order-asset", "getMoney": 97.91}
        }

        result = refresh_profit_trade_sales(
            self.settings,
            profit_config(
                profit_trade_sale_sync_initial_grace_seconds=0,
                profit_trade_c5_current_sale_net_factor=0.99,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([trade_id], result["settledTradeIds"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("completed", trade["status"])
            self.assertAlmostEqual(97.91, trade["c5_sold_net_price"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("seller-order-asset", note["c5SellerOrderId"])
            self.assertEqual("different-product-id", note["c5SellerOrderProductId"])
        finally:
            db.close()

    def test_refresh_sales_uses_seller_order_list_price_when_detail_net_is_outlier(self) -> None:
        trade_id = self._create_locked_trade()
        execute_profit_trade_buy(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            steam_client=FakeSteamBuyClient(total=2136),
        )
        execute_profit_trade_list_c5(
            self.settings,
            trade_id,
            config=profit_config(profit_trade_allow_real_execution=True),
            c5_client=FakeC5SaleClient(active_product_ids=["c5-product-1"]),
        )
        c5_client = FakeC5SaleClient(active_product_ids=[])
        c5_client.seller_orders = {
            "steam-a": [
                {
                    "orderId": "seller-order-1",
                    "productId": "c5-product-1",
                    "price": 15.9,
                    "status": 10,
                    "statusName": "success",
                }
            ]
        }
        c5_client.seller_order_details = {
            "seller-order-1": {"orderId": "seller-order-1", "getMoney": 2.42, "actualPay": 2.58}
        }

        result = refresh_profit_trade_sales(
            self.settings,
            profit_config(
                profit_trade_sale_sync_initial_grace_seconds=0,
                profit_trade_c5_current_sale_net_factor=0.99,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([trade_id], result["settledTradeIds"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("completed", trade["status"])
            self.assertAlmostEqual(15.9, trade["c5_sold_net_price"])
            self.assertAlmostEqual(15.9 - 14.7384, trade["realized_profit"])
            self.assertAlmostEqual(15.9 / 21.36 - 0.69, trade["realized_roi"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("seller_order_list_price_detail_net_outlier", note["c5SoldNetPriceSource"])
        finally:
            db.close()

    def test_run_once_settles_c5_sales_even_when_real_execution_is_disabled(self) -> None:
        trade_id = self._create_c5_listed_trade()
        c5_client = FakeC5SaleClient(active_product_ids=["123"])
        c5_client.seller_orders = {
            "steam-a": [
                {
                    "orderId": "seller-order-1",
                    "productId": "123",
                    "price": 88.0,
                    "status": 10,
                    "statusName": "success",
                }
            ]
        }
        c5_client.seller_order_details = {
            "seller-order-1": {"orderId": "seller-order-1", "getMoney": 88.8}
        }

        report = run_profit_trade_once(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=False,
                profit_trade_sale_sync_initial_grace_seconds=0,
            ),
            inventory_payload={"source": "fixture", "list": []},
            market_service=FakeProfitMarketService(),
            c5_client=c5_client,
        )

        self.assertEqual([trade_id], report.settled_trade_ids)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("completed", trade["status"])
            self.assertAlmostEqual(88.8, trade["c5_sold_net_price"])
            self.assertAlmostEqual(88.8 / 100.0 - 0.69, trade["realized_roi"])
        finally:
            db.close()

    def _create_c5_listed_trade(
        self,
        *,
        listed_hours_ago: float = 4.0,
        current_price: float = 100.0,
        min_roi_at_open: float = 0.07,
        note_overrides: dict | None = None,
        trade_no: str = "PT-test-listed",
        asset_id: str = "asset-a",
        product_id: str = "123",
    ) -> int:
        listed_at = datetime.now(timezone.utc) - timedelta(hours=listed_hours_ago)
        expected_net = current_price * 0.99
        note = {
            "c5FirstListedAt": listed_at.isoformat(),
            "c5ListedAt": listed_at.isoformat(),
            "minRoiAtOpen": min_roi_at_open,
            "minRoiAtOpenSource": "trade_create_config",
            "staleMinRoiFactorAtOpen": 0.5,
            **(note_overrides or {}),
        }
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            return db.add_profit_trade(
                trade_no=trade_no,
                market_hash_name="AK-47 | Redline (Field-Tested)",
                status="c5_listed",
                step_key="c5_listed",
                step_index=5,
                a_asset_id=asset_id,
                a_steam_id="steam-a",
                c5_product_id=product_id,
                steam_buy_price=100.0,
                steam_balance_discount=0.69,
                steam_real_cost=69.0,
                c5_listing_price=current_price,
                c5_expected_net_price=expected_net,
                expected_profit=expected_net - 69.0,
                expected_roi=expected_net / 100.0 - 0.69,
                note=profit_trade_module._build_note(note),
            )
        finally:
            db.close()

    def _c5_depth_client(self, *, lowest_price: float = 95.0) -> FakeC5SaleClient:
        c5_client = FakeC5SaleClient(
            active_product_ids=["123"],
            price_batch_price=lowest_price,
            price_batch_count=3,
        )
        c5_client.statistics = {
            "AK-47 | Redline (Field-Tested)": {
                "marketHashName": "AK-47 | Redline (Field-Tested)",
                "sellPrice": lowest_price,
                "sellCount": 3,
                "purchaseMaxPrice": lowest_price * 0.8,
                "purchaseCount": 1,
            }
        }
        return c5_client

    def test_refresh_listings_requires_real_execution(self) -> None:
        self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client()

        with self.assertRaisesRegex(RuntimeError, "allowRealExecution"):
            refresh_profit_trade_listings(
                self.settings,
                profit_config(profit_trade_allow_real_execution=False),
                c5_client=c5_client,
            )

        self.assertEqual([], c5_client.modify_calls)

    def test_refresh_listings_keeps_current_price_when_depth_api_fails(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client(lowest_price=95.0)
        c5_client.price_batch_error = RuntimeError("price batch unavailable")

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertTrue(any("price batch unavailable" in error for error in result["errors"]))
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertAlmostEqual(100.0, trade["c5_listing_price"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("listing_evidence_unavailable", note["listingRepriceDecision"])
            self.assertIn("price batch unavailable", note["listingRepriceBlockedReason"])
        finally:
            db.close()

    def test_refresh_listings_skips_protected_listed_trade(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client()

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_protected_market_hash_names=["AK-47 | Redline (Field-Tested)"],
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("protected", note["listingRepriceDecision"])
            self.assertIn("protected marketHashName", note["listingRepriceBlockedReason"])
        finally:
            db.close()

    def test_refresh_listings_skips_reprice_before_three_hour_cooldown(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=2.9)
        c5_client = self._c5_depth_client()

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertAlmostEqual(100.0, trade["c5_listing_price"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("cooldown", note["listingRepriceDecision"])
            self.assertIn("cooldown", note["listingRepriceCooldownReason"])
        finally:
            db.close()

    def test_refresh_listings_equal_to_price_batch_lowest_keeps_price_to_avoid_self_undercut(self) -> None:
        trade_id = self._create_c5_listed_trade(
            listed_hours_ago=3.1,
            current_price=95.0,
        )
        c5_client = self._c5_depth_client(lowest_price=95.0)

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("kept_possible_own_lowest", note["listingRepriceDecision"])
        finally:
            db.close()

    def test_refresh_listings_same_item_own_lowest_prevents_all_own_listings_from_chasing_it(self) -> None:
        lowest_trade_id = self._create_c5_listed_trade(
            listed_hours_ago=3.1,
            current_price=95.0,
            trade_no="PT-test-listed-lowest",
            asset_id="asset-lowest",
            product_id="product-lowest",
        )
        higher_trade_id = self._create_c5_listed_trade(
            listed_hours_ago=3.1,
            current_price=100.0,
            trade_no="PT-test-listed-higher",
            asset_id="asset-higher",
            product_id="product-higher",
        )
        c5_client = FakeC5SaleClient(
            active_product_ids=["product-lowest", "product-higher"],
            price_batch_price=95.0,
            price_batch_count=3,
        )

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertCountEqual(
            [lowest_trade_id, higher_trade_id],
            result["skippedTradeIds"],
        )
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            for trade_id in (lowest_trade_id, higher_trade_id):
                trade = db.get_profit_trade(trade_id)
                note = profit_trade_module._read_note(trade["note"])
                self.assertEqual("kept_possible_own_lowest", note["listingRepriceDecision"])
        finally:
            db.close()

    def test_refresh_listings_keeps_price_when_already_strictly_below_competitor(self) -> None:
        trade_id = self._create_c5_listed_trade(
            listed_hours_ago=3.1,
            current_price=94.0,
        )
        c5_client = self._c5_depth_client(lowest_price=95.0)

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertAlmostEqual(94.0, trade["c5_listing_price"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("kept_price_advantage", note["listingRepriceDecision"])
        finally:
            db.close()

    def test_refresh_listings_reprices_after_three_hour_cooldown(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.update_profit_trade(
                trade_id,
                error="old reprice error",
                note=profit_trade_module._build_note(
                    {
                        "c5ListedAt": (
                            datetime.now(timezone.utc) - timedelta(hours=3.1)
                        ).isoformat(),
                        "c5SalePrice": 100.0,
                        "c5ListingPrice": 100.0,
                        "c5ExpectedNetPrice": 99.0,
                        "expectedProfit": 30.0,
                        "expectedRoi": 0.30,
                    }
                ),
            )
        finally:
            db.close()
        c5_client = self._c5_depth_client()

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([trade_id], result["repricedTradeIds"])
        self.assertEqual(1, len(c5_client.modify_calls))
        payload = c5_client.modify_calls[0]["data_list"][0]
        self.assertEqual(123, payload["productId"])
        self.assertAlmostEqual(94.05, payload["price"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertAlmostEqual(94.05, trade["c5_listing_price"])
            self.assertAlmostEqual(93.1095, trade["c5_expected_net_price"])
            self.assertAlmostEqual(24.1095, trade["expected_profit"])
            self.assertAlmostEqual(0.241095, trade["expected_roi"])
            self.assertIsNone(trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("repriced", note["listingRepriceDecision"])
            self.assertAlmostEqual(94.05, note["c5SalePrice"])
            self.assertAlmostEqual(94.05, note["c5ListingPrice"])
            self.assertAlmostEqual(93.11, note["c5ExpectedNetPrice"])
            self.assertAlmostEqual(24.11, note["expectedProfit"])
            self.assertAlmostEqual(0.2411, note["expectedRoi"])
            self.assertAlmostEqual(100.0, note["repriceFrom"])
            self.assertAlmostEqual(94.05, note["repriceTo"])
            self.assertAlmostEqual(93.11, note["repriceExpectedNet"])
            self.assertAlmostEqual(0.2411, note["repriceExpectedRoi"])
            self.assertIn("repriceAt", note)
        finally:
            db.close()

    def test_refresh_listings_clamps_reprice_to_purchase_max_price_floor(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client(lowest_price=95.0)
        c5_client.statistics["AK-47 | Redline (Field-Tested)"]["purchaseMaxPrice"] = 94.5

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([trade_id], result["repricedTradeIds"])
        payload = c5_client.modify_calls[0]["data_list"][0]
        self.assertAlmostEqual(94.5, payload["price"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertAlmostEqual(94.5, trade["c5_listing_price"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("repriced", note["listingRepriceDecision"])
            self.assertAlmostEqual(94.5, note["repriceTo"])
            self.assertAlmostEqual(94.5, note["listingMarketStats"]["purchaseMaxPrice"])
        finally:
            db.close()

    def test_refresh_listings_skips_when_purchase_max_price_floor_is_above_current(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client(lowest_price=95.0)
        c5_client.statistics["AK-47 | Redline (Field-Tested)"]["purchaseMaxPrice"] = 105.0

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertAlmostEqual(100.0, trade["c5_listing_price"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("purchase_floor_cannot_win", note["listingRepriceDecision"])
            self.assertAlmostEqual(105.0, note["purchaseFloorPrice"])
        finally:
            db.close()

    def test_refresh_listings_keeps_listed_when_c5_modify_fails(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client()
        c5_client.modify_error = RuntimeError("temporary c5 modify error")

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual(1, len(c5_client.modify_calls))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertAlmostEqual(100.0, trade["c5_listing_price"])
            self.assertIn("temporary c5 modify error", trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("modify_failed", note["listingRepriceDecision"])
            self.assertIn("temporary c5 modify error", note["listingRepriceBlockedReason"])
        finally:
            db.close()

    def test_refresh_listings_blocks_reprice_when_c5_purchase_gap_is_too_large(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client(lowest_price=95.0)
        c5_client.statistics["AK-47 | Redline (Field-Tested)"]["purchaseMaxPrice"] = 50.0

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
                profit_trade_require_c5_market_depth=True,
                profit_trade_c5_min_purchase_sell_ratio=0.7,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertIn("purchase/sell ratio", trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("blocked_c5_market_depth", note["listingRepriceDecision"])
            self.assertIn("purchase/sell ratio", note["listingRepriceBlockedReason"])
            self.assertEqual("blocked_c5_purchase_price_gap", note["listingMarketStats"]["status"])
        finally:
            db.close()

    def test_refresh_listings_keeps_listed_when_reprice_roi_is_below_min(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client(lowest_price=75.0)

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
                profit_trade_min_roi=0.07,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertIn("< open ROI floor", trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("below_min_roi", note["listingRepriceDecision"])
            self.assertIn("< open ROI floor", note["listingRepriceBlockedReason"])
            self.assertAlmostEqual(74.25, note["repriceTargetPrice"])
        finally:
            db.close()

    def test_refresh_listings_after_twelve_hours_ignores_cooldown_and_reprices_by_one_percent(self) -> None:
        trade_id = self._create_c5_listed_trade(
            listed_hours_ago=12.1,
            note_overrides={"lastRepriceAt": datetime.now(timezone.utc).isoformat()},
        )
        c5_client = self._c5_depth_client(lowest_price=75.0)

        result = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_reprice_cooldown_hours=3.0,
                profit_trade_min_roi=0.07,
                profit_trade_stale_reprice_after_hours=12.0,
                profit_trade_stale_min_roi_factor=0.5,
            ),
            c5_client=c5_client,
        )

        self.assertEqual([trade_id], result["repricedTradeIds"])
        self.assertEqual(1, len(c5_client.modify_calls))
        payload = c5_client.modify_calls[0]["data_list"][0]
        self.assertAlmostEqual(74.25, payload["price"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertAlmostEqual(74.25, trade["c5_listing_price"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("repriced", note["listingRepriceDecision"])
            self.assertEqual("clearance", note["listingRepriceMode"])
            self.assertAlmostEqual(74.25, note["repriceTo"])
            self.assertAlmostEqual(0.0451, note["repriceExpectedRoi"], places=4)
            self.assertAlmostEqual(0.035, note["repriceRoiFloor"])
        finally:
            db.close()

    def test_refresh_listings_clearance_roi_floor_keeps_listed_without_alert_and_can_recover(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=12.1)
        c5_client = self._c5_depth_client(lowest_price=72.0)
        original_serverchan = profit_trade_module.ServerChanClient
        FakeServerChan.messages = []
        profit_trade_module.ServerChanClient = FakeServerChan
        self.settings.serverchan_sendkey = "send-key"
        try:
            result = refresh_profit_trade_listings(
                self.settings,
                profit_config(
                    profit_trade_allow_real_execution=True,
                    profit_trade_reprice_cooldown_hours=3.0,
                    profit_trade_stale_reprice_after_hours=12.0,
                    profit_trade_stale_min_roi_factor=0.5,
                ),
                c5_client=c5_client,
            )
        finally:
            profit_trade_module.ServerChanClient = original_serverchan

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        self.assertEqual([], FakeServerChan.messages)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("c5_listed", trade["status"])
            self.assertIsNone(trade["completed_at"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("clearance_roi_floor_reached", note["listingRepriceDecision"])
            self.assertAlmostEqual(71.28, note["repriceTargetPrice"])
            self.assertAlmostEqual(0.035, note["repriceRoiFloor"])
        finally:
            db.close()

        recovered_client = self._c5_depth_client(lowest_price=75.0)
        recovered = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_stale_reprice_after_hours=12.0,
                profit_trade_stale_min_roi_factor=0.5,
            ),
            c5_client=recovered_client,
        )
        self.assertEqual([trade_id], recovered["repricedTradeIds"])
        self.assertAlmostEqual(74.25, recovered_client.modify_calls[0]["data_list"][0]["price"])

    def test_refresh_listings_after_twenty_four_hours_marks_manual_before_depth_and_notifies_once(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=24.1)
        c5_client = self._c5_depth_client(lowest_price=95.0)
        c5_client.price_batch_error = RuntimeError("depth must not be queried after 24h")
        original_serverchan = profit_trade_module.ServerChanClient
        FakeServerChan.messages = []
        profit_trade_module.ServerChanClient = FakeServerChan
        self.settings.serverchan_sendkey = "send-key"
        try:
            result = refresh_profit_trade_listings(
                self.settings,
                profit_config(
                    profit_trade_allow_real_execution=True,
                    profit_trade_stale_manual_review_after_hours=24.0,
                ),
                c5_client=c5_client,
            )
        finally:
            profit_trade_module.ServerChanClient = original_serverchan

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        self.assertEqual([], c5_client.price_batch_calls)
        self.assertEqual(1, len(FakeServerChan.messages))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("manual_required", trade["status"])
            self.assertEqual("c5_listed", trade["step_key"])
            self.assertIsNone(trade["completed_at"])
            self.assertIn("more than stale manual review hours", trade["error"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertEqual("stale_manual_review", note["listingRepriceDecision"])
            self.assertEqual("listed too long without sale", note["listingRepriceBlockedReason"])
        finally:
            db.close()

        second = refresh_profit_trade_listings(
            self.settings,
            profit_config(
                profit_trade_allow_real_execution=True,
                profit_trade_stale_manual_review_after_hours=24.0,
            ),
            c5_client=c5_client,
        )
        self.assertEqual([], second["repricedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        self.assertEqual(1, len(FakeServerChan.messages))

    def test_refresh_listings_blocks_high_roi_reprice_and_sends_serverchan(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=3.1)
        c5_client = self._c5_depth_client(lowest_price=95.0)
        original_serverchan = profit_trade_module.ServerChanClient
        FakeServerChan.messages = []
        profit_trade_module.ServerChanClient = FakeServerChan
        self.settings.serverchan_sendkey = "send-key"
        try:
            result = refresh_profit_trade_listings(
                self.settings,
                profit_config(
                    profit_trade_allow_real_execution=True,
                    profit_trade_reprice_cooldown_hours=3.0,
                    profit_trade_require_c5_market_depth=True,
                    profit_trade_manual_review_roi=0.20,
                ),
                c5_client=c5_client,
            )
        finally:
            profit_trade_module.ServerChanClient = original_serverchan

        self.assertEqual([], result["repricedTradeIds"])
        self.assertEqual([trade_id], result["skippedTradeIds"])
        self.assertEqual([], c5_client.modify_calls)
        self.assertEqual(1, len(FakeServerChan.messages))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("manual_required", trade["status"])
            self.assertIn("manual review threshold", trade["error"])
        finally:
            db.close()

    def test_dismiss_profit_trade_hides_manual_required_trade(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            existing = db.get_profit_trade(trade_id)
            existing_note = profit_trade_module._read_note(existing["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                error="known failed buy",
            )
        finally:
            db.close()

        result = dismiss_profit_trade(
            self.settings,
            trade_id,
            reason="user acknowledged from dashboard",
        )

        self.assertTrue(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        dashboard = build_profit_trade_dashboard_payload(self.settings)
        self.assertNotIn(trade_id, [trade["id"] for trade in dashboard["trades"]])

    def test_dismiss_tracked_buy_order_cancels_and_confirms_before_hiding(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakePendingBuyOrderClient(total=10000)
        client.create_buy_order(price_total=10000)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                steam_listing_id="buy-order-1",
                steam_buy_price=100.0,
                error="unverified Steam buy order",
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "steamBuyMethod": "createbuyorder",
                        "steamBuyOrderId": "buy-order-1",
                        "steamBuyUnverifiedAt": "2026-07-10T01:00:00+00:00",
                        "steamId": "steam-a",
                        "walletBalanceBefore": 1000.0,
                        "beforeAssetIds": ["asset-a"],
                    }
                ),
            )
        finally:
            db.close()

        result = dismiss_profit_trade(
            self.settings,
            trade_id,
            reason="user requested safe close",
            steam_client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertEqual(1, len(client.cancel_buy_order_calls))
        note = result["trade"]["note"]
        self.assertEqual("cancelled", note["dismissBuyOrderResolution"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_refresh_listings_notification_error_does_not_block_twenty_four_hour_transition(self) -> None:
        class FailingServerChan:
            def __init__(self, *_: object, **__: object) -> None:
                pass

            def send(self, _title: str, _body: str) -> None:
                raise RuntimeError("serverchan unavailable")

        trade_id = self._create_c5_listed_trade(listed_hours_ago=24.1)
        c5_client = self._c5_depth_client()
        original_serverchan = profit_trade_module.ServerChanClient
        profit_trade_module.ServerChanClient = FailingServerChan
        self.settings.serverchan_sendkey = "send-key"
        try:
            result = refresh_profit_trade_listings(
                self.settings,
                profit_config(
                    profit_trade_allow_real_execution=True,
                    profit_trade_stale_manual_review_after_hours=24.0,
                ),
                c5_client=c5_client,
            )
        finally:
            profit_trade_module.ServerChanClient = original_serverchan

        self.assertTrue(any("serverchan unavailable" in error for error in result["errors"]))
        self.assertEqual([], c5_client.modify_calls)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("manual_required", trade["status"])
            self.assertEqual("c5_listed", trade["step_key"])
            self.assertIsNone(trade["completed_at"])
            note = profit_trade_module._read_note(trade["note"])
            self.assertIs(note["staleManualReviewServerChanSent"], False)
            self.assertIn("serverchan unavailable", note["staleManualReviewServerChanError"])
        finally:
            db.close()

    def test_dismiss_tracked_buy_order_refuses_to_hide_when_order_remains_active(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeCancelResponseButOrderRemainsActiveClient(total=10000)
        client.create_buy_order(price_total=10000)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                steam_listing_id="buy-order-1",
                steam_buy_price=100.0,
                error="unverified Steam buy order",
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "steamBuyMethod": "createbuyorder",
                        "steamBuyOrderId": "buy-order-1",
                        "steamBuyUnverifiedAt": "2026-07-10T01:00:00+00:00",
                        "steamId": "steam-a",
                        "walletBalanceBefore": 1000.0,
                        "beforeAssetIds": ["asset-a"],
                    }
                ),
            )
        finally:
            db.close()

        with self.assertRaisesRegex(RuntimeError, "still active|cannot safely hide"):
            dismiss_profit_trade(
                self.settings,
                trade_id,
                reason="user requested safe close",
                steam_client=client,
            )

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade = db.get_profit_trade(trade_id)
            self.assertEqual("manual_required", trade["status"])
            self.assertIsNotNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_dismiss_tracked_buy_order_restores_trade_when_cancel_races_with_fill(self) -> None:
        trade_id = self._create_locked_trade()
        client = FakeBuyOrderFillsDuringCancelClient(total=10000)
        client.create_buy_order(price_total=10000)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                steam_listing_id="buy-order-1",
                steam_buy_price=100.0,
                error="unverified Steam buy order",
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "steamBuyMethod": "createbuyorder",
                        "steamBuyOrderId": "buy-order-1",
                        "steamBuyUnverifiedAt": "2026-07-10T01:00:00+00:00",
                        "steamId": "steam-a",
                        "walletBalanceBefore": 1000.0,
                        "beforeAssetIds": ["asset-a"],
                    }
                ),
            )
        finally:
            db.close()

        result = dismiss_profit_trade(
            self.settings,
            trade_id,
            reason="user requested safe close",
            steam_client=client,
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["dismissed"])
        self.assertEqual("steam_bought", result["trade"]["status"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertIsNotNone(db.get_active_asset_reservation("asset-a"))
        finally:
            db.close()

    def test_dismiss_confirmed_duplicate_purchase_closes_incident_when_a_is_consumed_elsewhere(self) -> None:
        trade_id = self._create_locked_trade()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            successful_trade_id = db.add_profit_trade(
                trade_no="PT-successful-follow-up",
                market_hash_name="AK-47 | Redline (Field-Tested)",
                status="c5_listed",
                step_key="c5_listed",
                step_index=4,
                a_asset_id="asset-a",
                a_steam_id="steam-a",
                b_asset_id="asset-b-second",
                c5_product_id="c5-product-second",
            )
            self.assertTrue(
                db.consume_asset_reservation(
                    asset_id="asset-a",
                    owner="profit_trade",
                    operation_id=successful_trade_id,
                    note="A was consumed by the successful follow-up trade",
                )
            )
            db.update_profit_trade(
                trade_id,
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                steam_listing_id="buy-order-1",
                steam_buy_price=9.77,
                steam_balance_discount=0.69,
                steam_real_cost=6.7413,
                error="Steam buy request succeeded but purchase completion is not verified",
                note=profit_trade_module._build_note(
                    {
                        **note,
                        "steamBuyMethod": "createbuyorder",
                        "steamBuyOrderId": "buy-order-1",
                        "steamBuyRequestedAt": "2026-07-10T01:00:00+00:00",
                        "steamBuyUnverifiedAt": "2026-07-10T01:01:00+00:00",
                        "steamId": "steam-a",
                        "walletBalanceBefore": 100.0,
                        "walletDelta": 9.44,
                        "beforeAssetIds": ["asset-a"],
                    }
                ),
            )
        finally:
            db.close()

        steam_client = FakeHistoricalPurchaseClient(
            total=977,
            actual_total=9.44,
            wallet_balance=90.56,
        )
        result = dismiss_profit_trade(
            self.settings,
            trade_id,
            reason="user confirmed duplicate purchase incident",
            steam_client=steam_client,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["dismissed"])
        self.assertEqual("cancelled", result["trade"]["status"])
        self.assertEqual("asset-b-history", result["trade"]["bAssetId"])
        self.assertAlmostEqual(9.44, result["trade"]["steamBuyPrice"])
        self.assertAlmostEqual(9.44 * 0.69, result["trade"]["steamRealCost"])
        self.assertIsNone(result["trade"]["realizedProfit"])
        incident_note = result["trade"]["note"]
        self.assertTrue(incident_note["duplicatePurchaseIncident"])
        self.assertTrue(incident_note["purchaseActuallyCompleted"])
        self.assertEqual(
            "profit_trade_duplicate_purchase_incident_acknowledged",
            incident_note["cancelSource"],
        )
        self.assertEqual("asset-b-history", incident_note["steamPurchaseReceipt"]["newAssetId"])
        self.assertEqual(9.44, steam_client.purchase_receipt_calls[-1]["expected_total"])
        self.assertEqual(9.77, steam_client.purchase_receipt_calls[-1]["maximum_total"])

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            successful_trade = db.get_profit_trade(successful_trade_id)
            self.assertEqual("c5_listed", successful_trade["status"])
            self.assertEqual("c5-product-second", successful_trade["c5_product_id"])
            reservation = db.get_active_asset_reservation("asset-a")
            self.assertEqual("consumed", reservation["status"])
            self.assertEqual(successful_trade_id, reservation["operation_id"])
            acknowledgement = db.conn.execute(
                "SELECT * FROM profit_trade_acknowledgements WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
            self.assertIsNotNone(acknowledgement)
            self.assertEqual(1, acknowledgement["acknowledged"])
        finally:
            db.close()

        dashboard = build_profit_trade_dashboard_payload(self.settings)
        self.assertNotIn(trade_id, [trade["id"] for trade in dashboard["trades"]])

    def test_dismiss_profit_trade_blocks_live_c5_listed_trade(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=1.0)

        with self.assertRaisesRegex(RuntimeError, "live follow-up state"):
            dismiss_profit_trade(self.settings, trade_id)

    def test_manual_settle_uses_sold_net_price_for_final_roi(self) -> None:
        trade_id = self._create_c5_listed_trade(listed_hours_ago=4.0)

        result = manual_settle_profit_trade(
            self.settings,
            trade_id,
            sold_net_price=328.0,
            source="manual_other_platform",
            memo="other platform 328",
        )

        self.assertTrue(result["ok"])
        self.assertEqual("completed", result["trade"]["status"])
        self.assertAlmostEqual(328.0, result["trade"]["c5SoldNetPrice"])
        self.assertAlmostEqual(259.0, result["trade"]["realizedProfit"])
        self.assertAlmostEqual(328.0 / 100.0 - 0.69, result["trade"]["realizedRoi"])

    def test_refresh_sales_skips_c5_api_when_no_listed_trades(self) -> None:
        c5_client = FakeC5SaleClient(active_product_ids=[])

        result = refresh_profit_trade_sales(
            self.settings,
            profit_config(),
            c5_client=c5_client,
        )

        self.assertEqual([], result["settledTradeIds"])
        self.assertEqual([], result["skippedTradeIds"])
        self.assertEqual([], result["errors"])
        self.assertEqual([], c5_client.sale_search_calls)


class ProfitTradeManualRecordTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.temp_dir.name) / "assistant.db")
        save_strategy_config(self.settings, profit_config(profit_trade_balance_discount=0.69))
        db = Database(self.settings.db_path)
        try:
            db.initialize()
        finally:
            db.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_manual_record_converts_time_recalculates_and_skips_daily_budget(self) -> None:
        result = create_manual_profit_trade_record(
            self.settings,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            name="AK-47 | 红线（略有磨损）",
            steam_buy_price=100,
            balance_discount=0.69,
            c5_sold_net_price=75,
            steam_bought_at="2026-07-14T10:20:30+08:00",
            completed_at="2026-07-14T11:21:31+08:00",
            memo="historical manual trade",
        )

        trade = result["trade"]
        self.assertEqual("completed", trade["status"])
        self.assertEqual("settled", trade["stepKey"])
        self.assertEqual(6, trade["stepIndex"])
        self.assertEqual("manual_backfill", trade["recordOrigin"])
        self.assertEqual("2026-07-14T02:20:30+00:00", trade["steamBoughtAt"])
        self.assertEqual("2026-07-14T03:21:31+00:00", trade["completedAt"])
        self.assertAlmostEqual(69.0, trade["steamRealCost"])
        self.assertAlmostEqual(6.0, trade["realizedProfit"])
        self.assertAlmostEqual(0.06, trade["realizedRoi"])
        dashboard = build_profit_trade_dashboard_payload(self.settings)
        self.assertEqual(1, dashboard["summary"]["completedCount"])
        self.assertAlmostEqual(6.0, dashboard["summary"]["realizedProfit"])
        self.assertAlmostEqual(0.0, dashboard["summary"]["dailySteamSpent"])

    def test_update_completed_auto_record_preserves_original_times_and_writes_audit(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-auto-edit",
                market_hash_name="Dreams & Nightmares Case",
                status="completed",
                step_key="settled",
                step_index=6,
                steam_buy_price=10.0,
                steam_balance_discount=0.69,
                steam_real_cost=6.9,
                c5_sold_net_price=8.0,
                realized_profit=1.1,
                realized_roi=0.11,
                note=profit_trade_module._build_note(
                    {"steamBuySucceededAt": "2026-07-14T01:00:00+00:00"}
                ),
            )
            db.update_profit_trade(trade_id, completed_at="2026-07-14T02:00:00+00:00")
        finally:
            db.close()

        result = update_manual_profit_trade_record(
            self.settings,
            trade_id,
            market_hash_name="Dreams & Nightmares Case",
            steam_buy_price=12.0,
            balance_discount=0.68,
            c5_sold_net_price=9.5,
            steam_bought_at="2026-07-13T20:00:00+08:00",
            completed_at="2026-07-13T21:00:00+08:00",
            a_asset_id="asset-a",
            b_asset_id="asset-b",
            memo="corrected from Steam history",
        )

        trade = result["trade"]
        self.assertTrue(trade["manuallyEdited"])
        self.assertEqual("2026-07-13T12:00:00+00:00", trade["steamBoughtAt"])
        self.assertEqual("2026-07-13T13:00:00+00:00", trade["completedAt"])
        self.assertAlmostEqual(8.16, trade["steamRealCost"])
        self.assertAlmostEqual(1.34, trade["realizedProfit"])
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.get_profit_trade(trade_id)
            note = profit_trade_module._read_note(row["note"])
            self.assertEqual("2026-07-14T01:00:00+00:00", note["steamBuySucceededAt"])
            self.assertEqual("2026-07-13T12:00:00+00:00", note["manualSteamBoughtAtOverride"])
            self.assertEqual("2026-07-14T02:00:00+00:00", row["completed_at"])
            events = db.list_profit_trade_state_events(trade_id)
        finally:
            db.close()
        self.assertEqual("manual_edit", events[-1]["eventType"])

    def test_update_rejects_non_completed_and_invalid_inputs(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-not-completed",
                market_hash_name="Dreams & Nightmares Case",
                status="candidate",
            )
        finally:
            db.close()
        with self.assertRaisesRegex(RuntimeError, "only completed"):
            update_manual_profit_trade_record(
                self.settings,
                trade_id,
                market_hash_name="Dreams & Nightmares Case",
                steam_buy_price=10,
                balance_discount=0.69,
                c5_sold_net_price=8,
                steam_bought_at="2026-07-14T10:00:00+08:00",
                completed_at="2026-07-14T11:00:00+08:00",
            )
        with self.assertRaisesRegex(ValueError, "timezone"):
            create_manual_profit_trade_record(
                self.settings,
                market_hash_name="Dreams & Nightmares Case",
                steam_buy_price=10,
                balance_discount=0.69,
                c5_sold_net_price=8,
                steam_bought_at="2026-07-14T10:00:00",
                completed_at="2026-07-14T11:00:00+08:00",
            )
        with self.assertRaisesRegex(ValueError, "earlier"):
            create_manual_profit_trade_record(
                self.settings,
                market_hash_name="Dreams & Nightmares Case",
                steam_buy_price=10,
                balance_discount=0.69,
                c5_sold_net_price=8,
                steam_bought_at="2026-07-14T12:00:00+08:00",
                completed_at="2026-07-14T11:00:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()





















