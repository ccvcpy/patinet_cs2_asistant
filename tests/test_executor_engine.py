from __future__ import annotations

import json
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
from cs2_assistant.clients import C5GameError
from cs2_assistant.config import Settings
from cs2_assistant.clients.steam_market import (
    SteamInventoryAssetLookupResult,
    SteamListing,
    SteamMarketError,
    SteamMyListingsSnapshot,
    SteamSaleReceiptLookupResult,
)
from cs2_assistant.db import Database
from cs2_assistant.models import (
    CatalogItem,
    OP_REBUY_C5,
    OP_SELL_STEAM,
    OP_TRANSFER_BUY,
    OP_TRANSFER_SELL,
    POOL_STATUS_HOLDING,
    POOL_STATUS_LISTED,
    POOL_STATUS_LISTING_PENDING,
    POOL_STATUS_PENDING_REBUY,
    POOL_STATUS_TRANSFER_HOLDING,
    POOL_STATUS_TRANSFER_SOLD,
    STRATEGY_GUADAO,
    STRATEGY_TRANSFER,
    StrategyCandidate,
    StrategyConfig,
)
from cs2_assistant.services.executor_buy import RebuyResult
from cs2_assistant.services.executor_engine import ExecutionEngine, ListingDecision
from cs2_assistant.services.steam_request_scheduler import SteamRequestGuardRejected


class FakeSteamClient:
    def __init__(self) -> None:
        self.steam_id64 = "76561198000000000"
        self.buy_calls: list[dict[str, object]] = []
        self.active_listing_ids: set[str] = set()
        self.active_listing_assets: dict[str, str] = {}
        self.pending_listing_assets: dict[str, str] = {}
        self.sale_receipts: dict[str, dict[str, object]] = {}
        self.sale_receipts_by_asset: dict[str, dict[str, object]] = {}
        self.removed_listing_ids: list[str] = []
        self.remove_listing_should_fail = False
        self.sell_calls: list[dict[str, object]] = []
        self.trade_url = "https://steamcommunity.com/tradeoffer/new/?partner=39734272&token=abc"
        self.confirm_calls = 0
        self.confirm_asset_calls: list[dict[str, object]] = []
        self.confirm_should_fail = False
        self.confirm_result = 1
        self.orderbook_should_fail = False
        self.orderbook_payload: dict | None = None
        self.orderbook_calls: list[dict[str, object]] = []
        self.orderbook_safety_terminal_calls: list[bool] = []
        self.price_overview_should_fail = False
        self.list_active_listing_safety_terminal_calls: list[bool] = []

    def order_book(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        safety_terminal: bool = False,
    ) -> dict:
        self.orderbook_safety_terminal_calls.append(bool(safety_terminal))
        self.orderbook_calls.append({"app_id": app_id, "market_hash_name": market_hash_name})
        if self.orderbook_should_fail:
            raise SteamMarketError("steam ssl eof")
        if self.orderbook_payload is not None:
            return self.orderbook_payload
        return {
            "success": 1,
            "data": {
                "eCurrency": 23,
                "rgCompactSellOrders": [
                    2500, 20,
                ],
            },
        }

    def price_overview(self, *, app_id: int, market_hash_name: str, country: str, currency: int) -> dict:
        if self.price_overview_should_fail:
            raise SteamMarketError("priceoverview boom")
        return {"success": True, "lowest_price": "¥ 25.00"}

    def search_listings(self, *, app_id: int, market_hash_name: str, start: int = 0, count: int = 10) -> dict:
        return {
            "success": 1,
            "listinginfo": {
                "listing-low": {
                    "listingid": "listing-low",
                    "converted_price": 2200,
                    "converted_fee": 300,
                    "converted_total": 2500,
                },
                "listing-high": {
                    "listingid": "listing-high",
                    "converted_price": 2300,
                    "converted_fee": 300,
                    "converted_total": 2600,
                },
            },
        }

    def buy_listing(self, **kwargs: object) -> dict:
        self.buy_calls.append(dict(kwargs))
        return {"wallet_info": {"success": 1}}

    def get_trade_url(self) -> str:
        return self.trade_url

    def sell_item(self, **kwargs: object) -> dict:
        self.sell_calls.append(dict(kwargs))
        return {
            "listingid": "listing-1",
        }

    def confirm_all(self) -> int:
        raise AssertionError("confirm_all must not be used; use confirm_listing_assets")

    def confirm_listing_assets(self, *, asset_ids: object, listing_ids: object | None = None) -> int:
        self.confirm_calls += 1
        self.confirm_asset_calls.append({"asset_ids": asset_ids, "listing_ids": listing_ids})
        if self.confirm_should_fail:
            raise RuntimeError("confirm boom")
        return self.confirm_result

    def list_active_listings(self, *, safety_terminal: bool = False) -> list[object]:
        self.list_active_listing_safety_terminal_calls.append(bool(safety_terminal))
        class Listing:
            def __init__(self, listing_id: str, asset_id: str | None = None) -> None:
                self.listing_id = listing_id
                self.asset_id = asset_id

        listings = [Listing(listing_id) for listing_id in sorted(self.active_listing_ids)]
        listings.extend(
            Listing(listing_id, asset_id)
            for asset_id, listing_id in sorted(self.active_listing_assets.items())
        )
        return listings

    def list_confirmation_pending_listings(self) -> list[object]:
        class Listing:
            def __init__(self, listing_id: str, asset_id: str | None = None) -> None:
                self.listing_id = listing_id
                self.asset_id = asset_id

        return [
            Listing(listing_id, asset_id)
            for asset_id, listing_id in sorted(self.pending_listing_assets.items())
        ]

    def remove_listing(
        self,
        listing_id: str,
        *,
        execution_guard: object | None = None,
    ) -> bool:
        if execution_guard is not None:
            if not callable(execution_guard):
                raise TypeError("execution_guard must be callable")
            if not bool(execution_guard()):
                raise SteamRequestGuardRejected("fake Steam request guard rejected")
        if self.remove_listing_should_fail:
            return False
        self.removed_listing_ids.append(listing_id)
        for asset_id, pending_listing_id in list(self.pending_listing_assets.items()):
            if pending_listing_id == listing_id:
                del self.pending_listing_assets[asset_id]
        for asset_id, active_listing_id in list(self.active_listing_assets.items()):
            if active_listing_id == listing_id:
                del self.active_listing_assets[asset_id]
        self.active_listing_ids.discard(listing_id)
        return True

    def find_sale_receipt(self, listing_id: str) -> dict[str, object] | None:
        return self.sale_receipts.get(listing_id)

    def find_sale_receipt_by_asset(self, asset_id: str) -> dict[str, object] | None:
        return self.sale_receipts_by_asset.get(asset_id)


class FakeC5Client:
    def __init__(self) -> None:
        self.sale_create_calls: list[dict[str, object]] = []
        self.price_batch_calls: list[dict[str, object]] = []
        self.price_batch_payload: dict[str, object] | None = None
        self.price_batch_should_fail = False
        self.market_products_search_calls: list[dict[str, object]] = []
        self.market_products_search_payload: dict[str, object] = {"list": []}
        self.quick_buy_calls: list[dict[str, object]] = []
        self.quick_buy_results: list[dict[str, object] | Exception] = []
        self.batch_buy_calls: list[dict[str, object]] = []
        self.batch_buy_payload_by_trade_url: dict[str, dict[str, object]] = {}
        self.batch_buy_should_raise: Exception | None = None

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        self.price_batch_calls.append(
            {"market_hash_names": list(market_hash_names), "app_id": app_id}
        )
        if self.price_batch_should_fail:
            raise RuntimeError("c5 price batch unavailable")
        if self.price_batch_payload is not None:
            return self.price_batch_payload
        return {
            market_hash_names[0]: {
                "price": 20.0,
                "count": 1,
                "itemId": "item-default",
            }
        }

    def sale_create(self, *, app_id: int, items: list[dict]) -> dict:
        self.sale_create_calls.append({"app_id": app_id, "items": items})
        return {
            "shopOn": True,
            "succeed": 1,
            "failed": 0,
            "successList": [
                {
                    "assetId": items[0]["assetId"],
                    "productId": "product-1",
                }
            ],
        }

    def sale_search(self, *, app_id: int, steam_id: str | None = None, delivery: int | None = None, page: int = 1, limit: int = 20) -> dict:
        return {"total": 0, "page": page, "limit": limit, "list": []}

    def market_products_search(
        self,
        *,
        item_id: str,
        page_size: int,
    ) -> dict:
        self.market_products_search_calls.append(
            {
                "item_id": item_id,
                "page_size": page_size,
            }
        )
        return dict(self.market_products_search_payload)

    def quick_buy(self, **kwargs: object) -> dict[str, object]:
        self.quick_buy_calls.append(dict(kwargs))
        if not self.quick_buy_results:
            raise AssertionError("unexpected quick_buy call")
        result = self.quick_buy_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return dict(result)

    def batch_buy(self, *, product_list: list[dict], trade_url: str) -> dict:
        self.batch_buy_calls.append(
            {"product_list": [dict(row) for row in product_list], "trade_url": trade_url}
        )
        if self.batch_buy_should_raise is not None:
            raise self.batch_buy_should_raise
        payload = self.batch_buy_payload_by_trade_url.get(trade_url)
        return dict(payload) if payload is not None else {"successList": [], "failedList": product_list}


class FakeServerChan:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def send(self, title: str, body: str) -> None:
        self.messages.append({"title": title, "body": body})


class FakeGuadaoLogger:
    def __init__(self, *, fail_emit: bool = False) -> None:
        self.fail_emit = fail_emit
        self.events: list[dict[str, object]] = []
        self.bound_contexts: list[dict[str, object]] = []

    def emit(self, **event: object) -> dict[str, object]:
        if self.fail_emit:
            raise OSError("log write failed")
        self.events.append(dict(event))
        return dict(event)

    def bind_telemetry(self, **context: object):
        self.bound_contexts.append(dict(context))

        def callback(event: dict[str, object]) -> None:
            self.events.append({**context, **event, "source": "guadao"})

        return callback


class FakeAccountStore:
    def __init__(self, accounts: list[Account] | None = None) -> None:
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.accounts = list(accounts or [])

    def update_account(self, account_id_or_name: str, **kwargs: object) -> None:
        self.updates.append((account_id_or_name, dict(kwargs)))

    def list_accounts(self) -> list[Account]:
        return list(self.accounts)

    def get_account(self, account_id_or_name: str) -> Account | None:
        lookup = str(account_id_or_name)
        return next(
            (
                account
                for account in self.accounts
                if account.id == lookup or account.name == lookup
            ),
            None,
        )


def build_candidate() -> StrategyCandidate:
    return StrategyCandidate(
        name="Revolution Case",
        market_hash_name="Revolution Case",
        inventory_count=1,
        tradable_count=1,
        rebuy_price=20.0,
        rebuy_price_source="c5_batch",
        steam_sell_price=25.0,
        steam_price_source="steam_market",
        steam_after_tax_price=21.73,
        listing_ratio=0.92,
        transfer_real_ratio=0.07,
        recommended_strategies=[STRATEGY_TRANSFER],
        steam_accounts=["main-steam"],
    )


def build_guadao_candidate() -> StrategyCandidate:
    return StrategyCandidate(
        name="Revolution Case",
        market_hash_name="Revolution Case",
        inventory_count=1,
        tradable_count=1,
        rebuy_price=20.0,
        rebuy_price_source="c5_batch",
        steam_sell_price=25.0,
        steam_price_source="steam_orderbook",
        steam_after_tax_price=21.73,
        listing_ratio=0.92,
        transfer_real_ratio=0.07,
        recommended_strategies=[STRATEGY_GUADAO],
        steam_accounts=["main-steam"],
    )


def build_guadao_candidate_for(market_hash_name: str, *, rebuy_price: float = 20.0) -> StrategyCandidate:
    return StrategyCandidate(
        name=market_hash_name,
        market_hash_name=market_hash_name,
        inventory_count=1,
        tradable_count=1,
        rebuy_price=rebuy_price,
        rebuy_price_source="c5_batch",
        steam_sell_price=25.0,
        steam_price_source="steam_orderbook",
        steam_after_tax_price=21.73,
        listing_ratio=0.69,
        transfer_real_ratio=0.07,
        recommended_strategies=[STRATEGY_GUADAO],
        steam_accounts=["main-steam"],
    )


class ExecutorEngineTransferTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "assistant.db"
        self.db = Database(self.db_path)
        self.db.initialize()

        self.engine = object.__new__(ExecutionEngine)
        self.engine.settings = Settings(db_path=self.db_path, app_id=730)
        self.engine.config = StrategyConfig(
            execution_enabled=True,
            dry_run=False,
            max_transfer_buy_per_cycle=3,
            max_list_per_cycle=3,
            transfer_min_real_ratio=0.05,
        )
        self.engine.db = self.db
        self.engine.c5_client = FakeC5Client()
        self.engine.steam_client = FakeSteamClient()
        self.engine.serverchan = None
        self.engine.account = None
        self.engine.account_store = None
        self.engine._steam_trade_url = None
        self.engine._last_inventory_payload = {}
        self.engine._inventory_items_by_asset_id = {}
        self.engine._pending_confirmation_count = 0
        self.engine._market_pending_cleanup_failed_count = 0
        self.engine._process_session_id = "test-process-session"

        self.db.upsert_pool_item("Revolution Case", 1, status=POOL_STATUS_HOLDING)
        old_asset = {
            "assetId": "asset-old",
            "marketHashName": "Revolution Case",
            "steamId": self.engine.steam_client.steam_id64,
            "ifTradable": True,
            "tradableTime": None,
            "token": "token-old",
            "styleToken": "style-old",
            "price": 20.0,
        }
        self.db.upsert_inventory_assets([old_asset])
        self.engine._inventory_items_by_asset_id = {"asset-old": old_asset}

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_execution_engine_binds_c5_client_to_guadao_telemetry(self) -> None:
        logger = FakeGuadaoLogger()
        account = Account(
            id="account-a",
            name="main-account",
            steam_id64="76561198000000000",
            c5_api_key="test-c5-key",
        )
        config = StrategyConfig(execution_enabled=False)
        with patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=logger,
        ), patch("cs2_assistant.services.executor_engine.C5GameClient") as c5_client_class:
            engine = ExecutionEngine(
                Settings(db_path=Path(self.temp_dir.name) / "telemetry.db"),
                config,
                account=account,
            )
            try:
                kwargs = c5_client_class.call_args.kwargs
                self.assertEqual(
                    {
                        "source": "guadao",
                        "account_id": "account-a",
                        "steam_id64": "76561198000000000",
                    },
                    kwargs["telemetry_context"],
                )
                kwargs["telemetry_callback"](
                    {
                        "provider": "c5",
                        "component": "c5game",
                        "operation": "buyer_order_detail",
                        "message": "C5 request succeeded",
                    }
                )
                self.assertEqual("guadao", logger.events[-1]["source"])
                self.assertEqual("account-a", logger.events[-1]["account_id"])
            finally:
                engine.close()

    def test_account_client_for_stale_recheck_disables_internal_relogin(self) -> None:
        """The standalone stale walk must not turn a 400 into an auto-login."""

        account = Account(
            id="stale-account",
            name="stale-account",
            steam_id64="76561198000000123",
            cookies="sessionid=stale-session; steamLoginSecure=stale-cookie",
        )
        self.engine.account_store = FakeAccountStore([account])
        captured: dict[str, object] = {}

        class CapturingSteamClient:
            steam_id64 = account.steam_id64

            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

        with patch(
            "cs2_assistant.services.executor_engine.SteamMarketClient",
            CapturingSteamClient,
        ):
            client = self.engine._steam_client_for_account(
                account,
                account.steam_id64,
                validate_session=False,
                allow_relogin=False,
            )

        self.assertIsNotNone(client)
        self.assertIs(captured["allow_account_relogin"], False)

    def test_stale_recheck_does_not_reuse_a_relogin_enabled_cached_client(self) -> None:
        account = Account(
            id="cached-account",
            name="cached-account",
            steam_id64="76561198000000456",
            cookies="sessionid=cached-session; steamLoginSecure=cached-cookie",
        )
        self.engine.account_store = FakeAccountStore([account])
        cached = type(
            "CachedSteamClient",
            (),
            {
                "steam_id64": account.steam_id64,
                "_allow_account_relogin": True,
            },
        )()
        self.engine._steam_clients = {
            f"account:{account.id}": cached,
            f"steam:{account.steam_id64}": cached,
        }

        created: list[object] = []

        class CapturingSteamClient:
            steam_id64 = account.steam_id64

            def __init__(self, **kwargs: object) -> None:
                self._allow_account_relogin = bool(kwargs["allow_account_relogin"])
                created.append(self)

        with patch(
            "cs2_assistant.services.executor_engine.SteamMarketClient",
            CapturingSteamClient,
        ):
            client = self.engine._steam_client_for_account(
                account,
                account.steam_id64,
                validate_session=False,
                allow_relogin=False,
            )

        self.assertEqual(1, len(created))
        self.assertIs(client, created[0])
        self.assertFalse(bool(getattr(client, "_allow_account_relogin")))
        self.assertIs(
            cached,
            self.engine._steam_clients[f"account:{account.id}"],
            "the one-off no-relogin client must not replace the normal cached client",
        )

    def test_guadao_local_event_is_fail_open_and_does_not_create_gd_zero(self) -> None:
        failing_logger = FakeGuadaoLogger(fail_emit=True)
        with patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=failing_logger,
        ):
            self.engine._emit_guadao_local_event(
                operation="state_change",
                message="must remain fail-open",
                operation_id=1,
            )

        logger = FakeGuadaoLogger()
        with patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=logger,
        ):
            self.engine._emit_guadao_local_event(
                operation="state_change",
                message="no persisted operation yet",
                operation_id=0,
            )

        self.assertIsNone(logger.events[0]["trade_id"])
        self.assertIsNone(logger.events[0]["trade_no"])

    def test_pick_tradable_asset_skips_profit_trade_reserved_asset(self) -> None:
        reserved_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        reservation_id = self.db.reserve_asset(
            asset_id="asset-old",
            market_hash_name="Revolution Case",
            owner="profit_trade",
            purpose="sell_existing_a",
            reserved_until=reserved_until,
        )

        self.assertIsNotNone(reservation_id)
        asset = self.db.pick_tradable_asset(
            "Revolution Case",
            steam_id=self.engine.steam_client.steam_id64,
        )
        self.assertIsNone(asset)

    def test_live_inventory_refresh_recovers_orphan_listing_failed_asset(self) -> None:
        self.db.set_asset_status("asset-old", "listing_failed")

        self.db.upsert_inventory_assets(
            [
                {
                    "assetId": "asset-old",
                    "marketHashName": "Revolution Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                }
            ]
        )

        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("available", asset["status"])
        self.assertEqual(1, asset["tradable"])

    def test_live_inventory_refresh_preserves_active_listing_states(self) -> None:
        for status in ("listed", "sold", "listing_pending"):
            with self.subTest(status=status):
                self.db.set_asset_status("asset-old", status)
                self.db.upsert_inventory_assets(
                    [
                        {
                            "assetId": "asset-old",
                            "marketHashName": "Revolution Case",
                            "steamId": self.engine.steam_client.steam_id64,
                            "ifTradable": True,
                        }
                    ]
                )
                asset = self.db.get_asset("asset-old")
                assert asset is not None
                self.assertEqual(status, asset["status"])

    def test_transfer_sell_asset_skips_profit_trade_reserved_asset(self) -> None:
        reserved_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        self.db.reserve_asset(
            asset_id="asset-old",
            market_hash_name="Revolution Case",
            owner="profit_trade",
            purpose="sell_existing_a",
            reserved_until=reserved_until,
        )

        asset_id, inventory_item = self.engine._find_transfer_sell_asset("Revolution Case")

        self.assertIsNone(asset_id)
        self.assertIsNone(inventory_item)

    def test_case_listing_uses_case_specific_price_offset(self) -> None:
        self.engine.config.listing_price_offset = 0.01
        self.engine.config.case_listing_price_offset = -0.01

        candidate = build_guadao_candidate()

        self.assertEqual(-0.01, self.engine._listing_price_offset_for_candidate(candidate))
        self.assertEqual(20, self.engine._listing_wall_min_count_for_candidate(candidate))

    def test_non_case_listing_uses_default_price_offset(self) -> None:
        self.engine.config.listing_price_offset = 0.01
        self.engine.config.case_listing_price_offset = -0.01
        self.engine.config.listing_wall_min_count = 20
        self.db.upsert_items(
            [
                CatalogItem(
                    market_hash_name="AK-47 | Redline (Field-Tested)",
                    name_cn="AK-47 | 红线 (久经沙场)",
                    c5_item_id=None,
                    steam_item_id=None,
                    raw_json={
                        "marketHashName": "AK-47 | Redline (Field-Tested)",
                        "name": "AK-47 | 红线 (久经沙场)",
                        "typeName": "步枪",
                    },
                )
            ]
        )
        candidate = StrategyCandidate(
            name="AK-47 | Redline (Field-Tested)",
            market_hash_name="AK-47 | Redline (Field-Tested)",
            inventory_count=1,
            tradable_count=1,
            rebuy_price=100.0,
            rebuy_price_source="c5_batch",
            steam_sell_price=150.0,
            steam_price_source="steam_market",
            steam_after_tax_price=130.35,
            listing_ratio=0.77,
            transfer_real_ratio=-0.06,
            recommended_strategies=[STRATEGY_GUADAO],
            steam_accounts=["main-steam"],
        )

        self.assertEqual(0.01, self.engine._listing_price_offset_for_candidate(candidate))
        self.assertEqual(1, self.engine._listing_wall_min_count_for_candidate(candidate))

    def test_guadao_scope_filters_case_and_non_case_candidates(self) -> None:
        self.db.upsert_items(
            [
                CatalogItem(
                    market_hash_name="AK-47 | Redline (Field-Tested)",
                    name_cn="AK-47 | Redline (Field-Tested)",
                    raw_json={"typeName": "Rifle"},
                )
            ]
        )

        self.engine.config.guadao_item_scope = "crates_only"
        self.assertTrue(self.engine._guadao_scope_allows_market_hash_name("Revolution Case"))
        self.assertFalse(
            self.engine._guadao_scope_allows_market_hash_name("AK-47 | Redline (Field-Tested)")
        )

        self.engine.config.guadao_item_scope = "non_case_only"
        self.assertFalse(self.engine._guadao_scope_allows_market_hash_name("Revolution Case"))
        self.assertTrue(
            self.engine._guadao_scope_allows_market_hash_name("AK-47 | Redline (Field-Tested)")
        )

    def test_strategy_scan_pool_names_are_prefiltered_when_transfer_is_disabled(self) -> None:
        market_hash_name = "AK-47 | Redline (Field-Tested)"
        self.db.upsert_items(
            [
                CatalogItem(
                    market_hash_name=market_hash_name,
                    name_cn=market_hash_name,
                    raw_json={"typeName": "Rifle"},
                )
            ]
        )
        self.engine.config.guadao_item_scope = "crates_only"
        self.engine.config.transfer_min_real_ratio = 9999

        pool_names = self.engine._pool_names_for_strategy_scan(["Revolution Case", market_hash_name])

        self.assertEqual(["Revolution Case"], pool_names)

    def test_strategy_scan_pool_names_are_not_prefiltered_when_transfer_is_enabled(self) -> None:
        market_hash_name = "AK-47 | Redline (Field-Tested)"
        self.engine.config.guadao_item_scope = "crates_only"
        self.engine.config.transfer_min_real_ratio = 0.05

        pool_names = self.engine._pool_names_for_strategy_scan(["Revolution Case", market_hash_name])

        self.assertEqual(["Revolution Case", market_hash_name], pool_names)

    def test_dry_run_listing_decision_falls_back_to_scan_price_when_steam_wall_unavailable(self) -> None:
        self.engine.config.dry_run = True
        self.engine.steam_client.orderbook_should_fail = True
        self.engine.steam_client.price_overview_should_fail = True
        candidate = build_guadao_candidate()

        with patch("builtins.print") as print_mock:
            decision = self.engine._decide_listing(candidate)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(25.0, decision.list_price)
        self.assertIsNotNone(decision.pricing)
        assert decision.pricing is not None
        self.assertEqual("scan_orderbook_snapshot", decision.pricing.reason)

    def test_sync_assets_updates_pool_from_inventory(self) -> None:
        self.db.remove_pool_item("Revolution Case")
        payload = {
            "list": [
                {
                    "assetId": "asset-auto",
                    "marketHashName": "Kilowatt Case",
                    "name": "Kilowatt Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 1.35,
                }
            ]
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        self.assertEqual(["Kilowatt Case"], self.db.get_pool_market_hash_names())
        assets = self.db.list_assets(market_hash_name="Kilowatt Case")
        self.assertEqual(1, len(assets))

    def test_sync_assets_zeroes_holding_pool_item_absent_from_live_inventory(self) -> None:
        payload = {
            "list": [
                {
                    "assetId": "asset-auto",
                    "marketHashName": "Kilowatt Case",
                    "name": "Kilowatt Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 1.35,
                }
            ]
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        pool_rows = {row["market_hash_name"]: row for row in self.db.list_pool_items()}
        self.assertEqual(0, pool_rows["Revolution Case"]["quantity"])
        self.assertEqual(POOL_STATUS_HOLDING, pool_rows["Revolution Case"]["status"])
        self.assertEqual(["Kilowatt Case"], self.db.get_pool_market_hash_names())

    def test_sync_assets_deletes_available_assets_absent_from_live_inventory(self) -> None:
        payload = {
            "list": [
                {
                    "assetId": "asset-auto",
                    "marketHashName": "Kilowatt Case",
                    "name": "Kilowatt Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 1.35,
                }
            ]
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        self.assertIsNone(self.db.get_asset("asset-old"))

    def test_sync_assets_deletes_locked_assets_absent_from_live_inventory(self) -> None:
        self.db.set_asset_status("asset-old", "locked")
        payload = {
            "list": [
                {
                    "assetId": "asset-auto",
                    "marketHashName": "Kilowatt Case",
                    "name": "Kilowatt Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 1.35,
                }
            ]
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        self.assertIsNone(self.db.get_asset("asset-old"))

    def test_sync_assets_keeps_stateful_assets_absent_from_live_inventory(self) -> None:
        self.db.set_asset_status("asset-old", "listed")
        payload = {
            "list": [
                {
                    "assetId": "asset-auto",
                    "marketHashName": "Kilowatt Case",
                    "name": "Kilowatt Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 1.35,
                }
            ]
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        old_asset = self.db.get_asset("asset-old")
        self.assertIsNotNone(old_asset)
        assert old_asset is not None
        self.assertEqual("listed", old_asset["status"])

    def test_sync_assets_keeps_pending_assets_absent_from_live_inventory(self) -> None:
        self.db.set_asset_status("asset-old", "listing_pending")
        payload = {
            "list": [
                {
                    "assetId": "asset-auto",
                    "marketHashName": "Kilowatt Case",
                    "name": "Kilowatt Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 1.35,
                }
            ]
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        old_asset = self.db.get_asset("asset-old")
        self.assertIsNotNone(old_asset)
        assert old_asset is not None
        self.assertEqual("listing_pending", old_asset["status"])

    def test_sync_assets_keeps_pending_assets_present_in_live_inventory(self) -> None:
        self.db.set_asset_status("asset-old", "listing_pending")
        payload = {
            "list": [
                {
                    "assetId": "asset-old",
                    "marketHashName": "Revolution Case",
                    "name": "Revolution Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 20.0,
                }
            ]
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        old_asset = self.db.get_asset("asset-old")
        self.assertIsNotNone(old_asset)
        assert old_asset is not None
        self.assertEqual("listing_pending", old_asset["status"])

    def test_sync_assets_keeps_open_pool_item_absent_from_live_inventory(self) -> None:
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        payload = {
            "list": [
                {
                    "assetId": "asset-auto",
                    "marketHashName": "Kilowatt Case",
                    "name": "Kilowatt Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 1.35,
                }
            ]
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        pool_rows = {row["market_hash_name"]: row for row in self.db.list_pool_items()}
        self.assertEqual(1, pool_rows["Revolution Case"]["quantity"])
        self.assertEqual(POOL_STATUS_LISTED, pool_rows["Revolution Case"]["status"])
        self.assertEqual(["Kilowatt Case", "Revolution Case"], self.db.get_pool_market_hash_names())

    def test_sync_assets_does_not_zero_pool_from_cached_inventory(self) -> None:
        payload = {
            "source": "cache",
            "list": [
                {
                    "assetId": "asset-auto",
                    "marketHashName": "Kilowatt Case",
                    "name": "Kilowatt Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "price": 1.35,
                }
            ],
        }

        with patch("cs2_assistant.services.executor_engine.fetch_all_c5_inventories", return_value=payload):
            self.engine._sync_assets()

        pool_rows = {row["market_hash_name"]: row for row in self.db.list_pool_items()}
        self.assertEqual(1, pool_rows["Revolution Case"]["quantity"])
        self.assertEqual(["Kilowatt Case", "Revolution Case"], self.db.get_pool_market_hash_names())
        old_asset = self.db.get_asset("asset-old")
        self.assertIsNotNone(old_asset)
        assert old_asset is not None
        self.assertEqual("available", old_asset["status"])
        self.assertEqual(1, old_asset["tradable"])

    def test_real_listing_decision_requires_live_steam_price(self) -> None:
        self.engine.config.dry_run = False
        candidate = build_guadao_candidate()
        candidate.steam_price_source = "steamdt"

        with patch("builtins.print") as print_mock:
            decision = self.engine._decide_listing(candidate)

        self.assertIsNone(decision)
        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("Steam orderbook", printed)

    def test_real_listing_decision_reuses_scan_orderbook_price(self) -> None:
        self.engine.config.dry_run = False
        candidate = build_guadao_candidate()
        candidate.steam_price_source = "steam_orderbook"

        decision = self.engine._decide_listing(candidate)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(25.0, round(decision.list_price, 2))
        self.assertIsNotNone(decision.pricing)
        assert decision.pricing is not None
        self.assertEqual("scan_orderbook_snapshot", decision.pricing.reason)

    def test_non_case_listing_decision_reuses_scan_orderbook_price(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.listing_price_offset = 0.01
        self.engine.config.listing_wall_min_count = 20
        market_hash_name = "AK-47 | Redline (Field-Tested)"
        self.db.upsert_items(
            [
                CatalogItem(
                    market_hash_name=market_hash_name,
                    name_cn=market_hash_name,
                    raw_json={"typeName": "Rifle"},
                )
            ]
        )
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {
                "rgCompactSellOrders": [
                    2500,
                    1,
                    2600,
                    19,
                ],
            },
        }
        candidate = StrategyCandidate(
            name=market_hash_name,
            market_hash_name=market_hash_name,
            inventory_count=1,
            tradable_count=1,
            rebuy_price=10.0,
            rebuy_price_source="c5_batch",
            steam_sell_price=25.0,
            steam_price_source="steam_orderbook",
            steam_after_tax_price=21.73,
            listing_ratio=0.50,
            transfer_real_ratio=-0.1,
            recommended_strategies=[STRATEGY_GUADAO],
            steam_accounts=["main-steam"],
        )

        decision = self.engine._decide_listing(candidate)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(25.0, round(decision.list_price, 2))
        self.assertIsNotNone(decision.pricing)
        assert decision.pricing is not None
        self.assertIsNone(decision.pricing.wall_price)
        self.assertEqual("scan_orderbook_snapshot", decision.pricing.reason)

    @patch("cs2_assistant.services.executor_engine.scan_strategies")
    def test_guadao_scan_reuses_single_snapshot_and_keeps_filtered_rows(
        self,
        scan_mock: object,
    ) -> None:
        self.engine._sync_assets = lambda: None  # type: ignore[method-assign]
        self.engine._refresh_scan_listing_prices_from_steam = (  # type: ignore[method-assign]
            lambda _report: (_ for _ in ()).throw(
                AssertionError("scan must not refetch every successful orderbook")
            )
        )
        self.engine._guadao_account_inventory_infos = lambda _report: []  # type: ignore[method-assign]
        self.engine._execute_guadao_listings = lambda _report, _status_map: 0  # type: ignore[method-assign]
        self.engine._release_full_case_listing_capacity = lambda: 0  # type: ignore[method-assign]
        report = type(
            "Report",
            (),
            {
                "generated_at": "2026-07-30T00:00:00+00:00",
                "inventory_source": "live",
                "all_evaluated": [],
                "guadao_candidates": [],
                "transfer_candidates": [],
                "missing_price_count": 0,
                "total_pool_types": 1,
                "item_outcomes": [
                    {
                        "name": "Revolution Case",
                        "marketHashName": "Revolution Case",
                        "status": "below_min_price",
                        "reason": "C5 补仓价 ¥0.50 低于扫描下限 ¥0.90",
                        "stage": "minimum_price_filter",
                        "c5RebuyPrice": 0.50,
                        "steamListPrice": 2.01,
                        "requestSent": True,
                    }
                ],
            },
        )()
        scan_mock.return_value = report

        result = self.engine.run_guadao_scan_task()

        kwargs = scan_mock.call_args.kwargs
        self.assertEqual("guadao", kwargs["steam_request_source"])
        self.assertEqual(1, kwargs["steam_orderbook_max_workers"])
        self.assertFalse(kwargs["refresh_steam_accounts"])
        resolver = kwargs["steam_orderbook_price_resolver"]
        pricing = resolver(
            "Revolution Case",
            {
                "success": 1,
                "eCurrency": 23,
                "rgCompactSellOrders": [[100, 1], [200, 19]],
            },
        )
        self.assertIsNotNone(pricing)
        assert pricing is not None
        self.assertEqual(2.01, round(pricing.list_price, 2))
        self.assertEqual("below_min_price", result["scanRound"]["items"][0]["decision"])
        self.assertEqual(1, result["scanRound"]["outcomeCounts"]["below_min_price"])

    def test_settings_do_not_accept_global_trade_url(self) -> None:
        self.assertFalse(hasattr(self.engine.settings, "steam_trade_url"))

    def test_current_imported_account_trade_url_is_resolved_from_account_cookie(self) -> None:
        self.engine.account = Account(
            id="current",
            name="current",
            steam_id64=self.engine.steam_client.steam_id64,
        )
        self.engine.account_store = FakeAccountStore()

        trade_url = self.engine._resolve_trade_url()

        self.assertEqual(self.engine.steam_client.trade_url, trade_url)
        self.assertEqual(
            [("current", {"trade_url": self.engine.steam_client.trade_url})],
            self.engine.account_store.updates,
        )

    def test_mismatched_account_trade_url_is_rejected(self) -> None:
        self.engine._steam_trade_url = (
            "https://steamcommunity.com/tradeoffer/new/?partner=319711777&token=old"
        )
        self.engine.account = Account(
            id="current",
            name="current",
            steam_id64=self.engine.steam_client.steam_id64,
        )
        self.engine.account_store = FakeAccountStore()

        with patch("builtins.print"):
            trade_url = self.engine._resolve_trade_url()

        self.assertEqual(self.engine.steam_client.trade_url, trade_url)

    def test_transfer_sells_existing_base_asset_instead_of_new_buy(self) -> None:
        candidate = build_candidate()

        self.assertTrue(self.engine._execute_transfer_buy(candidate))
        self.assertEqual(1, len(self.engine.steam_client.buy_calls))
        self.assertEqual("listing-low", self.engine.steam_client.buy_calls[0]["listing_id"])

        buy_ops = self.db.list_pool_operations_by_type(OP_TRANSFER_BUY, limit=10)
        self.assertEqual(1, len(buy_ops))
        self.assertEqual("pending", buy_ops[0]["status"])
        self.assertIn('"sellAssetId": "asset-old"', buy_ops[0]["note"])

        listed = self.engine._execute_transfer_sells()
        self.assertEqual(1, listed)
        self.assertEqual(1, len(self.engine.c5_client.sale_create_calls))

        sale_call = self.engine.c5_client.sale_create_calls[0]
        sale_item = sale_call["items"][0]
        self.assertEqual("asset-old", sale_item["assetId"])
        self.assertEqual("token-old", sale_item["token"])
        self.assertEqual("style-old", sale_item["styleToken"])

        buy_ops = self.db.list_pool_operations_by_type(OP_TRANSFER_BUY, limit=10)
        sell_ops = self.db.list_pool_operations_by_type(OP_TRANSFER_SELL, limit=10)
        self.assertEqual("listed", buy_ops[0]["status"])
        self.assertEqual(1, len(sell_ops))
        self.assertEqual("listed", sell_ops[0]["status"])
        self.assertEqual("asset-old", sell_ops[0]["asset_id"])

    def test_transfer_returns_to_holding_only_after_replacement_is_tradable(self) -> None:
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_TRANSFER,
            operation_type=OP_TRANSFER_BUY,
            expected_price=25.0,
            asset_id="asset-new",
            note='{"boughtAssetId":"asset-new"}',
        )
        op = self.db.list_pool_operations_by_type(OP_TRANSFER_BUY, limit=10)[0]
        self.db.update_pool_operation(op["id"], status="sold")
        self.db.upsert_pool_item("Revolution Case", 1, status=POOL_STATUS_TRANSFER_SOLD)

        self.engine._inventory_items_by_asset_id["asset-new"] = {
            "assetId": "asset-new",
            "marketHashName": "Revolution Case",
            "steamId": self.engine.steam_client.steam_id64,
            "ifTradable": False,
            "tradableTime": None,
        }
        self.engine._refresh_transfer_holdings()
        row = self.db.list_pool_items(status=POOL_STATUS_TRANSFER_HOLDING)[0]
        self.assertEqual("Revolution Case", row["market_hash_name"])

        self.engine._inventory_items_by_asset_id["asset-new"]["ifTradable"] = True
        self.engine._refresh_transfer_holdings()
        row = self.db.list_pool_items(status=POOL_STATUS_HOLDING)[0]
        self.assertEqual("Revolution Case", row["market_hash_name"])

    def test_guadao_listing_does_not_pre_rebuy_even_if_config_enabled(self) -> None:
        self.engine.config.dry_run = True
        self.engine.config.force_refresh_before_execution = False
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type(
            "Report",
            (),
            {"guadao_candidates": [build_guadao_candidate()]},
        )()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        rebuy_ops = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual([], rebuy_ops)

    def test_guadao_refresh_listing_missing_without_receipt_stays_pending(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        sold = self.engine._refresh_listings()

        self.assertEqual(0, sold)
        row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", row["market_hash_name"])
        sell_op = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?",
            (op_id,),
        ).fetchone()
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        self.assertEqual("listing_missing_unverified", json.loads(sell_op["note"])["confirmationStatus"])
        self.assertEqual("listing_pending", self.db.get_asset("asset-old")["status"])
        rebuy_ops = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual([], rebuy_ops)

    def _add_inventory_return_candidate(
        self,
        *,
        market_hash_name: str = "Revolution Case",
        asset_id: str = "asset-old",
        listing_id: str = "listing-1",
        observations: int = 1,
    ) -> int:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.db.upsert_pool_item(market_hash_name, 1, status=POOL_STATUS_LISTING_PENDING)
        if self.db.get_asset(asset_id) is None:
            self.db.upsert_inventory_assets(
                [
                    {
                        "assetId": asset_id,
                        "marketHashName": market_hash_name,
                        "steamId": self.engine.steam_client.steam_id64,
                        "ifTradable": True,
                    }
                ]
            )
        self.db.set_asset_status(asset_id, "listing_pending")
        operation_id = self.db.add_pool_operation(
            market_hash_name=market_hash_name,
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id=asset_id,
            note=json.dumps(
                {
                    "listingId": listing_id,
                    "steamId64": self.engine.steam_client.steam_id64,
                    "activeVerifiedAt": "2026-07-25T15:17:41+00:00",
                    "listingPendingAt": "2026-07-27T05:28:05+00:00",
                    "confirmationStatus": "listing_missing_unverified",
                    "activeListingMissingObservationCount": observations,
                    "rebuyPrice": 20.0,
                    "steamListPrice": 25.0,
                }
            ),
        )
        self.db.update_pool_operation(operation_id, status=POOL_STATUS_LISTING_PENDING)
        return operation_id

    def test_listing_missing_partial_history_then_inventory_releases_exact_returned_asset(self) -> None:
        operation_id = self._add_inventory_return_candidate()
        inventory_calls: list[list[str]] = []
        task_key = f"sale-evidence:{operation_id}"
        self.db.upsert_scheduled_task(
            task_key,
            source="guadao",
            task_type="steam_sale_evidence",
            next_attempt_at="2026-07-27T06:30:00+00:00",
            operation_id=operation_id,
            status="waiting",
        )

        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: SteamSaleReceiptLookupResult({}, False, max_pages)
        )

        def find_inventory(asset_ids: list[str]) -> SteamInventoryAssetLookupResult:
            inventory_calls.append(list(asset_ids))
            return SteamInventoryAssetLookupResult(frozenset({"asset-old"}), False, 1)

        self.engine.steam_client.find_inventory_asset_ids = find_inventory  # type: ignore[attr-defined]

        self.assertEqual(0, self.engine._refresh_listings())
        self.assertEqual([["asset-old"]], inventory_calls)
        operation = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        assert operation is not None
        note = json.loads(operation["note"])
        self.assertEqual("canceled", operation["status"])
        self.assertEqual("listing_missing_unsold_asset_returned", note["confirmationStatus"])
        self.assertEqual("official_steam_inventory_same_asset", note["terminalEvidence"])
        self.assertEqual("asset-old", note["steamInventoryReturnCheckAssetId"])
        self.assertTrue(note["releasedForRelisting"])
        self.assertEqual("available", self.db.get_asset("asset-old")["status"])
        self.assertEqual(POOL_STATUS_HOLDING, self.db.get_pool_status_map()["Revolution Case"])
        self.assertIsNone(self.db.get_scheduled_task(task_key))
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))

    def test_listing_missing_inventory_release_reconciles_pool_to_remaining_listed_case(
        self,
    ) -> None:
        self.engine.config.case_max_open_guadao_count = 150
        operation_id = self._add_inventory_return_candidate()
        remaining_operation_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-still-listed",
            note=json.dumps(
                {
                    "listingId": "listing-still-active",
                    "steamListPrice": 25.0,
                    "activeVerifiedAt": "2026-07-25T15:18:00+00:00",
                }
            ),
        )
        self.db.update_pool_operation(remaining_operation_id, status="listed")
        self.engine.steam_client.active_listing_assets["asset-still-listed"] = (
            "listing-still-active"
        )
        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: SteamSaleReceiptLookupResult({}, False, max_pages)
        )
        self.engine.steam_client.find_inventory_asset_ids = (  # type: ignore[attr-defined]
            lambda _asset_ids: SteamInventoryAssetLookupResult(
                frozenset({"asset-old"}),
                False,
                1,
            )
        )

        self.assertEqual(0, self.engine._refresh_listings())

        released_operation = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        assert released_operation is not None
        self.assertEqual("canceled", released_operation["status"])
        self.assertEqual(
            POOL_STATUS_LISTED,
            self.db.get_pool_status_map()["Revolution Case"],
        )
        self.assertFalse(self.engine._has_open_guadao_cycle())

    def test_listing_missing_history_receipt_never_queries_official_inventory(self) -> None:
        operation_id = self._add_inventory_return_candidate()
        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: SteamSaleReceiptLookupResult(
                {
                    str(operation_id): {
                        "listingId": "listing-1",
                        "purchaseId": "receipt-1",
                        "timeSold": "2026-07-27T06:16:11+00:00",
                        "receivedAmount": 21.55,
                    }
                },
                False,
                max_pages,
            )
        )
        self.engine.steam_client.find_inventory_asset_ids = (  # type: ignore[attr-defined]
            lambda _asset_ids: (_ for _ in ()).throw(
                AssertionError("official inventory must not run after a Steam sale receipt")
            )
        )

        self.assertEqual(1, self.engine._refresh_listings())
        operation = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        self.assertEqual("sold", operation["status"])
        self.assertEqual("sold", self.db.get_asset("asset-old")["status"])
        self.assertEqual(1, len(self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)))

    def test_listing_missing_history_failure_never_queries_official_inventory(self) -> None:
        operation_id = self._add_inventory_return_candidate()
        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: (_ for _ in ()).throw(
                SteamMarketError(f"history HTTP 429 after {max_pages} pages")
            )
        )
        self.engine.steam_client.find_inventory_asset_ids = (  # type: ignore[attr-defined]
            lambda _asset_ids: (_ for _ in ()).throw(
                AssertionError("failed history cannot authorize an inventory request")
            )
        )

        self.assertEqual(0, self.engine._refresh_listings())
        operation = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        self.assertEqual(POOL_STATUS_LISTING_PENDING, operation["status"])
        self.assertEqual("listing_pending", self.db.get_asset("asset-old")["status"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))

    def test_listing_missing_different_or_incomplete_inventory_asset_stays_pending(self) -> None:
        operation_id = self._add_inventory_return_candidate()
        inventory_calls = 0
        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: SteamSaleReceiptLookupResult({}, True, max_pages)
        )

        def find_inventory(_asset_ids: list[str]) -> SteamInventoryAssetLookupResult:
            nonlocal inventory_calls
            inventory_calls += 1
            return SteamInventoryAssetLookupResult(frozenset({"different-asset"}), False, 1)

        self.engine.steam_client.find_inventory_asset_ids = find_inventory  # type: ignore[attr-defined]

        self.assertEqual(0, self.engine._refresh_listings())
        self.assertEqual(0, self.engine._refresh_listings())
        self.assertEqual(1, inventory_calls)
        operation = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        assert operation is not None
        self.assertEqual(POOL_STATUS_LISTING_PENDING, operation["status"])
        self.assertEqual(
            "not_found_incomplete",
            json.loads(operation["note"])["steamInventoryReturnCheckStatus"],
        )
        self.assertEqual("listing_pending", self.db.get_asset("asset-old")["status"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))

    def test_listing_missing_pending_market_row_blocks_inventory_fallback(self) -> None:
        operation_id = self._add_inventory_return_candidate()
        self.engine.steam_client.pending_listing_assets["asset-old"] = "pending-listing-1"
        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: SteamSaleReceiptLookupResult({}, True, max_pages)
        )
        self.engine.steam_client.find_inventory_asset_ids = (  # type: ignore[attr-defined]
            lambda _asset_ids: (_ for _ in ()).throw(
                AssertionError("a pending Steam market row must be handled before inventory fallback")
            )
        )

        self.assertEqual(0, self.engine._refresh_listings())
        operation = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        assert operation is not None
        self.assertEqual(POOL_STATUS_LISTING_PENDING, operation["status"])
        self.assertEqual(
            "market_pending_visible",
            json.loads(operation["note"])["steamInventoryReturnPrecondition"],
        )

    def test_listing_missing_inventory_fallback_batches_same_account_assets_once(self) -> None:
        first_id = self._add_inventory_return_candidate(
            market_hash_name="Inventory Batch Case A",
            asset_id="asset-inventory-batch-a",
            listing_id="listing-inventory-batch-a",
        )
        second_id = self._add_inventory_return_candidate(
            market_hash_name="Inventory Batch Case B",
            asset_id="asset-inventory-batch-b",
            listing_id="listing-inventory-batch-b",
        )
        inventory_calls: list[list[str]] = []
        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: SteamSaleReceiptLookupResult({}, True, max_pages)
        )

        def find_inventory(asset_ids: list[str]) -> SteamInventoryAssetLookupResult:
            inventory_calls.append(sorted(asset_ids))
            return SteamInventoryAssetLookupResult(frozenset(asset_ids), False, 1)

        self.engine.steam_client.find_inventory_asset_ids = find_inventory  # type: ignore[attr-defined]

        self.assertEqual(0, self.engine._refresh_listings())
        self.assertEqual(
            [["asset-inventory-batch-a", "asset-inventory-batch-b"]],
            inventory_calls,
        )
        rows = self.db.conn.execute(
            "SELECT id, status FROM pool_operations WHERE id IN (?, ?) ORDER BY id",
            (first_id, second_id),
        ).fetchall()
        self.assertEqual(["canceled", "canceled"], [row["status"] for row in rows])

    def test_listing_missing_unverified_advances_same_operation_when_history_arrives(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        self.assertEqual(0, self.engine._refresh_listings())
        self.engine.steam_client.sale_receipts["listing-1"] = {
            "purchaseId": "purchase-delayed",
            "timeSold": "2026-07-16T01:02:03+00:00",
            "receivedAmount": 21.73,
        }

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(1, updated)
        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("sold", op["status"])
        self.assertEqual("sold", self.db.get_asset("asset-old")["status"])
        rebuys = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual(1, len(rebuys))
        self.assertEqual(op_id, json.loads(rebuys[0]["note"])["sourceSellOperationId"])

    def test_listing_missing_unverified_returns_to_listed_when_remote_reappears(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        self.assertEqual(0, self.engine._refresh_listings())
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(1, updated)
        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual("listed", self.db.get_asset("asset-old")["status"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))

    def test_guadao_refresh_listing_falls_back_to_asset_sale_receipt(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.engine.steam_client.sale_receipts_by_asset["asset-old"] = {
            "listingId": "listing-1",
            "purchaseId": "purchase-1",
            "timeSold": 1782755467,
            "receivedAmount": 2.52,
            "receivedCurrencyId": "2023",
        }
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=2.88,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":1.69,"steamListPrice":2.88,"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        sold = self.engine._refresh_listings()

        self.assertEqual(1, sold)
        op = self.db.conn.execute("SELECT * FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        note = json.loads(op["note"])
        self.assertEqual("2026-06-29T17:51:07+00:00", note["steamSoldAt"])
        self.assertEqual("purchase-1", note["steamPurchaseId"])
        self.assertEqual("steam_history", note["steamSellerNetPriceSource"])
        self.assertAlmostEqual(2.52, float(note["steamSellerNetPrice"]))

    def test_stale_guadao_listed_active_listing_defers_when_c5_price_unavailable(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine._last_inventory_payload = {}
        self.engine.c5_client.price_batch_should_fail = True
        (self.db_path.parent / "c5_inventory_all_cache.json").write_text(
            json.dumps(
                {
                    "cachedAt": datetime.now(timezone.utc).isoformat(),
                    "list": [
                        {
                            "assetId": "cached-c5-asset",
                            "marketHashName": "Revolution Case",
                            "name": "革命武器箱",
                            "price": None,
                            "ifTradable": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        self.db.set_asset_status("asset-old", "listed")
        stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note=(
                '{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,'
                '"guadaoMaxListingRatioAtOpen":0.69,"steamNetFactorAtOpen":0.869,'
                '"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}'
            ),
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.conn.execute("UPDATE pool_operations SET created_at = ? WHERE id = ?", (stale_created_at, op_id))
        self.db.conn.commit()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        sold = self.engine._refresh_listings()

        self.assertEqual(0, sold)
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        op = self.db.conn.execute("SELECT * FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        self.assertEqual("listed", op["status"])
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("C5 price_batch unavailable", note["staleListedCleanupReason"])
        self.assertEqual("listed", self.db.get_asset("asset-old")["status"])
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTED)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))

    def test_stale_guadao_listing_at_floor_within_tolerance_is_kept_and_rechecked_daily(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.engine.config.stale_listed_recheck_hours = 24.0
        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine._last_inventory_payload = {
            "list": [
                {
                    "marketHashName": "Revolution Case",
                    "name": "革命武器箱",
                    "price": None,
                    "ifTradable": False,
                }
            ]
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 15.2, "count": 10}
        }
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        self.db.set_asset_status("asset-old", "listed")
        stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note=(
                '{"listingId":"listing-1","rebuyPrice":15.2,"steamListPrice":25.0,'
                '"guadaoMaxListingRatioAtOpen":0.69,"steamNetFactorAtOpen":0.869,'
                '"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}'
            ),
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.conn.execute("UPDATE pool_operations SET created_at = ? WHERE id = ?", (stale_created_at, op_id))
        self.db.conn.commit()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        first_sold = self.engine._refresh_listings()
        second_sold = self.engine._refresh_listings()

        self.assertEqual(0, first_sold)
        self.assertEqual(0, second_sold)
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(1, len(self.engine.steam_client.orderbook_calls))
        self.assertEqual(1, len(self.engine.c5_client.price_batch_calls))
        op = self.db.conn.execute("SELECT * FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        self.assertEqual("listed", op["status"])
        note = json.loads(op["note"])
        self.assertEqual("kept_at_market_floor", note["staleListedCleanupStatus"])
        self.assertTrue(note["staleListedAtMarketFloor"])
        self.assertAlmostEqual(0.705, float(note["staleListedAllowedMaxRatio"]), places=6)
        self.assertLess(float(note["staleListedCurrentRatio"]), 0.705)
        self.assertIn("staleListedNextCheckAt", note)
        self.assertEqual("listed", self.db.get_asset("asset-old")["status"])

    def test_stale_guadao_listing_with_missing_c5_price_retries_after_ten_minutes(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.engine.config.stale_listed_recheck_hours = 24.0
        self.engine._last_inventory_payload = {"list": []}
        self.engine.c5_client.price_batch_payload = {}
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        self.db.set_asset_status("asset-old", "listed")
        stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note=(
                '{"listingId":"listing-1","steamListPrice":25.0,'
                '"guadaoMaxListingRatioAtOpen":0.69,"steamNetFactorAtOpen":0.869,'
                '"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}'
            ),
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.conn.execute(
            "UPDATE pool_operations SET created_at = ? WHERE id = ?",
            (stale_created_at, op_id),
        )
        self.db.conn.commit()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"
        before = datetime.now(timezone.utc)

        sold = self.engine._refresh_listings()

        self.assertEqual(0, sold)
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?",
            (op_id,),
        ).fetchone()
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertEqual(
            "current C5 market price is unavailable",
            note["staleListedCleanupReason"],
        )
        next_check_at = datetime.fromisoformat(note["staleListedNextCheckAt"])
        self.assertGreaterEqual(next_check_at, before + timedelta(minutes=9, seconds=55))
        self.assertLessEqual(next_check_at, before + timedelta(minutes=10, seconds=5))

    def test_legacy_deferred_stale_listing_does_not_keep_old_daily_delay(self) -> None:
        self.engine.config.stale_listed_recheck_hours = 24.0
        now = datetime.now(timezone.utc)
        note = {
            "staleListedCleanupStatus": "check_deferred",
            "staleListedCheckedAt": (now - timedelta(minutes=11)).isoformat(),
            "staleListedNextCheckAt": (now + timedelta(hours=23, minutes=49)).isoformat(),
        }

        self.assertTrue(self.engine._stale_listed_recheck_due(note, now=now))

    def test_stale_guadao_listing_at_floor_over_tolerated_ratio_is_removed(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine._last_inventory_payload = {
            "list": [
                {
                    "marketHashName": "Revolution Case",
                    "name": "革命武器箱",
                    "price": 16.0,
                    "ifTradable": True,
                }
            ]
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 16.0, "count": 10}
        }
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        self.db.set_asset_status("asset-old", "listed")
        stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note=(
                '{"listingId":"listing-1","rebuyPrice":16.0,"steamListPrice":25.0,'
                '"guadaoMaxListingRatioAtOpen":0.69,"steamNetFactorAtOpen":0.869,'
                '"guadaoRatioRuleSource":"special_case",'
                '"guadaoRatioRuleId":"special-revolution","guadaoRatioRuleVersion":3}'
            ),
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.conn.execute("UPDATE pool_operations SET created_at = ? WHERE id = ?", (stale_created_at, op_id))
        self.db.conn.commit()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        sold = self.engine._refresh_listings()

        self.assertEqual(0, sold)
        self.assertEqual(["listing-1"], self.engine.steam_client.removed_listing_ids)
        op = self.db.conn.execute("SELECT * FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        self.assertEqual("canceled", op["status"])
        note = json.loads(op["note"])
        self.assertEqual("stale listing ratio exceeds tolerated maximum", note["staleListedRemoveReason"])
        self.assertGreater(float(note["staleListedCurrentRatio"]), 0.705)

    def test_stale_guadao_listed_missing_remote_with_sale_receipt_advances_to_rebuy(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 999 * 60  # type: ignore[method-assign]
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        self.db.set_asset_status("asset-old", "listed")
        stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}',
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.conn.execute("UPDATE pool_operations SET created_at = ? WHERE id = ?", (stale_created_at, op_id))
        self.db.conn.commit()
        self.engine.steam_client.sale_receipts["listing-1"] = {
            "receivedAmount": 21.55,
            "purchaseId": "purchase-1",
        }

        sold = self.engine._refresh_listings()

        self.assertEqual(1, sold)
        op = self.db.conn.execute("SELECT * FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        self.assertEqual("sold", op["status"])
        self.assertEqual("sold", self.db.get_asset("asset-old")["status"])
        pool_row = self.db.list_pool_items(status=POOL_STATUS_PENDING_REBUY)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        rebuy_ops = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual(1, len(rebuy_ops))

    def test_stale_guadao_listed_missing_remote_without_receipt_requires_manual_review(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        self.db.set_asset_status("asset-old", "listed")
        stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}',
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.conn.execute("UPDATE pool_operations SET created_at = ? WHERE id = ?", (stale_created_at, op_id))
        self.db.conn.commit()

        sold = self.engine._refresh_listings()

        self.assertEqual(0, sold)
        op = self.db.conn.execute("SELECT * FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        self.assertEqual("manual_required", op["status"])
        note = json.loads(op["note"])
        self.assertEqual("manual_required", note["staleListedCleanupStatus"])
        self.assertIn("missing active Steam listing and sale receipt", note["manualReviewReason"])
        self.assertEqual("listed", self.db.get_asset("asset-old")["status"])
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTED)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))

    def test_stale_sync_manual_review_does_not_overwrite_newer_terminal_state(self) -> None:
        op_id = self._add_stale_recheck_operation()
        row = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        latest_note = json.loads(row["note"] or "{}")
        latest_note["steamSaleReceipt"] = {
            "listingId": "listing-1",
            "receivedAmount": 21.73,
        }
        self.db.update_pool_operation(
            op_id,
            status="sold",
            note=json.dumps(latest_note, ensure_ascii=False),
        )
        self.db.set_asset_status("asset-old", "sold")

        changed = self.engine._mark_stale_listed_manual_required(
            row,
            json.loads(row["note"] or "{}"),
            reason="stale listed operation missing active Steam listing and sale receipt",
        )

        final = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertFalse(changed)
        self.assertEqual("sold", final["status"])
        self.assertIn("steamSaleReceipt", json.loads(final["note"] or "{}"))
        self.assertEqual("sold", self.db.get_asset("asset-old")["status"])

    def test_stale_manual_required_listing_keeps_rechecking_for_delayed_history(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        self.db.set_asset_status("asset-old", "listed")
        stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}',
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.conn.execute(
            "UPDATE pool_operations SET created_at = ? WHERE id = ?",
            (stale_created_at, op_id),
        )
        self.db.conn.commit()

        self.assertEqual(0, self.engine._refresh_listings())
        self.engine.steam_client.sale_receipts["listing-1"] = {
            "purchaseId": "purchase-late-after-manual",
            "timeSold": "2026-07-16T02:03:04+00:00",
            "receivedAmount": 21.73,
        }

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(1, updated)
        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("sold", op["status"])
        self.assertEqual(1, len(self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)))

    def test_guadao_refresh_listing_carries_frozen_ratio_to_rebuy_op(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.engine.steam_client.sale_receipts["listing-1"] = {"receivedAmount": 21.73}
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note=(
                '{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,'
                '"activeVerifiedAt":"2026-01-01T00:00:00+00:00",'
                '"listingRatioAtOpen":0.62,"maxRebuyRatioAtOpen":0.62,'
                '"guadaoMaxListingRatioAtOpen":0.69,"steamNetFactorAtOpen":0.869,'
                '"guadaoRatioRuleSource":"special_case",'
                '"guadaoRatioRuleId":"special-revolution",'
                '"guadaoRatioRuleVersion":3}'
            ),
        )
        self.db.update_pool_operation(op_id, status="listed")

        sold = self.engine._refresh_listings()

        self.assertEqual(1, sold)
        rebuy_ops = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual(1, len(rebuy_ops))
        note = json.loads(rebuy_ops[0]["note"])
        self.assertAlmostEqual(0.62, float(note["listingRatioAtOpen"]), places=6)
        self.assertAlmostEqual(0.62, float(note["maxRebuyRatioAtOpen"]), places=6)
        self.assertAlmostEqual(0.69, float(note["guadaoMaxListingRatioAtOpen"]), places=6)
        self.assertEqual("special_case", note["guadaoRatioRuleSource"])
        self.assertEqual("special-revolution", note["guadaoRatioRuleId"])
        self.assertEqual(3, note["guadaoRatioRuleVersion"])

    def test_guadao_refresh_listing_prints_net_amount(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.engine.steam_client.sale_receipts["listing-1"] = {"receivedAmount": 21.73}
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,"activeVerifiedAt":"2026-01-01T00:00:00+00:00"}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        with patch("builtins.print") as print_mock:
            sold = self.engine._refresh_listings()

        self.assertEqual(1, sold)
        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("Steam售价 CNY 25", printed)
        self.assertIn("税后到手 CNY 21.73", printed)

    def test_guadao_sold_transition_emits_structured_amounts(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=3.0,
            asset_id="asset-old",
            note=(
                '{"listingId":"listing-1","rebuyPrice":1.6,"steamListPrice":3.0,'
                '"steamAccountId":"account-a","steamId64":"76561198000000000"}'
            ),
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.engine.steam_client.sale_receipts["listing-1"] = {
            "listingId": "listing-1",
            "purchaseId": "purchase-1",
            "timeSold": 1782755467,
            "receivedAmount": 2.52,
        }
        logger = FakeGuadaoLogger()

        with patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=logger,
        ):
            sold = self.engine._refresh_listings()

        self.assertEqual(1, sold)
        event = next(row for row in logger.events if row["operation"] == "steam_listing_sold")
        context = event["safe_context"]
        assert isinstance(context, dict)
        self.assertEqual(3.0, context["steamSalePrice"])
        self.assertEqual(2.52, context["steamNetAmount"])
        self.assertEqual("steam_history", context["steamNetAmountSource"])
        self.assertEqual("purchase-1", context["steamPurchaseId"])

    def test_listing_prints_expected_listing_ratio(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.6543,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        with patch("builtins.print") as print_mock:
            listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("65.43%", printed)

    def test_guadao_listing_orders_candidates_by_live_listing_ratio(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.engine.config.max_list_per_cycle = 2
        kilowatt_asset = {
            "assetId": "asset-kilowatt",
            "marketHashName": "Kilowatt Case",
            "steamId": self.engine.steam_client.steam_id64,
            "ifTradable": True,
            "tradableTime": None,
            "token": "token-kilowatt",
            "styleToken": "style-kilowatt",
            "price": 10.0,
        }
        self.db.upsert_pool_item("Kilowatt Case", 1, status=POOL_STATUS_HOLDING)
        self.db.upsert_inventory_assets([kilowatt_asset])

        decisions = {
            "Revolution Case": ListingDecision(25.0, 0.68, 0.07, None),
            "Kilowatt Case": ListingDecision(25.0, 0.62, 0.07, None),
        }

        def decide(candidate: StrategyCandidate) -> ListingDecision:
            return decisions[candidate.market_hash_name]

        self.engine._decide_listing = decide  # type: ignore[method-assign]
        report = type(
            "Report",
            (),
            {
                "guadao_candidates": [
                    build_guadao_candidate_for("Revolution Case"),
                    build_guadao_candidate_for("Kilowatt Case", rebuy_price=10.0),
                ]
            },
        )()

        listed = self.engine._execute_guadao_listings(
            report,
            {
                "Revolution Case": POOL_STATUS_HOLDING,
                "Kilowatt Case": POOL_STATUS_HOLDING,
            },
        )

        self.assertEqual(2, listed)
        self.assertEqual("asset-kilowatt", self.engine.steam_client.sell_calls[0]["asset_id"])
        self.assertEqual("asset-old", self.engine.steam_client.sell_calls[1]["asset_id"])

    def test_guadao_scan_defers_same_account_confirmations_to_one_batched_sync(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.config.max_list_per_cycle = 2
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.db.upsert_inventory_assets(
            [
                {
                    "assetId": "asset-new",
                    "marketHashName": "Revolution Case",
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                    "tradableTime": None,
                    "token": "token-new",
                    "styleToken": "style-new",
                    "price": 20.0,
                }
            ]
        )
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.62,
            transfer_real_ratio=0.07,
            pricing=None,
        )

        def submit_listing(**kwargs: object) -> dict[str, object]:
            asset_id = str(kwargs["asset_id"])
            listing_id = f"listing-{asset_id}"
            self.engine.steam_client.sell_calls.append(dict(kwargs))
            self.engine.steam_client.pending_listing_assets[asset_id] = listing_id
            return {"listingid": listing_id}

        def confirm_batch(
            *,
            asset_ids: object,
            listing_ids: object | None = None,
            pending_listings: object | None = None,
        ) -> int:
            assets = [str(value) for value in asset_ids]
            self.engine.steam_client.confirm_calls += 1
            self.engine.steam_client.confirm_asset_calls.append(
                {"asset_ids": assets, "listing_ids": listing_ids}
            )
            for asset_id in assets:
                listing_id = self.engine.steam_client.pending_listing_assets.pop(asset_id)
                self.engine.steam_client.active_listing_assets[asset_id] = listing_id
            return len(assets)

        self.engine._sell_item_with_retry = submit_listing  # type: ignore[method-assign]
        self.engine.steam_client.confirm_listing_assets = confirm_batch  # type: ignore[method-assign]
        report = type(
            "Report",
            (),
            {"guadao_candidates": [build_guadao_candidate()]},
        )()

        listed = self.engine._execute_guadao_listings(
            report,
            {"Revolution Case": POOL_STATUS_HOLDING},
        )

        self.assertEqual(2, listed)
        self.assertEqual(2, len(self.engine.steam_client.sell_calls))
        self.assertEqual(1, self.engine.steam_client.confirm_calls)
        self.assertEqual(
            {"asset-new", "asset-old"},
            set(self.engine.steam_client.confirm_asset_calls[0]["asset_ids"]),
        )
        statuses = {
            str(row["status"])
            for row in self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)
        }
        self.assertEqual({"listed"}, statuses)

    def test_guadao_listing_uses_recent_sell_speed_as_tie_breaker(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.engine.config.max_list_per_cycle = 2
        kilowatt_asset = {
            "assetId": "asset-kilowatt",
            "marketHashName": "Kilowatt Case",
            "steamId": self.engine.steam_client.steam_id64,
            "ifTradable": True,
            "tradableTime": None,
            "token": "token-kilowatt",
            "styleToken": "style-kilowatt",
            "price": 10.0,
        }
        self.db.upsert_pool_item("Kilowatt Case", 1, status=POOL_STATUS_HOLDING)
        self.db.upsert_inventory_assets([kilowatt_asset])
        sold_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-sold",
            note="{}",
        )
        self.db.update_pool_operation(sold_id, status="sold")

        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.62,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type(
            "Report",
            (),
            {
                "guadao_candidates": [
                    build_guadao_candidate_for("Revolution Case"),
                    build_guadao_candidate_for("Kilowatt Case", rebuy_price=10.0),
                ]
            },
        )()

        listed = self.engine._execute_guadao_listings(
            report,
            {
                "Revolution Case": POOL_STATUS_HOLDING,
                "Kilowatt Case": POOL_STATUS_HOLDING,
            },
        )

        self.assertEqual(2, listed)
        self.assertEqual("asset-kilowatt", self.engine.steam_client.sell_calls[0]["asset_id"])

    def test_guadao_listing_freezes_open_ratio_in_sell_note(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.62,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        note = json.loads(sell_op["note"])
        self.assertAlmostEqual(0.62, float(note["listingRatioAtOpen"]), places=6)
        self.assertAlmostEqual(0.62, float(note["maxRebuyRatioAtOpen"]), places=6)
        self.assertAlmostEqual(0.69, float(note["guadaoMaxListingRatioAtOpen"]), places=6)
        self.assertEqual("global", note["guadaoRatioRuleSource"])
        self.assertIsNone(note["guadaoRatioRuleId"])
        self.assertIsNone(note["guadaoRatioRuleVersion"])

    def test_new_action_guard_stops_before_real_sellitem(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine._new_action_guard = lambda: False
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.62,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(
            report,
            {"Revolution Case": POOL_STATUS_HOLDING},
        )

        self.assertEqual(0, listed)
        self.assertEqual([], self.engine.steam_client.sell_calls)
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10))
        self.assertEqual("new_action_guard_blocked", self.engine._stop_reason)

    def test_new_action_guard_rechecks_after_scheduler_queue_before_sellitem_http(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        guard_results = iter((True, False))
        self.engine._new_action_guard = lambda: next(guard_results)
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.62,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        http_sent: list[bool] = []

        def queued_sell_item(**kwargs: object) -> dict[str, object]:
            execution_guard = kwargs.get("execution_guard")
            assert callable(execution_guard)
            if not execution_guard():
                raise SteamRequestGuardRejected("runtime disabled before HTTP callback")
            http_sent.append(True)
            return {"listingid": "listing-1"}

        self.engine.steam_client.sell_item = queued_sell_item  # type: ignore[method-assign]
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(
            report,
            {"Revolution Case": POOL_STATUS_HOLDING},
        )

        self.assertEqual(0, listed)
        self.assertEqual([], http_sent)
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10))
        self.assertEqual("new_action_guard_blocked", self.engine._stop_reason)

    def test_new_listing_asset_selection_excludes_invalid_cookie_account(self) -> None:
        invalid = Account(
            id="invalid-account",
            name="invalid",
            steam_id64=self.engine.steam_client.steam_id64,
        )
        valid = Account(id="valid-account", name="valid", steam_id64="222")
        self.engine.account_store = FakeAccountStore([invalid, valid])
        self.db.upsert_inventory_assets(
            [
                {
                    "assetId": "asset-valid-cookie",
                    "marketHashName": "Revolution Case",
                    "steamId": "222",
                    "ifTradable": True,
                    "tradableTime": None,
                    "price": 20.0,
                }
            ]
        )
        self.db.upsert_steam_cookie_health(
            invalid.id,
            status="invalid",
            account_name=invalid.name,
            steam_id=invalid.steam_id64,
        )
        self.db.upsert_steam_cookie_health(
            valid.id,
            status="valid",
            account_name=valid.name,
            steam_id=valid.steam_id64,
        )

        target = self.engine._find_guadao_asset_target(build_guadao_candidate())

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual("asset-valid-cookie", target.asset_id)
        self.assertEqual(valid.id, target.account.id if target.account else None)

    def test_guadao_listing_freezes_matching_special_case_ratio_rule(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.engine.config.guadao_special_ratio_rules = [
            {
                "ruleId": "special-revolution",
                "marketHashName": "Revolution Case",
                "maxListingRatio": 0.75,
                "rebuyReferenceFloor": 7.60,
                "version": 3,
                "enabled": True,
            }
        ]
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.75,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(
            report,
            {"Revolution Case": POOL_STATUS_HOLDING},
        )

        self.assertEqual(1, listed)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        note = json.loads(sell_op["note"])
        self.assertEqual("special_case", note["guadaoRatioRuleSource"])
        self.assertEqual("special-revolution", note["guadaoRatioRuleId"])
        self.assertEqual(3, note["guadaoRatioRuleVersion"])
        self.assertEqual(7.60, note["rebuyReferenceFloorAtOpen"])
        self.assertAlmostEqual(0.75, float(note["guadaoMaxListingRatioAtOpen"]), places=6)
        self.assertAlmostEqual(0.75, float(note["maxRebuyRatioAtOpen"]), places=6)

    def test_guadao_listing_freezes_stricter_special_case_ratio_rule(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.engine.config.guadao_special_ratio_rules = [
            {
                "ruleId": "special-revolution-strict",
                "marketHashName": "Revolution Case",
                "maxListingRatio": 0.68,
                "version": 2,
                "enabled": True,
            }
        ]
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.68,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(
            report,
            {"Revolution Case": POOL_STATUS_HOLDING},
        )

        self.assertEqual(1, listed)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        note = json.loads(sell_op["note"])
        self.assertEqual("special_case", note["guadaoRatioRuleSource"])
        self.assertEqual("special-revolution-strict", note["guadaoRatioRuleId"])
        self.assertEqual(2, note["guadaoRatioRuleVersion"])
        self.assertAlmostEqual(0.68, float(note["guadaoMaxListingRatioAtOpen"]), places=6)
        self.assertAlmostEqual(0.68, float(note["maxRebuyRatioAtOpen"]), places=6)

    def test_case_pending_rebuy_does_not_block_new_listing(self) -> None:
        self.engine.config.dry_run = True
        self.engine.config.force_refresh_before_execution = False
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_PENDING_REBUY})

        self.assertEqual(1, listed)

    def test_listed_sell_operation_blocks_new_listing_even_when_pool_status_holding(self) -> None:
        self.engine.config.dry_run = True
        self.engine.config.force_refresh_before_execution = False
        market_hash_name = "AK-47 | Redline (Field-Tested)"
        self.db.upsert_pool_item(market_hash_name, 1, status=POOL_STATUS_HOLDING)
        op_id = self.db.add_pool_operation(
            market_hash_name=market_hash_name,
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        self.assertTrue(self.engine._has_open_guadao_cycle())

    def test_pending_rebuy_operation_blocks_new_listing_even_when_pool_status_holding(self) -> None:
        self.engine.config.dry_run = True
        self.engine.config.force_refresh_before_execution = False
        market_hash_name = "AK-47 | Redline (Field-Tested)"
        self.db.upsert_pool_item(market_hash_name, 1, status=POOL_STATUS_HOLDING)
        self.db.add_pool_operation(
            market_hash_name=market_hash_name,
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=20.0,
            note='{"sourceSellOperationId": 1, "steamListPrice": 25.0}',
        )

        self.assertTrue(self.engine._has_open_guadao_cycle())

    def test_case_listed_sell_operation_under_limit_does_not_block_new_listing(self) -> None:
        self.engine.config.case_max_open_guadao_count = 100
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        self.assertFalse(self.engine._has_open_guadao_cycle())

    def test_case_pending_rebuy_operation_under_limit_does_not_block_new_listing(self) -> None:
        self.engine.config.case_max_open_guadao_count = 100
        self.db.set_pool_status("Revolution Case", POOL_STATUS_PENDING_REBUY)
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=20.0,
            note='{"sourceSellOperationId": 1, "steamListPrice": 25.0}',
        )

        self.assertFalse(self.engine._has_open_guadao_cycle())

    def test_case_pending_rebuy_does_not_consume_listing_capacity_at_limit(self) -> None:
        self.engine.config.case_max_open_guadao_count = 1
        self.db.set_pool_status("Revolution Case", POOL_STATUS_PENDING_REBUY)
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=20.0,
            note='{"sourceSellOperationId": 1, "steamListPrice": 25.0}',
        )

        self.assertEqual(0, self.engine._open_case_guadao_count())
        self.assertFalse(self.engine._has_open_guadao_cycle())

    def _add_active_case_listings(self, count: int, *, age_hours: float) -> list[int]:
        created_at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
        operation_ids: list[int] = []
        for index in range(count):
            asset_id = f"asset-sequence-{index}"
            listing_id = f"listing-sequence-{index}"
            self.db.upsert_inventory_assets(
                [
                    {
                        "assetId": asset_id,
                        "marketHashName": "Revolution Case",
                        "steamId": self.engine.steam_client.steam_id64,
                        "ifTradable": True,
                        "tradableTime": None,
                        "token": f"token-{index}",
                        "styleToken": f"style-{index}",
                        "price": 20.0,
                    }
                ]
            )
            self.db.set_asset_status(asset_id, "listed")
            self.engine.steam_client.active_listing_assets[asset_id] = listing_id
            operation_id = self.db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy=STRATEGY_GUADAO,
                operation_type=OP_SELL_STEAM,
                expected_price=25.0,
                asset_id=asset_id,
                note=json.dumps(
                    {
                        "listingId": listing_id,
                        "steamListPrice": 25.0,
                        "steamId64": self.engine.steam_client.steam_id64,
                    }
                ),
            )
            self.db.update_pool_operation(operation_id, status="listed")
            self.db.conn.execute(
                "UPDATE pool_operations SET created_at = ? WHERE id = ?",
                (created_at, operation_id),
            )
            operation_ids.append(operation_id)
        self.db.conn.commit()
        return operation_ids

    def test_full_case_listing_capacity_under_three_hours_does_not_release(self) -> None:
        self.engine.config.case_max_open_guadao_count = 8
        self.engine.config.case_full_release_after_hours = 3.0
        self.engine.config.case_full_release_fraction = 0.125
        self._add_active_case_listings(8, age_hours=2.0)

        released = self.engine._release_full_case_listing_capacity()

        self.assertEqual(0, released)
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(8, self.engine._open_case_guadao_count())

    def _seed_case_full_observation(
        self,
        *,
        full_since_hours: float,
        last_observed_minutes: float = 1.0,
        process_session_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.engine._save_case_capacity_observation(
            {
                "isFull": True,
                "fullSince": (now - timedelta(hours=full_since_hours)).isoformat(),
                "lastObservedAt": (now - timedelta(minutes=last_observed_minutes)).isoformat(),
                "processSessionId": process_session_id or self.engine._process_session_id,
                "occupied": 8,
                "capacity": 8,
            }
        )

    def test_case_listing_capacity_timer_ignores_old_listing_age_on_first_observation(self) -> None:
        self.engine.config.case_max_open_guadao_count = 8
        self.engine.config.case_full_release_after_hours = 3.0
        self.engine.config.case_full_release_fraction = 0.125
        self._add_active_case_listings(8, age_hours=24.0)

        released = self.engine._release_full_case_listing_capacity()

        self.assertEqual(0, released)
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        state = self.engine._case_capacity_observation()
        self.assertTrue(state["isFull"])
        self.assertLess(
            (datetime.now(timezone.utc) - datetime.fromisoformat(state["fullSince"])).total_seconds(),
            10,
        )

    def test_full_case_listing_capacity_over_three_hours_randomly_releases_12_5_percent(self) -> None:
        self.engine.config.case_max_open_guadao_count = 8
        self.engine.config.case_full_release_after_hours = 3.0
        self.engine.config.case_full_release_fraction = 0.125
        operation_ids = self._add_active_case_listings(8, age_hours=4.0)
        self._seed_case_full_observation(full_since_hours=4.0)

        with patch(
            "cs2_assistant.services.executor_engine.random.sample",
            side_effect=lambda rows, count: list(rows)[-count:],
        ):
            released = self.engine._release_full_case_listing_capacity()

        self.assertEqual(1, released)
        self.assertEqual(["listing-sequence-7"], self.engine.steam_client.removed_listing_ids)
        released_row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?",
            (operation_ids[-1],),
        ).fetchone()
        self.assertEqual("canceled", released_row["status"])
        released_note = json.loads(released_row["note"])
        self.assertEqual("case_listing_capacity_full_random", released_note["sequenceReleaseReason"])
        self.assertEqual("available", self.db.get_asset("asset-sequence-7")["status"])
        self.assertEqual(7, self.engine._open_case_guadao_count())

    def test_case_listing_capacity_restart_breaks_previous_full_continuity(self) -> None:
        self.engine.config.case_max_open_guadao_count = 8
        self.engine.config.case_full_release_after_hours = 3.0
        self.engine.config.case_full_release_fraction = 0.125
        self._add_active_case_listings(8, age_hours=4.0)
        self._seed_case_full_observation(
            full_since_hours=4.0,
            process_session_id="previous-process",
        )

        released = self.engine._release_full_case_listing_capacity()

        self.assertEqual(0, released)
        state = self.engine._case_capacity_observation()
        self.assertEqual(self.engine._process_session_id, state["processSessionId"])
        self.assertLess(
            (datetime.now(timezone.utc) - datetime.fromisoformat(state["fullSince"])).total_seconds(),
            10,
        )

    def test_case_listing_capacity_observation_gap_breaks_full_continuity(self) -> None:
        self.engine.config.case_max_open_guadao_count = 8
        self.engine.config.case_full_release_after_hours = 3.0
        self.engine.config.case_full_release_fraction = 0.125
        self._add_active_case_listings(8, age_hours=4.0)
        self._seed_case_full_observation(
            full_since_hours=4.0,
            last_observed_minutes=60.0,
        )

        released = self.engine._release_full_case_listing_capacity()

        self.assertEqual(0, released)
        state = self.engine._case_capacity_observation()
        self.assertEqual("observation_gap", state["continuityResetReason"])

    def test_case_listing_capacity_snapshot_failure_breaks_full_continuity(self) -> None:
        self.engine.config.case_max_open_guadao_count = 8
        self.engine.config.case_full_release_after_hours = 3.0
        self.engine.config.case_full_release_fraction = 0.125
        self._add_active_case_listings(8, age_hours=4.0)
        self._seed_case_full_observation(full_since_hours=4.0)

        with patch.object(
            self.engine.steam_client,
            "list_active_listings",
            side_effect=RuntimeError("Steam temporarily unavailable"),
        ):
            released = self.engine._release_full_case_listing_capacity()

        self.assertEqual(0, released)
        state = self.engine._case_capacity_observation()
        self.assertIsNone(state["isFull"])
        self.assertIsNone(state["fullSince"])
        self.assertEqual("snapshot_unavailable", state["continuityResetReason"])

    def test_case_listing_capacity_not_full_then_full_restarts_timer(self) -> None:
        self.engine.config.case_max_open_guadao_count = 8
        self.engine.config.case_full_release_after_hours = 3.0
        self.engine.config.case_full_release_fraction = 0.125
        self._add_active_case_listings(7, age_hours=4.0)
        self._seed_case_full_observation(full_since_hours=4.0)

        self.assertEqual(0, self.engine._release_full_case_listing_capacity())
        self.assertFalse(self.engine._case_capacity_observation()["isFull"])

        self._add_active_case_listings(1, age_hours=4.0)
        self.assertEqual(0, self.engine._release_full_case_listing_capacity())
        state = self.engine._case_capacity_observation()
        self.assertTrue(state["isFull"])
        self.assertLess(
            (datetime.now(timezone.utc) - datetime.fromisoformat(state["fullSince"])).total_seconds(),
            10,
        )

    def test_full_case_listing_release_failure_keeps_remote_and_local_state(self) -> None:
        self.engine.config.case_max_open_guadao_count = 8
        self.engine.config.case_full_release_after_hours = 3.0
        self.engine.config.case_full_release_fraction = 0.125
        operation_ids = self._add_active_case_listings(8, age_hours=4.0)
        self._seed_case_full_observation(full_since_hours=4.0)
        self.engine.steam_client.remove_listing_should_fail = True

        released = self.engine._release_full_case_listing_capacity()

        self.assertEqual(0, released)
        row = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (operation_ids[0],),
        ).fetchone()
        self.assertEqual("listed", row["status"])
        self.assertEqual("listed", self.db.get_asset("asset-sequence-0")["status"])
        self.assertEqual(8, self.engine._open_case_guadao_count())

    def test_rebuy_waiting_emits_structured_prices_and_ratio(self) -> None:
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.69,
            note=(
                '{"sourceSellOperationId":1,"steamListPrice":2.86,'
                '"steamNetFactorAtOpen":0.869,"steamAccountName":"main-account"}'
            ),
        )
        result = RebuyResult(
            False,
            True,
            "ratio_no_longer_profitable",
            actual_price=1.71,
            max_price=1.69,
            steam_reference_price=2.86,
            listing_ratio_now=0.688,
        )
        logger = FakeGuadaoLogger()

        with patch(
            "cs2_assistant.services.executor_engine.execute_rebuy",
            return_value=result,
        ), patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=logger,
        ):
            rebought = self.engine._execute_rebuys()

        self.assertEqual(0, rebought)
        event = next(row for row in logger.events if row["operation"] == "c5_rebuy_waiting")
        context = event["safe_context"]
        assert isinstance(context, dict)
        self.assertEqual("ratio_no_longer_profitable", context["reason"])
        self.assertEqual(1.71, context["c5ActualPrice"])
        self.assertEqual(1.69, context["c5MaxPrice"])
        self.assertAlmostEqual(2.48534, float(context["steamNetAmount"]), places=5)
        self.assertEqual(0.688, context["rebuyRatio"])

    def test_rebuy_submission_emits_c5_order_identifiers(self) -> None:
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note='{"sourceSellOperationId":1,"steamListPrice":3.0}',
        )
        result = RebuyResult(
            True,
            False,
            "ok",
            actual_price=1.58,
            max_price=1.60,
            payload={
                "orderAssetId": "asset-order-1",
                "orderId": "trade-order-1",
                "payStatus": 1,
            },
            out_trade_no="out-trade-1",
        )
        logger = FakeGuadaoLogger()

        with patch(
            "cs2_assistant.services.executor_engine.execute_rebuy",
            return_value=result,
        ), patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=logger,
        ):
            rebought = self.engine._execute_rebuys()

        self.assertEqual(1, rebought)
        event = next(row for row in logger.events if row["operation"] == "c5_rebuy_submitted")
        context = event["safe_context"]
        assert isinstance(context, dict)
        self.assertEqual(1.58, context["c5ActualPrice"])
        self.assertEqual("out-trade-1", context["c5OutTradeNo"])
        self.assertEqual("asset-order-1", context["c5OrderId"])
        self.assertEqual("trade-order-1", context["c5TradeOrderId"])
        self.assertEqual("delivery_pending", context["state"])

    def test_rebuy_http_200_without_order_credentials_stays_submission_unconfirmed(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                }
            ),
        )
        result = RebuyResult(
            False,
            False,
            "c5_submission_unconfirmed",
            actual_price=1.58,
            max_price=1.60,
            payload={"payStatus": 2, "orderAssetId": None, "orderId": None},
            out_trade_no="out-missing-orders",
            submitted_at="2026-07-17T05:52:55+00:00",
            submission_outcome="unconfirmed",
        )

        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result):
            rebought = self.engine._execute_rebuys()

        self.assertEqual(0, rebought)
        row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("c5_submission_unconfirmed", row["status"])
        note = json.loads(row["note"])
        self.assertEqual("out-missing-orders", note["c5OutTradeNo"])
        self.assertEqual("2026-07-17T05:52:55+00:00", note["c5OrderSubmittedAt"])
        self.assertNotIn("c5DeliveryDeadlineAt", note)

    def test_rebuy_http_200_with_both_order_ids_ignores_pay_status_for_existence(self) -> None:
        submitted_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            microsecond=0
        ).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                }
            ),
        )
        result = RebuyResult(
            True,
            False,
            "ok",
            actual_price=1.58,
            payload={
                "payStatus": 2,
                "orderAssetId": "asset-http-200-pay-two",
                "orderId": "trade-http-200-pay-two",
            },
            out_trade_no="out-http-200-pay-two",
            submitted_at=submitted_at,
        )
        detail_calls: list[str] = []

        def buyer_order_detail(order_id: str) -> dict[str, object]:
            detail_calls.append(order_id)
            raise RuntimeError("temporary detail outage")

        self.engine.c5_client.buyer_order_detail = buyer_order_detail
        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result):
            rebought = self.engine._execute_rebuys()

        self.assertEqual(1, rebought)
        self.assertEqual(["asset-http-200-pay-two"], detail_calls)
        row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(row["note"])
        self.assertEqual("delivery_pending", row["status"])
        self.assertEqual("asset-http-200-pay-two", note["c5OrderId"])
        self.assertEqual("trade-http-200-pay-two", note["c5TradeOrderId"])
        self.assertEqual(2, note["c5PayStatus"])
        self.assertEqual("temporary detail outage", note["c5OrderDetailLastError"])

    def test_rebuy_immediate_completed_detail_does_not_emit_waiting_submission_event(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note='{"sourceSellOperationId":1,"steamListPrice":3.0}',
        )
        result = RebuyResult(
            True,
            False,
            "ok",
            actual_price=1.58,
            payload={
                "payStatus": 2,
                "orderAssetId": "asset-direct-completed",
                "orderId": "trade-direct-completed",
            },
            out_trade_no="out-direct-completed",
        )
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "orderAssetId": "asset-direct-completed",
            "orderId": "trade-direct-completed",
            "status": 10,
            "statusName": "success",
            "marketHashName": "Revolution Case",
        }
        logger = FakeGuadaoLogger()

        with patch(
            "cs2_assistant.services.executor_engine.execute_rebuy", return_value=result
        ), patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=logger,
        ):
            rebought = self.engine._execute_rebuys()

        self.assertEqual(1, rebought)
        self.assertEqual(
            "completed",
            self.db.conn.execute(
                "SELECT status FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["status"],
        )
        operations = [str(row.get("operation")) for row in logger.events]
        self.assertIn("c5_rebuy_completed", operations)
        self.assertNotIn("c5_rebuy_submitted", operations)

    def test_submission_reconcile_unique_order_backfills_both_ids_and_enters_delivery(self) -> None:
        submitted_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            microsecond=0
        )
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.0,
                    "steamId64": "76561198000000000",
                    "c5OutTradeNo": "out-unique",
                    "c5OrderSubmittedAt": submitted_at.isoformat(),
                    "c5OrderPayload": {"payStatus": 2},
                    "c5SubmissionNotCreatedCount": 2,
                }
            ),
        )
        self.db.update_pool_operation(
            op_id,
            status="c5_submission_unconfirmed",
            actual_price=1.58,
        )
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "outTradeNo": "out-unique",
                    "orderAssetId": "asset-order-unique",
                    "orderId": "trade-order-unique",
                    "payStatus": 1,
                    "marketHashName": "Revolution Case",
                    "receiveSteamId": "76561198000000000",
                    "price": 1.58,
                    "createTime": int(submitted_at.timestamp()),
                }
            ],
            "pages": 1,
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertTrue(result["ok"])
        self.assertEqual("delivery_pending", result["state"])
        self.assertEqual(0, result["replacements"])
        row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("delivery_pending", row["status"])
        note = json.loads(row["note"])
        self.assertEqual("asset-order-unique", note["c5OrderId"])
        self.assertEqual("trade-order-unique", note["c5TradeOrderId"])
        self.assertEqual(1, note["c5PayStatus"])
        self.assertEqual(0, note["c5SubmissionNotCreatedCount"])
        self.assertIn("c5DeliveryDeadlineAt", note)

    def test_submission_reconcile_uses_detail_to_resolve_trade_id_from_asset_only_list_row(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-detail-backfill",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "outTradeNo": "out-detail-backfill",
                    "orderId": "asset-order-from-list",
                    "marketHashName": "Revolution Case",
                    "price": 1.58,
                }
            ],
            "pages": 1,
        }
        detail_calls: list[str] = []

        def buyer_order_detail(order_id: str) -> dict[str, object]:
            detail_calls.append(order_id)
            return {
                "orderAssetId": "asset-order-from-list",
                "orderId": "trade-order-from-detail",
                "payStatus": 1,
                "marketHashName": "Revolution Case",
            }

        self.engine.c5_client.buyer_order_detail = buyer_order_detail

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual(["asset-order-from-list"], detail_calls)
        self.assertEqual("delivery_pending", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual("asset-order-from-list", note["c5OrderId"])
        self.assertEqual("trade-order-from-detail", note["c5TradeOrderId"])

    def test_order_detail_does_not_overwrite_quick_buy_trade_order_id(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.68,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-625",
                    "c5TradeOrderId": "trade-order-624",
                    "c5OrderPayload": {
                        "orderAssetId": "asset-order-625",
                        "orderId": "trade-order-624",
                        "payStatus": 1,
                    },
                    "c5OrderSubmittedAt": "2026-07-19T16:29:32+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="delivery_pending", actual_price=1.68)
        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(op["note"])

        state, replacements = self.engine._apply_recognized_c5_order_detail(
            op,
            note,
            {
                # buyer/detail uses orderId for the asset-order lookup id.
                "orderId": "asset-order-625",
                "status": 1,
                "statusName": "delivery_pending",
                "marketHashName": "Revolution Case",
            },
            "asset-order-625",
        )

        self.assertEqual("delivery_pending", state)
        self.assertEqual(0, replacements)
        updated_note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual("asset-order-625", updated_note["c5OrderId"])
        self.assertEqual("trade-order-624", updated_note["c5TradeOrderId"])

    def test_submission_reconcile_recognizes_order_when_pay_status_is_not_one(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-pay-pending",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "outTradeNo": "out-pay-pending",
                    "orderAssetId": "asset-pay-pending",
                    "orderId": "trade-pay-pending",
                    "payStatus": 2,
                    "marketHashName": "Revolution Case",
                }
            ],
            "pages": 1,
        }
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "orderAssetId": "asset-pay-pending",
            "orderId": "trade-pay-pending",
            "payStatus": 2,
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("delivery_pending", result["state"])
        row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(row["note"])
        self.assertEqual("delivery_pending", row["status"])
        self.assertEqual("asset-pay-pending", note["c5OrderId"])
        self.assertEqual("trade-pay-pending", note["c5TradeOrderId"])
        self.assertEqual(2, note["c5PayStatus"])
        self.assertIn("c5DeliveryDeadlineAt", note)

    def test_submission_reconcile_reads_beyond_five_pages_until_submitted_window_is_covered(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-outside-window",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        requested_pages: list[int] = []

        def buyer_order_status(**kwargs: object) -> dict[str, object]:
            page_num = int(kwargs["page_num"])
            requested_pages.append(page_num)
            if page_num < 6:
                rows = [
                    {
                        "orderAssetId": f"other-asset-{page_num}-{index}",
                        "orderId": f"other-trade-{page_num}-{index}",
                        "payStatus": 1,
                        "marketHashName": "Kilowatt Case",
                        "createTime": "2026-07-17T06:00:00+00:00",
                    }
                    for index in range(100)
                ]
            else:
                rows = [
                    {
                        "outTradeNo": "out-outside-window",
                        "orderAssetId": "asset-page-six",
                        "orderId": "trade-page-six",
                        "payStatus": 2,
                        "marketHashName": "Revolution Case",
                        "createTime": "2026-07-17T05:52:55+00:00",
                    },
                    {
                        "orderAssetId": "older-boundary",
                        "orderId": "older-boundary-trade",
                        "marketHashName": "Kilowatt Case",
                        "createTime": "2026-07-17T05:50:00+00:00",
                    },
                ]
            return {"list": rows, "pages": 10}

        self.engine.c5_client.buyer_order_status = buyer_order_status
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "orderAssetId": "asset-page-six",
            "orderId": "trade-page-six",
            "status": 5,
            "statusName": "delivery_pending",
            "marketHashName": "Revolution Case",
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("delivery_pending", result["state"])
        self.assertEqual([1, 2, 3, 4, 5, 6], requested_pages)
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(0, note.get("c5SubmissionReconcileAbsenceCount", 0))
        self.assertEqual("asset-page-six", note["c5OrderId"])

    def test_submission_reconcile_repeated_page_is_not_complete_absence_evidence(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-repeated-pages",
                    "c5OrderSubmittedAt": "2026-07-17T05:00:00+00:00",
                    "c5SubmissionReconcileAbsenceCount": 1,
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        requested_pages: list[int] = []
        repeated_rows = [
            {
                "orderAssetId": f"newer-{index}",
                "orderId": f"newer-trade-{index}",
                "marketHashName": "Kilowatt Case",
                "createTime": "2026-07-17T06:00:00+00:00",
            }
            for index in range(100)
        ]

        def buyer_order_status(**kwargs: object) -> dict[str, object]:
            requested_pages.append(int(kwargs["page_num"]))
            return {"list": repeated_rows, "pages": 200}

        self.engine.c5_client.buyer_order_status = buyer_order_status
        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", result["state"])
        self.assertLessEqual(len(requested_pages), 3)
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(1, note["c5SubmissionReconcileAbsenceCount"])
        self.assertEqual("buyer_order_window_not_covered", note["c5SubmissionLastCheckError"])

    def test_submission_reconcile_budget_doubles_and_rescans_fuzzy_candidate_from_page_one(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.58,
            note=json.dumps(
                {
                    "steamId64": "76561198000000000",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        fuzzy_row = {
            "orderAssetId": "asset-budget-fuzzy",
            "orderId": "trade-budget-fuzzy",
            "marketHashName": "Revolution Case",
            "receiveSteamId": "76561198000000000",
            "price": 1.58,
            "createTime": "2026-07-17T05:53:00+00:00",
        }
        first_pages: list[int] = []
        self.engine.c5_client.buyer_order_status = lambda **kwargs: (
            first_pages.append(int(kwargs["page_num"]))
            or {"list": [fuzzy_row], "pages": 3}
        )

        with patch(
            "cs2_assistant.services.executor_engine.C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET",
            1,
        ):
            first = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", first["state"])
        self.assertEqual([1], first_pages)
        first_note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(2, first_note["c5SubmissionReconcileNextPageBudget"])

        second_pages: list[int] = []

        def second_status(**kwargs: object) -> dict[str, object]:
            page_num = int(kwargs["page_num"])
            second_pages.append(page_num)
            if page_num == 1:
                return {"list": [fuzzy_row], "pages": 3}
            return {
                "list": [
                    {
                        "orderAssetId": "older-budget-boundary",
                        "orderId": "older-budget-boundary-trade",
                        "marketHashName": "Kilowatt Case",
                        "createTime": "2026-07-17T05:50:00+00:00",
                    }
                ],
                "pages": 3,
            }

        self.engine.c5_client.buyer_order_status = second_status
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "orderAssetId": "asset-budget-fuzzy",
            "orderId": "trade-budget-fuzzy",
            "status": 5,
            "marketHashName": "Revolution Case",
        }
        with patch(
            "cs2_assistant.services.executor_engine.C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET",
            1,
        ):
            second = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("delivery_pending", second["state"])
        self.assertEqual([1, 2], second_pages)

    def test_submission_reconcile_budget_retry_rescans_delayed_exact_order_on_page_one(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.58,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-delayed-homepage",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        first_pages: list[int] = []
        self.engine.c5_client.buyer_order_status = lambda **kwargs: (
            first_pages.append(int(kwargs["page_num"]))
            or {
                "list": [
                    {
                        "orderAssetId": f"newer-{index}",
                        "orderId": f"newer-trade-{index}",
                        "marketHashName": "Kilowatt Case",
                        "createTime": "2026-07-17T06:00:00+00:00",
                    }
                    for index in range(100)
                ],
                "pages": 3,
            }
        )
        with patch(
            "cs2_assistant.services.executor_engine.C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET",
            1,
        ):
            first = self.engine.run_guadao_c5_submission_reconcile_task(op_id)
        self.assertEqual("c5_submission_unconfirmed", first["state"])
        self.assertEqual([1], first_pages)

        second_pages: list[int] = []
        self.engine.c5_client.buyer_order_status = lambda **kwargs: (
            second_pages.append(int(kwargs["page_num"]))
            or {
                "list": [
                    {
                        "outTradeNo": "out-delayed-homepage",
                        "orderAssetId": "asset-delayed-homepage",
                        "orderId": "trade-delayed-homepage",
                        "marketHashName": "Revolution Case",
                    }
                ],
                "pages": 1,
            }
        )
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "orderAssetId": "asset-delayed-homepage",
            "orderId": "trade-delayed-homepage",
            "status": 5,
            "marketHashName": "Revolution Case",
        }
        with patch(
            "cs2_assistant.services.executor_engine.C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET",
            1,
        ):
            second = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("delivery_pending", second["state"])
        self.assertEqual([1], second_pages)

    def test_submission_reconcile_page_budget_stops_growing_at_safety_cap(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.58,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-page-budget-cap",
                    "c5OrderSubmittedAt": "2026-07-17T05:00:00+00:00",
                    "c5SubmissionReconcileAbsenceCount": 1,
                    "c5SubmissionReconcileNextPageBudget": 1,
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "orderAssetId": f"newer-{kwargs['page_num']}",
                    "orderId": f"newer-trade-{kwargs['page_num']}",
                    "marketHashName": "Kilowatt Case",
                    # Missing time deliberately prevents submittedAt coverage.
                }
            ],
            "pages": 10_000,
        }

        with patch(
            "cs2_assistant.services.executor_engine.C5_SUBMISSION_RECONCILE_INITIAL_PAGE_BUDGET",
            1,
        ), patch(
            "cs2_assistant.services.executor_engine.C5_SUBMISSION_RECONCILE_MAX_PAGE_BUDGET",
            1,
        ):
            result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(1, note["c5SubmissionReconcileNextPageBudget"])
        self.assertEqual(1, note["c5SubmissionReconcileAbsenceCount"])
        self.assertEqual(
            "max_page_budget_exhausted_without_coverage",
            note["c5SubmissionReconcileAlertCode"],
        )
        self.assertIn("c5SubmissionReconcileSafetyCapReachedAt", note)

    def test_submission_reconcile_empty_page_before_declared_end_is_incomplete(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-early-empty",
                    "c5OrderSubmittedAt": "2026-07-17T05:00:00+00:00",
                    "c5SubmissionReconcileAbsenceCount": 1,
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [],
            "pages": 10,
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(1, note["c5SubmissionReconcileAbsenceCount"])
        self.assertEqual("buyer_order_window_not_covered", note["c5SubmissionLastCheckError"])

    def test_submission_reconcile_unique_failed_detail_fails_and_creates_replacement(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                    "c5OutTradeNo": "out-detail-failed",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "outTradeNo": "out-detail-failed",
                    "orderAssetId": "asset-detail-failed",
                    "orderId": "trade-detail-failed",
                    "payStatus": 2,
                    "marketHashName": "Revolution Case",
                }
            ],
            "pages": 1,
        }
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "orderAssetId": "asset-detail-failed",
            "orderId": "trade-detail-failed",
            "status": 11,
            "statusName": "failed",
            "failedCode": "SELLER_TIMEOUT",
            "failedDesc": "seller did not deliver",
            "marketHashName": "Revolution Case",
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_failed", result["state"])
        self.assertEqual(1, result["replacements"])
        replacements = [
            row
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=20)
            if json.loads(row["note"]).get("replacementForRebuyOperationId") == op_id
        ]
        self.assertEqual(1, len(replacements))

    def test_submission_reconcile_unique_completed_detail_completes_immediately(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-detail-completed",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "outTradeNo": "out-detail-completed",
                    "orderAssetId": "asset-detail-completed",
                    "orderId": "trade-detail-completed",
                    "payStatus": 2,
                    "marketHashName": "Revolution Case",
                }
            ],
            "pages": 1,
        }
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "orderAssetId": "asset-detail-completed",
            "orderId": "trade-detail-completed",
            "status": 10,
            "statusName": None,
            "marketHashName": "Revolution Case",
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("completed", result["state"])
        row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("completed", row["status"])
        self.assertEqual("c5_success", json.loads(row["note"])["c5FinalStatus"])

    def test_submission_reconcile_detail_item_mismatch_requires_manual_without_replacement(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-detail-mismatch",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "outTradeNo": "out-detail-mismatch",
                    "orderAssetId": "asset-detail-mismatch",
                    "orderId": "trade-detail-mismatch",
                    "marketHashName": "Revolution Case",
                }
            ],
            "pages": 1,
        }
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "orderAssetId": "asset-detail-mismatch",
            "orderId": "trade-detail-mismatch",
            "status": 10,
            "statusName": "success",
            "marketHashName": "Kilowatt Case",
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("manual_required", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual("c5_order_market_hash_name_mismatch", note["c5SubmissionManualReason"])
        replacements = [
            row
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=20)
            if json.loads(row["note"]).get("replacementForRebuyOperationId") == op_id
        ]
        self.assertEqual([], replacements)

    def test_recognized_single_lookup_id_stays_in_delivery_when_detail_is_unreadable(self) -> None:
        submitted_at = datetime.now(timezone.utc).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-recognized-single-id",
                    "c5OrderRecognized": True,
                    "c5OrderMatchMode": "safe_unique_fuzzy",
                    "c5OrderSubmittedAt": submitted_at,
                    "c5FinalStatus": "pending",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="delivery_pending", actual_price=1.58)
        self.engine.c5_client.buyer_order_detail = lambda order_id: (_ for _ in ()).throw(
            RuntimeError("detail temporarily unavailable")
        )

        result = self.engine.run_guadao_delivery_confirmation_task(op_id)

        self.assertEqual("delivery_pending", result["status"])
        row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("delivery_pending", row["status"])
        note = json.loads(row["note"])
        self.assertTrue(note["c5OrderRecognized"])
        self.assertNotIn("c5SubmissionUnconfirmedReason", note)

    def test_recognized_trade_only_id_returns_to_submission_reconcile_without_detail_lookup(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5TradeOrderId": "trade-only-not-queryable",
                    "c5OrderRecognized": True,
                    "c5OrderMatchMode": "safe_unique_fuzzy",
                    "c5OrderSubmittedAt": datetime.now(timezone.utc).isoformat(),
                    "c5FinalStatus": "pending",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="delivery_pending", actual_price=1.58)
        detail_calls: list[str] = []
        self.engine.c5_client.buyer_order_detail = lambda order_id: detail_calls.append(order_id)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {"list": [], "pages": 2}

        result = self.engine.run_guadao_delivery_confirmation_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", result["status"])
        self.assertEqual([], detail_calls)
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(
            "legacy_delivery_missing_required_order_evidence",
            note["c5SubmissionUnconfirmedReason"],
        )

    def test_submission_reconcile_does_not_count_absence_when_sweeper_state_is_unreadable(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-sweeper-state-unreadable",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                    "c5SubmissionReconcileAbsenceCount": 1,
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {"list": [], "pages": 1}
        fake_project_root = Path(self.temp_dir.name) / "sweeper-state-project"
        state_path = fake_project_root / "data" / "c5_case_sweeper_v2_state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{not-valid-json", encoding="utf-8")

        with patch("cs2_assistant.services.executor_engine.PROJECT_ROOT", fake_project_root):
            result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(1, note["c5SubmissionReconcileAbsenceCount"])
        self.assertEqual(
            "c5_sweeper_claim_evidence_unavailable",
            note["c5SubmissionLastCheckError"],
        )

    def test_submission_reconcile_does_not_fuzzy_claim_without_complete_account_evidence(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.58,
            note=json.dumps(
                {
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                    # Deliberately missing steamId64: fuzzy ownership must not
                    # be inferred from item, price and time alone.
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "orderAssetId": "asset-incomplete-fuzzy",
                    "orderId": "trade-incomplete-fuzzy",
                    "payStatus": 1,
                    "marketHashName": "Revolution Case",
                    "receiveSteamId": "76561198000000000",
                    "price": 1.58,
                    "createTime": "2026-07-17T05:53:10+00:00",
                }
            ],
            "pages": 1,
        }
        fake_project_root = Path(self.temp_dir.name) / "no-sweeper-state-project"

        with patch("cs2_assistant.services.executor_engine.PROJECT_ROOT", fake_project_root):
            result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", result["state"])
        row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(row["note"])
        self.assertEqual("c5_submission_unconfirmed", row["status"])
        self.assertEqual(1, note["c5SubmissionReconcileAbsenceCount"])
        self.assertNotIn("c5OrderId", note)

    def test_submission_reconcile_fuzzy_overlap_with_uncertain_sweeper_requires_manual(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.58,
            note=json.dumps(
                {
                    "steamId64": "76561198000000000",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    # C5 list rows can omit outTradeNo, making this otherwise
                    # look like a valid guadao fuzzy candidate.
                    "orderAssetId": "asset-sweeper-overlap",
                    "orderId": "trade-sweeper-overlap",
                    "payStatus": 1,
                    "marketHashName": "Revolution Case",
                    "receiveSteamId": "76561198000000000",
                    "price": 1.58,
                    "createTime": "2026-07-17T05:53:10+00:00",
                }
            ],
            "pages": 1,
        }
        fake_project_root = Path(self.temp_dir.name) / "uncertain-sweeper-state-project"
        state_path = fake_project_root / "data" / "c5_case_sweeper_v2_state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "rounds": [
                        {
                            "marketHashName": "Revolution Case",
                            "receivingSteamId": "76561198000000000",
                            "createdAt": "2026-07-17T05:52:30+00:00",
                            "orders": [],
                            "submissions": [
                                {
                                    "status": "uncertain",
                                    "submittedAt": "2026-07-17T05:53:00+00:00",
                                    "marketHashName": "Revolution Case",
                                    "receivingSteamId": "76561198000000000",
                                    "products": [
                                        {
                                            "productId": "sweeper-product-1",
                                            "buyPrice": 1.58,
                                            "outTradeNo": "sweeper-out-1",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with patch("cs2_assistant.services.executor_engine.PROJECT_ROOT", fake_project_root):
            result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("manual_required", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(
            "remote_order_overlaps_unconfirmed_c5_sweeper_submission",
            note["c5SubmissionManualReason"],
        )

    def test_submission_reconcile_exact_out_trade_claimed_by_other_operation_is_manual(self) -> None:
        target_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-duplicate-claim",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(target_id, status="c5_submission_unconfirmed", actual_price=1.58)
        owner_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-duplicate-claim",
                    "c5OrderId": "asset-owned",
                    "c5TradeOrderId": "trade-owned",
                    "c5PayStatus": 1,
                }
            ),
        )
        self.db.update_pool_operation(owner_id, status="delivery_pending", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "outTradeNo": "out-duplicate-claim",
                    "orderAssetId": "asset-owned",
                    "orderId": "trade-owned",
                    "payStatus": 1,
                    "marketHashName": "Revolution Case",
                }
            ],
            "pages": 1,
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(target_id)

        self.assertEqual("manual_required", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (target_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(
            "exact_out_trade_no_claimed_by_other_operation",
            note["c5SubmissionManualReason"],
        )

    def test_submission_reconcile_ignores_unrelated_ambiguous_claim_before_counting_absence(self) -> None:
        target_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-target-no-order",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(target_id, status="c5_submission_unconfirmed", actual_price=1.58)
        unrelated_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.02,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-unrelated-incomplete",
                    "c5OrderId": "asset-unrelated-incomplete",
                }
            ),
        )
        self.db.update_pool_operation(unrelated_id, status="c5_submission_unconfirmed", actual_price=1.02)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "outTradeNo": "out-unrelated-incomplete",
                    "orderAssetId": "asset-unrelated-incomplete",
                    "orderId": "trade-unrelated-incomplete",
                    "payStatus": 1,
                    "marketHashName": "Kilowatt Case",
                    "price": 1.02,
                    "createTime": "2026-07-17T05:52:55+00:00",
                }
            ],
            "pages": 1,
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(target_id)

        self.assertEqual("c5_submission_unconfirmed", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (target_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(1, note["c5SubmissionReconcileAbsenceCount"])
        self.assertNotIn("c5SubmissionManualReason", note)

    def test_submission_reconcile_excludes_orders_claimed_by_other_local_rebuys(self) -> None:
        submitted_at_value = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            microsecond=0
        )
        submitted_at = submitted_at_value.isoformat()
        target_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.58,
            note=json.dumps(
                {
                    "steamId64": "76561198000000000",
                    "c5OrderSubmittedAt": submitted_at,
                }
            ),
        )
        self.db.update_pool_operation(target_id, status="c5_submission_unconfirmed", actual_price=1.58)
        for suffix in ("a", "b"):
            claimed_id = self.db.add_pool_operation(
                market_hash_name="Revolution Case",
                strategy=STRATEGY_GUADAO,
                operation_type=OP_REBUY_C5,
                expected_price=1.58,
                note=json.dumps(
                    {
                        "c5OrderId": f"asset-order-{suffix}",
                        "c5TradeOrderId": f"trade-order-{suffix}",
                        "c5PayStatus": 1,
                    }
                ),
            )
            self.db.update_pool_operation(claimed_id, status="delivery_pending", actual_price=1.58)

        def remote_row(suffix: str) -> dict[str, object]:
            return {
                "orderAssetId": f"asset-order-{suffix}",
                "orderId": f"trade-order-{suffix}",
                "payStatus": 1,
                "marketHashName": "Revolution Case",
                "receiveSteamId": "76561198000000000",
                "price": 1.58,
                "createTime": int(submitted_at_value.timestamp()),
            }

        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [remote_row("a"), remote_row("b"), remote_row("free")],
            "pages": 1,
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(target_id)

        self.assertEqual("delivery_pending", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (target_id,)
            ).fetchone()["note"]
        )
        self.assertEqual("asset-order-free", note["c5OrderId"])
        self.assertEqual("trade-order-free", note["c5TradeOrderId"])

    def test_submission_reconcile_only_claimed_candidates_counts_as_absence(self) -> None:
        target_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.58,
            note=json.dumps(
                {
                    "steamId64": "76561198000000000",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(target_id, status="c5_submission_unconfirmed", actual_price=1.58)
        claimed_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.58,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-claimed",
                    "c5TradeOrderId": "trade-order-claimed",
                    "c5PayStatus": 1,
                }
            ),
        )
        self.db.update_pool_operation(claimed_id, status="delivery_pending", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [
                {
                    "orderAssetId": "asset-order-claimed",
                    "orderId": "trade-order-claimed",
                    "payStatus": 1,
                    "marketHashName": "Revolution Case",
                    "receiveSteamId": "76561198000000000",
                    "price": 1.58,
                    "createTime": 1784267575,
                }
            ],
            "pages": 1,
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(target_id)

        self.assertEqual("c5_submission_unconfirmed", result["state"])
        note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (target_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(1, note["c5SubmissionReconcileAbsenceCount"])

    def test_submission_reconcile_three_complete_absences_fail_and_create_one_replacement(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.0,
                    "listingRatioAtOpen": 0.62,
                    "maxRebuyRatioAtOpen": 0.62,
                    "guadaoMaxListingRatioAtOpen": 0.69,
                    "steamNetFactorAtOpen": 0.869,
                    "c5OutTradeNo": "out-absent",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(
            op_id,
            status="c5_submission_unconfirmed",
            actual_price=1.58,
        )
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {"list": [], "pages": 1}

        first = self.engine.run_guadao_c5_submission_reconcile_task(op_id)
        second = self.engine.run_guadao_c5_submission_reconcile_task(op_id)
        third = self.engine.run_guadao_c5_submission_reconcile_task(op_id)
        fourth = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", first["state"])
        self.assertEqual("c5_submission_unconfirmed", second["state"])
        self.assertEqual("c5_failed", third["state"])
        self.assertEqual(1, third["replacements"])
        self.assertEqual("c5_failed", fourth["state"])
        self.assertEqual(0, fourth["replacements"])
        original = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        original_note = json.loads(original["note"])
        self.assertEqual("submission_not_created", original_note["c5OrderFailedCode"])
        replacements = [
            row
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)
            if json.loads(row["note"]).get("replacementForRebuyOperationId") == op_id
        ]
        self.assertEqual(1, len(replacements))
        replacement_note = json.loads(replacements[0]["note"])
        self.assertEqual(1.58, replacements[0]["expected_price"])
        self.assertEqual(1.58, replacement_note["replacementMaxPrice"])
        self.assertAlmostEqual(0.62, replacement_note["maxRebuyRatioAtOpen"], places=6)

    def test_submission_not_created_chain_allows_two_replacements_then_requires_manual(self) -> None:
        current_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                    "c5OutTradeNo": "out-chain-0",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(current_id, status="c5_submission_unconfirmed", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {"list": [], "pages": 1}
        replacement_ids: list[int] = []

        for generation in range(3):
            results = [
                self.engine.run_guadao_c5_submission_reconcile_task(current_id)
                for _ in range(3)
            ]
            if generation < 2:
                self.assertEqual("c5_failed", results[-1]["state"])
                self.assertEqual(1, results[-1]["replacements"])
                current_note = json.loads(
                    self.db.conn.execute(
                        "SELECT note FROM pool_operations WHERE id = ?", (current_id,)
                    ).fetchone()["note"]
                )
                replacement_id = int(current_note["replacementRebuyOperationId"])
                replacement_ids.append(replacement_id)
                replacement = self.db.conn.execute(
                    "SELECT * FROM pool_operations WHERE id = ?", (replacement_id,)
                ).fetchone()
                replacement_note = json.loads(replacement["note"])
                self.assertEqual(generation + 1, replacement_note["c5SubmissionNotCreatedCount"])
                replacement_note.update(
                    {
                        "c5OutTradeNo": f"out-chain-{generation + 1}",
                        "c5OrderSubmittedAt": "2026-07-17T05:55:00+00:00",
                        "c5SubmissionReconcileAbsenceCount": 0,
                    }
                )
                self.db.update_pool_operation(
                    replacement_id,
                    status="c5_submission_unconfirmed",
                    actual_price=1.58,
                    note=json.dumps(replacement_note),
                )
                current_id = replacement_id
            else:
                self.assertEqual("manual_required", results[-1]["state"])
                self.assertEqual(0, results[-1]["replacements"])

        final = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (current_id,)
        ).fetchone()
        final_note = json.loads(final["note"])
        self.assertEqual("manual_required", final["status"])
        self.assertEqual(3, final_note["c5SubmissionNotCreatedCount"])
        self.assertEqual("submission_not_created_chain_limit", final_note["c5SubmissionManualReason"])
        all_replacements = [
            row
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=20)
            if json.loads(row["note"]).get("replacementForRebuyOperationId") is not None
        ]
        self.assertEqual(2, len(all_replacements))

    def test_submission_reconcile_multiple_matching_orders_requires_manual_review(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OutTradeNo": "out-conflict",
                    "c5OrderSubmittedAt": "2026-07-17T05:52:55+00:00",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="c5_submission_unconfirmed", actual_price=1.58)
        duplicate = {
            "outTradeNo": "out-conflict",
            "orderAssetId": "asset-order-a",
            "orderId": "trade-order-a",
            "payStatus": 1,
            "marketHashName": "Revolution Case",
        }
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {
            "list": [duplicate, {**duplicate, "orderAssetId": "asset-order-b", "orderId": "trade-order-b"}],
            "pages": 1,
        }

        result = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("manual_required", result["state"])
        self.assertEqual(0, result["replacements"])
        pending = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)
        self.assertEqual([], pending)

    def test_legacy_delivery_without_order_ids_migrates_and_replaces_after_bounded_absence(self) -> None:
        submitted_at = datetime.now(timezone.utc) - timedelta(hours=25)
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.0,
                    "listingRatioAtOpen": 0.62,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                    "c5OutTradeNo": "out-legacy-empty",
                    "c5OrderSubmittedAt": submitted_at.isoformat(),
                    "c5DeliveryDeadlineAt": (submitted_at + timedelta(hours=12)).isoformat(),
                    "c5FinalStatus": "pending",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="delivery_pending", actual_price=1.58)
        self.engine.c5_client.buyer_order_status = lambda **kwargs: {"list": [], "pages": 1}

        first = self.engine.run_guadao_delivery_confirmation_task(op_id)
        first_row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        second = self.engine.run_guadao_c5_submission_reconcile_task(op_id)
        third = self.engine.run_guadao_c5_submission_reconcile_task(op_id)

        self.assertEqual("c5_submission_unconfirmed", first_row["status"])
        self.assertNotIn("c5DeliveryDeadlineAt", json.loads(first_row["note"]))
        self.assertEqual(0, first["replacements"])
        self.assertEqual("c5_submission_unconfirmed", second["state"])
        self.assertEqual("c5_failed", third["state"])
        self.assertEqual(1, third["replacements"])

    def test_rebuy_delivery_success_emits_actual_c5_amount(self) -> None:
        submitted_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-1",
                    "c5TradeOrderId": "trade-order-1",
                    "c5PayStatus": 1,
                    "c5OutTradeNo": "out-trade-1",
                    "c5OrderSubmittedAt": submitted_at,
                    "c5FinalStatus": "pending",
                }
            ),
        )
        self.db.update_pool_operation(op_id, status="delivery_pending", actual_price=1.58)
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "statusName": "completed",
            "marketHashName": "Revolution Case",
        }
        logger = FakeGuadaoLogger()

        with patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=logger,
        ):
            result_count = self.engine._check_recent_rebuy_delivery_failures()

        self.assertEqual(0, result_count)
        event = next(row for row in logger.events if row["operation"] == "c5_rebuy_completed")
        context = event["safe_context"]
        assert isinstance(context, dict)
        self.assertEqual(1.58, context["c5ActualPrice"])
        self.assertEqual(1.60, context["c5ExpectedPrice"])
        self.assertEqual("asset-order-1", context["c5OrderId"])
        self.assertEqual("c5_success", context["deliveryStatus"])

    def test_auto_rebuy_disabled_still_confirms_targeted_existing_delivery(self) -> None:
        self.engine.config.auto_rebuy_enabled = False
        submitted_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        delivery_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-existing",
                    "c5TradeOrderId": "trade-order-existing",
                    "c5PayStatus": 1,
                    "c5OutTradeNo": "out-existing",
                    "c5OrderSubmittedAt": submitted_at,
                    "c5FinalStatus": "pending",
                }
            ),
        )
        self.db.update_pool_operation(delivery_id, status="delivery_pending", actual_price=1.58)
        pending_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=10.0,
            note='{"steamListPrice":20.0}',
        )
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "statusName": "completed",
            "marketHashName": "Revolution Case",
        }

        with patch("cs2_assistant.services.executor_engine.execute_rebuy") as execute_rebuy_mock:
            self.engine.run_guadao_delivery_confirmation_task(delivery_id)
            submitted = self.engine.run_guadao_rebuy_task(pending_id)

        delivery = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (delivery_id,),
        ).fetchone()
        self.assertEqual("completed", delivery["status"])
        self.assertEqual(0, submitted["rebought"])
        execute_rebuy_mock.assert_not_called()

    def test_rebuy_delivery_overdue_with_unreadable_detail_stays_pending(self) -> None:
        submitted_at = datetime.now(timezone.utc) - timedelta(hours=13)
        deadline = submitted_at + timedelta(hours=12)
        original_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-timeout",
                    "c5TradeOrderId": "trade-order-timeout",
                    "c5PayStatus": 1,
                    "c5OutTradeNo": "out-timeout",
                    "c5OrderSubmittedAt": submitted_at.isoformat(),
                    "c5DeliveryDeadlineAt": deadline.isoformat(),
                    "c5FinalStatus": "pending",
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                }
            ),
        )
        self.db.update_pool_operation(original_id, status="delivery_pending", actual_price=1.58)
        self.engine.c5_client.buyer_order_detail = lambda order_id: None
        logger = FakeGuadaoLogger()

        with patch(
            "cs2_assistant.services.guadao_logging.get_guadao_event_logger",
            return_value=logger,
        ):
            first = self.engine._check_recent_rebuy_delivery_failures()
            second = self.engine._check_recent_rebuy_delivery_failures()

        self.assertEqual(0, first)
        self.assertEqual(0, second)
        operations = [row["operation"] for row in logger.events]
        self.assertEqual(0, operations.count("c5_rebuy_replacement_created"))
        replacements = [
            row
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)
            if json.loads(row["note"]).get("replacementForRebuyOperationId") == original_id
        ]
        self.assertEqual(0, len(replacements))
        original = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (original_id,)
        ).fetchone()
        self.assertEqual("delivery_pending", original["status"])
        self.assertTrue(json.loads(original["note"])["c5DeliveryOverdue"])

    def test_rebuy_delivery_deadline_never_fails_when_detail_lookup_is_unavailable(self) -> None:
        submitted_at = datetime.now(timezone.utc) - timedelta(days=8)
        original_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-old-timeout",
                    "c5TradeOrderId": "trade-order-old-timeout",
                    "c5PayStatus": 1,
                    "c5OutTradeNo": "out-old-timeout",
                    "c5OrderSubmittedAt": submitted_at.isoformat(),
                    "c5FinalStatus": "pending",
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                }
            ),
        )
        self.db.update_pool_operation(original_id, status="delivery_pending", actual_price=1.58)

        with patch.object(
            self.engine,
            "_fetch_c5_buyer_order_detail",
            side_effect=RuntimeError("C5 temporarily unavailable"),
        ) as lookup:
            result = self.engine.run_guadao_delivery_confirmation_task(original_id)

        lookup.assert_called_once()
        original = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (original_id,)
        ).fetchone()
        self.assertEqual("delivery_pending", original["status"])
        self.assertTrue(json.loads(original["note"])["c5DeliveryOverdue"])
        self.assertEqual(0, result["replacements"])

    def test_rebuy_delivery_after_twelve_hours_uses_explicit_detail_failure(self) -> None:
        submitted_at = datetime.now(timezone.utc) - timedelta(hours=13)
        original_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-explicit-failure",
                    "c5TradeOrderId": "trade-order-explicit-failure",
                    "c5OrderSubmittedAt": submitted_at.isoformat(),
                    "c5FinalStatus": "pending",
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                }
            ),
        )
        self.db.update_pool_operation(original_id, status="delivery_pending", actual_price=1.58)
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "statusName": "failed",
            "failedCode": "SELLER_TIMEOUT",
            "failedDesc": "seller did not deliver in 12 hours",
            "marketHashName": "Revolution Case",
        }

        result = self.engine.run_guadao_delivery_confirmation_task(original_id)

        original = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (original_id,)
        ).fetchone()
        self.assertEqual("c5_failed", original["status"])
        self.assertEqual("SELLER_TIMEOUT", json.loads(original["note"])["c5OrderFailedCode"])
        self.assertEqual(1, result["replacements"])

    def test_rebuy_delivery_after_twelve_hours_still_shipping_stays_pending(self) -> None:
        submitted_at = datetime.now(timezone.utc) - timedelta(hours=13)
        original_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-still-shipping",
                    "c5TradeOrderId": "trade-order-still-shipping",
                    "c5OrderSubmittedAt": submitted_at.isoformat(),
                    "c5FinalStatus": "pending",
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                }
            ),
        )
        self.db.update_pool_operation(original_id, status="delivery_pending", actual_price=1.58)
        self.engine.c5_client.buyer_order_detail = lambda order_id: {
            "status": 1,
            "statusName": "shipping",
            "marketHashName": "Revolution Case",
        }

        result = self.engine.run_guadao_delivery_confirmation_task(original_id)

        original = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (original_id,)
        ).fetchone()
        self.assertEqual("delivery_pending", original["status"])
        note = json.loads(original["note"])
        self.assertTrue(note["c5DeliveryOverdue"])
        self.assertEqual(0, result["replacements"])

    def test_rebuy_delivery_timeout_never_starts_without_real_submission_time(self) -> None:
        original_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-without-submission-time",
                    "c5TradeOrderId": "trade-order-without-submission-time",
                    "c5PayStatus": 1,
                    "c5OutTradeNo": "out-without-submission-time",
                    "c5DeliveryDeadlineAt": "2020-01-01T00:00:00+00:00",
                    "c5FinalStatus": "pending",
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                }
            ),
        )
        self.db.update_pool_operation(original_id, status="delivery_pending", actual_price=1.58)
        self.db.conn.execute(
            "UPDATE pool_operations SET created_at = ?, completed_at = ? WHERE id = ?",
            (
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:01:00+00:00",
                original_id,
            ),
        )
        self.db.conn.commit()

        first = self.engine._check_recent_rebuy_delivery_failures()
        current = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (original_id,)
        ).fetchone()
        replacements = [
            row
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)
            if json.loads(row["note"]).get("replacementForRebuyOperationId") == original_id
        ]

        self.assertEqual(0, first)
        self.assertEqual("delivery_pending", current["status"])
        self.assertEqual([], replacements)

    def test_rebuy_delivery_mismatched_c5_order_detail_requires_manual(self) -> None:
        submitted_at = datetime.now(timezone.utc) - timedelta(hours=25)
        original_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=json.dumps(
                {
                    "c5OrderId": "asset-order-mismatch-timeout",
                    "c5TradeOrderId": "trade-order-mismatch-timeout",
                    "c5PayStatus": 1,
                    "c5OutTradeNo": "out-mismatch-timeout",
                    "c5OrderSubmittedAt": submitted_at.isoformat(),
                    "c5FinalStatus": "pending",
                    "steamListPrice": 3.0,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                }
            ),
        )
        self.db.update_pool_operation(original_id, status="delivery_pending", actual_price=1.58)

        with patch.object(
            self.engine,
            "_fetch_c5_buyer_order_detail",
            return_value=(
                {"statusName": "completed", "marketHashName": "Kilowatt Case"},
                "asset-order-mismatch-timeout",
                json.loads(self.db.conn.execute(
                    "SELECT note FROM pool_operations WHERE id = ?", (original_id,)
                ).fetchone()["note"]),
            ),
        ) as lookup:
            result = self.engine.run_guadao_delivery_confirmation_task(original_id)

        lookup.assert_called_once()
        original = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (original_id,)
        ).fetchone()
        self.assertEqual("manual_required", original["status"])
        self.assertEqual(
            "c5_order_market_hash_name_mismatch",
            json.loads(original["note"])["c5SubmissionManualReason"],
        )
        self.assertEqual(0, result["replacements"])

    def test_balance_insufficient_rebuy_stays_pending_for_retry(self) -> None:
        self.engine.serverchan = FakeServerChan()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.23,
            note='{"sourceSellOperationId": 1, "steamListPrice": 2.12, "steamAccountName": "main-account"}',
        )
        result = RebuyResult(
            False,
            False,
            "c5_api_error",
            actual_price=1.23,
            payload={"errorCode": 70001, "errorMsg": "余额不足"},
        )

        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result):
            rebought = self.engine._execute_rebuys()

        self.assertEqual(0, rebought)
        self.assertEqual([], self.engine.serverchan.messages)
        row = self.db.conn.execute("SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        assert row is not None
        self.assertEqual("pending", row["status"])
        self.assertIn('"lastSkipReason": "c5_balance_insufficient"', row["note"])
        self.assertIn('"balanceInsufficientAt":', row["note"])
        self.assertNotIn('"replacementReason": "rebuy_balance_insufficient"', row["note"])
        self.assertNotIn('"replacementRebuyOperationId":', row["note"])
        pending = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)
        self.assertEqual(1, len(pending))
        self.assertIn('"sourceSellOperationId": 1', pending[0]["note"])
        self.assertEqual(0, self.engine._open_case_guadao_count())

    def test_rebuy_batch_matches_cheapest_listings_to_tightest_frozen_prices(self) -> None:
        first_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.00,
            note='{"sourceSellOperationId": 1, "steamListPrice": 2.12}',
        )
        second_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.50,
            note='{"sourceSellOperationId": 2, "steamListPrice": 2.12}',
        )
        self.engine.c5_client.market_products_search_payload = {
            "list": [
                {"productId": "product-cheap", "price": 1.00},
                {"productId": "product-later", "price": 1.20},
            ]
        }
        self.engine.c5_client.batch_buy_payload_by_trade_url[
            self.engine.steam_client.trade_url
        ] = {
            "successList": [
                {"productId": "product-cheap", "orderAssetId": "asset-1", "orderId": "order-1", "actualPay": 1.00},
                {"productId": "product-later", "orderAssetId": "asset-2", "orderId": "order-2", "actualPay": 1.20},
            ],
            "failedList": [],
        }

        result = self.engine.run_guadao_rebuy_batch_task("Revolution Case")

        self.assertEqual(2, result["matched"])
        self.assertEqual(2, result["successes"])
        self.assertEqual(1, result["batchRequests"])
        self.assertEqual(1, result["marketReadRequests"])
        self.assertEqual(3, result["c5RequestCount"])
        self.assertEqual(1, len(self.engine.c5_client.market_products_search_calls))
        self.assertEqual(
            {
                "item_id": "item-default",
                "page_size": 50,
            },
            self.engine.c5_client.market_products_search_calls[0],
        )
        requested = self.engine.c5_client.batch_buy_calls[0]["product_list"]
        self.assertEqual(
            [("product-cheap", 1.0), ("product-later", 1.2)],
            [(row["productId"], row["buyPrice"]) for row in requested],
        )
        rows = {
            int(row["id"]): row
            for row in self.db.list_pool_operations_by_type(
                OP_REBUY_C5,
                status="delivery_pending",
                limit=10,
            )
        }
        self.assertEqual({first_id, second_id}, set(rows))
        self.assertAlmostEqual(1.00, float(rows[first_id]["actual_price"]), places=6)
        self.assertAlmostEqual(1.20, float(rows[second_id]["actual_price"]), places=6)

    def test_rebuy_batch_uses_price_batch_gap_then_reuses_one_concrete_snapshot(self) -> None:
        quick_id = self.db.add_pool_operation(
            market_hash_name="Austin 2025 Legends Autograph Capsule",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.63,
            note='{"sourceSellOperationId": 1, "steamListPrice": 2.12}',
        )
        batch_id = self.db.add_pool_operation(
            market_hash_name="Austin 2025 Legends Autograph Capsule",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.70,
            note='{"sourceSellOperationId": 2, "steamListPrice": 2.12}',
        )
        self.engine.c5_client.price_batch_payload = {
            "Austin 2025 Legends Autograph Capsule": {
                "itemId": "1399837948321718272",
                "price": 1.62,
                "count": 2944,
            }
        }
        self.engine.c5_client.market_products_search_payload = {
            "list": [
                {"productId": "visible-164", "price": 1.64},
                {"productId": "visible-165", "price": 1.65},
            ]
        }
        self.engine.c5_client.quick_buy_results = [
            {"orderAssetId": "quick-asset", "orderId": "quick-order", "actualPay": 1.62},
            C5GameError('{"errorCode": 1317, "errorMsg": "no matching listing"}'),
        ]
        self.engine.c5_client.batch_buy_payload_by_trade_url[
            self.engine.steam_client.trade_url
        ] = {
            "successList": [
                {
                    "productId": "visible-164",
                    "orderAssetId": "batch-asset",
                    "orderId": "batch-order",
                    "actualPay": 1.64,
                }
            ],
            "failedList": [],
        }

        result = self.engine.run_guadao_rebuy_batch_task(
            "Austin 2025 Legends Autograph Capsule"
        )

        self.assertEqual(1.62, result["priceBatchFloor"])
        self.assertEqual(1.64, result["concreteFloor"])
        self.assertEqual(0.02, result["priceFloorGap"])
        self.assertEqual(2, result["quickBuyAttempts"])
        self.assertEqual(1, result["quickBuySuccesses"])
        self.assertEqual(1, result["quickBuyNoMatch"])
        self.assertEqual(1, result["normalBatchSuccesses"])
        self.assertEqual(2, result["successes"])
        self.assertEqual(1, len(self.engine.c5_client.market_products_search_calls))
        self.assertEqual(
            {"item_id": "1399837948321718272", "page_size": 50},
            self.engine.c5_client.market_products_search_calls[0],
        )
        self.assertEqual(2, len(self.engine.c5_client.quick_buy_calls))
        for call in self.engine.c5_client.quick_buy_calls:
            self.assertEqual("1399837948321718272", call["item_id"])
            self.assertEqual(1.63, call["max_price"])
            self.assertNotIn("delivery", call)
            self.assertNotIn("low_price", call)
            self.assertNotIn("market_hash_name", call)
        self.assertEqual(1, len(self.engine.c5_client.batch_buy_calls))
        self.assertEqual(
            [("visible-164", 1.64)],
            [
                (row["productId"], row["buyPrice"])
                for row in self.engine.c5_client.batch_buy_calls[0]["product_list"]
            ],
        )
        completed = {
            int(row["id"]): row
            for row in self.db.list_pool_operations_by_type(
                OP_REBUY_C5,
                status="delivery_pending",
                limit=10,
            )
        }
        self.assertEqual({quick_id, batch_id}, set(completed))
        self.assertAlmostEqual(1.62, float(completed[quick_id]["actual_price"]), places=6)
        self.assertAlmostEqual(1.64, float(completed[batch_id]["actual_price"]), places=6)
        batch_note = json.loads(completed[batch_id]["note"])
        self.assertEqual("rejected_no_match", batch_note["c5GapQuickSubmissionState"])
        self.assertEqual(1317, batch_note["c5ErrorPayload"]["errorCode"])

    def test_rebuy_batch_stops_category_after_unconfirmed_gap_quick_buy(self) -> None:
        first_id = self.db.add_pool_operation(
            market_hash_name="Austin 2025 Legends Autograph Capsule",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.63,
            note='{"sourceSellOperationId": 1, "steamListPrice": 2.12}',
        )
        second_id = self.db.add_pool_operation(
            market_hash_name="Austin 2025 Legends Autograph Capsule",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.70,
            note='{"sourceSellOperationId": 2, "steamListPrice": 2.12}',
        )
        self.engine.c5_client.price_batch_payload = {
            "Austin 2025 Legends Autograph Capsule": {
                "itemId": "1399837948321718272",
                "price": 1.62,
                "count": 2944,
            }
        }
        self.engine.c5_client.market_products_search_payload = {
            "list": [{"productId": "visible-164", "price": 1.64}]
        }
        self.engine.c5_client.quick_buy_results = [TimeoutError("response lost")]

        result = self.engine.run_guadao_rebuy_batch_task(
            "Austin 2025 Legends Autograph Capsule"
        )

        self.assertEqual("c5_gap_quick_unconfirmed", result["reason"])
        self.assertEqual(1, result["quickBuyAttempts"])
        self.assertEqual(1, result["quickBuyUnconfirmed"])
        self.assertEqual([], self.engine.c5_client.batch_buy_calls)
        rows = {
            int(row["id"]): row
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        }
        self.assertEqual("c5_submission_unconfirmed", rows[first_id]["status"])
        first_note = json.loads(rows[first_id]["note"])
        self.assertEqual(
            "c5_gap_quick_submission_unconfirmed",
            first_note["c5SubmissionUnconfirmedReason"],
        )
        self.assertEqual("pending", rows[second_id]["status"])

    def test_rebuy_trade_url_uses_persisted_valid_url_without_steam_client(self) -> None:
        account = Account(
            id="account-a",
            name="account-a",
            steam_id64=self.engine.steam_client.steam_id64,
            trade_url=self.engine.steam_client.trade_url,
        )
        self.engine.account_store = FakeAccountStore([account])

        def unexpected_client(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("a valid persisted trade URL must not construct a Steam client")

        self.engine._steam_client_for_account = unexpected_client  # type: ignore[method-assign]

        resolved = self.engine._resolve_rebuy_trade_url(
            {
                "steamAccountId": account.id,
                "steamId64": account.steam_id64,
            }
        )

        self.assertEqual(account.trade_url, resolved)

    def test_rebuy_batch_counts_market_read_when_no_listing_matches(self) -> None:
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.00,
            note='{"sourceSellOperationId": 1, "steamListPrice": 2.12}',
        )
        self.engine.c5_client.market_products_search_payload = {"list": []}

        result = self.engine.run_guadao_rebuy_batch_task("Revolution Case")

        self.assertEqual("no_matching_listing", result["reason"])
        self.assertEqual(1, result["marketReadRequests"])
        self.assertEqual(2, result["c5RequestCount"])
        self.assertEqual([], self.engine.c5_client.batch_buy_calls)

    def test_rebuy_batch_splits_requests_by_receiving_trade_url(self) -> None:
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.00,
            note='{"steamAccountId": "account-a", "steamListPrice": 2.12}',
        )
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.50,
            note='{"steamAccountId": "account-b", "steamListPrice": 2.12}',
        )
        self.engine.c5_client.market_products_search_payload = {
            "list": [
                {"productId": "product-a", "price": 1.00},
                {"productId": "product-b", "price": 1.20},
            ]
        }
        url_a = "https://steamcommunity.com/tradeoffer/new/?partner=1&token=a"
        url_b = "https://steamcommunity.com/tradeoffer/new/?partner=2&token=b"
        self.engine._resolve_rebuy_trade_url = lambda note: (  # type: ignore[method-assign]
            url_a if note.get("steamAccountId") == "account-a" else url_b
        )
        self.engine.c5_client.batch_buy_payload_by_trade_url = {
            url_a: {"successList": [{"productId": "product-a", "orderAssetId": "asset-a", "orderId": "order-a"}], "failedList": []},
            url_b: {"successList": [{"productId": "product-b", "orderAssetId": "asset-b", "orderId": "order-b"}], "failedList": []},
        }

        result = self.engine.run_guadao_rebuy_batch_task("Revolution Case")

        self.assertEqual(2, result["batchRequests"])
        self.assertEqual(2, result["successes"])
        self.assertEqual(
            {url_a, url_b},
            {str(call["trade_url"]) for call in self.engine.c5_client.batch_buy_calls},
        )

    def test_rebuy_batch_marks_missing_batch_outcomes_unconfirmed_without_retrying(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.50,
            note='{"sourceSellOperationId": 1, "steamListPrice": 2.12}',
        )
        self.engine.c5_client.market_products_search_payload = {
            "list": [{"productId": "product-unknown", "price": 1.20}]
        }
        self.engine.c5_client.batch_buy_payload_by_trade_url[
            self.engine.steam_client.trade_url
        ] = {"successList": [], "failedList": []}

        result = self.engine.run_guadao_rebuy_batch_task("Revolution Case")

        self.assertEqual(1, result["unconfirmed"])
        row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        assert row is not None
        self.assertEqual("c5_submission_unconfirmed", row["status"])
        note = json.loads(row["note"])
        self.assertEqual("c5_batch_submission_unconfirmed", note["c5SubmissionUnconfirmedReason"])
        self.assertEqual("product-unknown", note["c5ProductId"])

    def test_c5_network_error_rebuy_stays_pending_for_retry(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.23,
            note='{"sourceSellOperationId": 1, "steamListPrice": 2.12}',
        )
        result = RebuyResult(
            False,
            True,
            "c5_network_error",
            payload={"error": "C5 request failed: connection reset"},
        )

        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result):
            rebought = self.engine._execute_rebuys()

        self.assertEqual(0, rebought)
        row = self.db.conn.execute("SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        assert row is not None
        self.assertEqual("pending", row["status"])
        self.assertIn('"lastSkipReason": "c5_network_error"', row["note"])
        self.assertIn('"c5ErrorPayload": {"error": "C5 request failed: connection reset"}', row["note"])

    def test_rebuy_uses_frozen_open_ratio_instead_of_current_global_ratio(self) -> None:
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.23,
            note=(
                '{"sourceSellOperationId": 1, "steamListPrice": 2.12, '
                '"maxRebuyRatioAtOpen": 0.62, "guadaoMaxListingRatioAtOpen": 0.69, '
                '"steamNetFactorAtOpen": 0.869, "c5SubmissionNotCreatedCount": 2}'
            ),
        )
        submitted_at = datetime.now(timezone.utc) - timedelta(hours=1)
        result = RebuyResult(
            True,
            False,
            "ok",
            actual_price=1.23,
            payload={"orderAssetId": "asset-ratio", "orderId": "trade-ratio", "payStatus": 1},
            submitted_at=submitted_at.isoformat(),
        )

        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result) as rebuy_mock:
            rebought = self.engine._execute_rebuys()

        self.assertEqual(1, rebought)
        kwargs = rebuy_mock.call_args.kwargs
        self.assertAlmostEqual(0.62, float(kwargs["guadao_max_listing_ratio"]), places=6)
        self.assertAlmostEqual(0.869, float(kwargs["steam_net_factor"]), places=6)
        persisted = self.db.list_pool_operations_by_type(
            OP_REBUY_C5,
            status="delivery_pending",
            limit=10,
        )[0]
        persisted_note = json.loads(persisted["note"])
        self.assertEqual(submitted_at.isoformat(), persisted_note["c5OrderSubmittedAt"])
        self.assertEqual(
            (submitted_at + timedelta(hours=12)).isoformat(),
            persisted_note["c5DeliveryDeadlineAt"],
        )
        self.assertEqual(0, persisted_note["c5SubmissionNotCreatedCount"])

    def test_manual_refreeze_replaces_original_ratio_price_and_exact_steam_net_amount(self) -> None:
        self.engine.config.guadao_max_listing_ratio = 0.69
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.80,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.00,
                    "listingRatioAtOpen": 0.62,
                    "maxRebuyRatioAtOpen": 0.75,
                    "guadaoMaxListingRatioAtOpen": 0.69,
                    "steamNetFactorAtOpen": 0.869,
                    "replacementForRebuyOperationId": 9,
                    "replacementMaxPrice": 1.60,
                    "manualRebuyRefrozenAt": "2026-07-17T01:02:03+00:00",
                    "manualRebuyRefrozenPrice": 1.80,
                    "manualRebuySteamNetAmount": 2.40,
                }
            ),
        )
        result = RebuyResult(
            True,
            False,
            "ok",
            actual_price=1.78,
            payload={"orderAssetId": "asset-refreeze", "orderId": "trade-refreeze", "payStatus": 1},
            submitted_at="2026-07-17T04:05:06+00:00",
        )

        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result) as rebuy_mock:
            rebought = self.engine._execute_rebuys()

        self.assertEqual(1, rebought)
        kwargs = rebuy_mock.call_args.kwargs
        self.assertAlmostEqual(0.75, float(kwargs["guadao_max_listing_ratio"]), places=6)
        self.assertAlmostEqual(1.80, float(kwargs["max_price_override"]), places=6)
        self.assertAlmostEqual(2.40, float(kwargs["steam_net_amount_override"]), places=6)
        self.assertFalse(kwargs["use_live_price_as_max"])

    def test_manual_refreeze_wait_log_uses_exact_steam_net_amount(self) -> None:
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.80,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.00,
                    "maxRebuyRatioAtOpen": 0.75,
                    "guadaoMaxListingRatioAtOpen": 0.69,
                    "steamNetFactorAtOpen": 0.869,
                    "manualRebuyRefrozenAt": "2026-07-17T01:02:03+00:00",
                    "manualRebuyRefrozenPrice": 1.80,
                    "manualRebuySteamNetAmount": 2.40,
                }
            ),
        )
        result = RebuyResult(
            False,
            True,
            "ratio_no_longer_profitable",
            actual_price=1.81,
            max_price=1.80,
            steam_reference_price=3.00,
            listing_ratio_now=1.81 / 2.40,
        )

        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result), patch(
            "builtins.print"
        ) as print_mock:
            rebought = self.engine._execute_rebuys()

        self.assertEqual(0, rebought)
        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Steam\u5356\u51fa\u7a0e\u540e\u5230\u624b: 2.4", output)
        self.assertNotIn("Steam\u5356\u51fa\u7a0e\u540e\u5230\u624b: 2.607", output)

    def test_failed_c5_delivery_replacement_keeps_original_buy_price_and_dynamic_ratio(self) -> None:
        original_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.55,
            note=(
                '{"sourceSellOperationId": 1, "steamListPrice": 3.00, '
                '"listingRatioAtOpen": 0.62, "maxRebuyRatioAtOpen": 0.62, '
                '"guadaoMaxListingRatioAtOpen": 0.69, "steamNetFactorAtOpen": 0.869, '
                '"c5FinalStatus": "c5_failed", "c5SubmissionNotCreatedCount": 2}'
            ),
        )
        self.db.update_pool_operation(original_id, status="c5_failed", actual_price=1.60)
        original = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?",
            (original_id,),
        ).fetchone()
        assert original is not None

        created = self.engine._create_replacement_rebuy_for_failed_op(
            original,
            json.loads(original["note"]),
        )

        self.assertEqual(1, created)
        replacement = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)[0]
        replacement_note = json.loads(replacement["note"])
        self.assertEqual(1.60, replacement["expected_price"])
        self.assertEqual(1.60, replacement_note["replacementMaxPrice"])
        self.assertEqual("original_failed_order_price", replacement_note["replacementPricePolicy"])
        self.assertFalse(replacement_note["forceRebuyReplacement"])
        self.assertAlmostEqual(0.62, float(replacement_note["maxRebuyRatioAtOpen"]), places=6)
        self.assertEqual(0, replacement_note["c5SubmissionNotCreatedCount"])

    def test_failed_rebuy_replacement_is_atomic_across_concurrent_workers(self) -> None:
        original_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.55,
            note=json.dumps(
                {
                    "sourceSellOperationId": 1,
                    "steamListPrice": 3.00,
                    "maxRebuyRatioAtOpen": 0.62,
                    "steamNetFactorAtOpen": 0.869,
                    "c5FinalStatus": "c5_failed",
                    "c5OrderFailedDesc": "seller delivery failed",
                }
            ),
        )
        self.db.update_pool_operation(original_id, status="c5_failed", actual_price=1.60)
        barrier = threading.Barrier(2)
        results: list[int] = []
        errors: list[BaseException] = []

        def worker() -> None:
            worker_db = Database(self.db_path)
            worker_engine = object.__new__(ExecutionEngine)
            worker_engine.db = worker_db
            try:
                row = worker_db.conn.execute(
                    "SELECT * FROM pool_operations WHERE id = ?", (original_id,)
                ).fetchone()
                assert row is not None
                stale_note = json.loads(row["note"])
                barrier.wait(timeout=5)
                results.append(
                    worker_engine._create_replacement_rebuy_for_failed_op(row, stale_note)
                )
            except BaseException as exc:  # surfaced below with the worker result
                errors.append(exc)
            finally:
                worker_db.close()

        with patch.object(ExecutionEngine, "_emit_guadao_local_event"), patch("builtins.print"):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual([0, 1], sorted(results))
        replacements = [
            row
            for row in self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=20)
            if json.loads(row["note"]).get("replacementForRebuyOperationId") == original_id
        ]
        self.assertEqual(1, len(replacements))
        parent_note = json.loads(
            self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (original_id,)
            ).fetchone()["note"]
        )
        self.assertEqual(replacements[0]["id"], parent_note["replacementRebuyOperationId"])

        result = RebuyResult(
            True,
            False,
            "ok",
            actual_price=1.58,
            payload={"orderAssetId": "asset-replacement", "orderId": "trade-replacement", "payStatus": 1},
        )
        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result) as rebuy_mock:
            rebought = self.engine._execute_rebuys()

        self.assertEqual(1, rebought)
        kwargs = rebuy_mock.call_args.kwargs
        self.assertAlmostEqual(1.60, float(kwargs["max_price_override"]), places=6)
        self.assertAlmostEqual(0.62, float(kwargs["guadao_max_listing_ratio"]), places=6)
        self.assertFalse(kwargs["use_live_price_as_max"])

    def test_legacy_forced_replacement_uses_expected_price_as_safe_cap(self) -> None:
        self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=1.60,
            note=(
                '{"replacementForRebuyOperationId": 9, "forceRebuyReplacement": true, '
                '"steamListPrice": 3.00, "maxRebuyRatioAtOpen": 0.62, '
                '"guadaoMaxListingRatioAtOpen": 0.69, "steamNetFactorAtOpen": 0.869}'
            ),
        )
        result = RebuyResult(
            True,
            False,
            "ok",
            actual_price=1.58,
            payload={"orderAssetId": "asset-legacy", "orderId": "trade-legacy", "payStatus": 1},
        )

        with patch("cs2_assistant.services.executor_engine.execute_rebuy", return_value=result) as rebuy_mock:
            rebought = self.engine._execute_rebuys()

        self.assertEqual(1, rebought)
        kwargs = rebuy_mock.call_args.kwargs
        self.assertAlmostEqual(1.60, float(kwargs["max_price_override"]), places=6)
        self.assertAlmostEqual(0.62, float(kwargs["guadao_max_listing_ratio"]), places=6)
        self.assertFalse(kwargs["use_live_price_as_max"])

    def test_no_matching_rebuy_timeout_does_not_stop_executor(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=20.0,
            note='{"sourceSellOperationId": 1, "steamListPrice": 25.0}',
        )
        op = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)[0]
        self.engine._rebuy_wait_started_at = {
            op_id: datetime.now(timezone.utc) - timedelta(hours=4)
        }
        self.engine._stop_requested = False
        result = RebuyResult(
            False,
            True,
            "no_matching_listing",
            actual_price=20.0,
            steam_price_now=25.0,
            listing_ratio_now=0.8,
        )

        should_stop = self.engine._handle_no_matching_rebuy_timeout(
            op=op,
            note={"sourceSellOperationId": 1, "steamListPrice": 25.0},
            result=result,
        )

        self.assertFalse(should_stop)
        self.assertFalse(self.engine._stop_requested)
        row = self.db.conn.execute("SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        assert row is not None
        self.assertEqual("pending", row["status"])
        self.assertIn('"timeoutReason": "no_matching_listing_timeout"', row["note"])

    def test_canceled_rebuy_creates_replacement(self) -> None:
        self.engine.config.case_max_open_guadao_count = 100
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=20.0,
            note='{"sourceSellOperationId": 1, "steamListPrice": 25.0, "timeoutReason": "no_matching_listing_timeout"}',
        )
        self.db.conn.execute("UPDATE pool_operations SET status = 'canceled' WHERE id = ?", (op_id,))
        self.db.conn.commit()

        replacements = self.engine._check_recent_rebuy_delivery_failures()

        self.assertEqual(1, replacements)
        row = self.db.conn.execute("SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        assert row is not None
        self.assertEqual("failed", row["status"])
        self.assertIn('"replacementReason": "rebuy_canceled"', row["note"])
        pending = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)
        self.assertEqual(1, len(pending))
        self.assertIn(f'"replacementForRebuyOperationId": {op_id}', pending[0]["note"])
        self.assertIn('"replacementReason": "rebuy_canceled"', pending[0]["note"])
        self.assertIn('"sourceSellOperationId": 1', pending[0]["note"])

    def test_skipped_balance_rebuy_without_replacement_returns_to_pending(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=20.0,
            note='{"sourceSellOperationId": 1, "steamListPrice": 25.0, "skipReason": "c5_balance_insufficient"}',
        )
        self.db.update_pool_operation(op_id, status="skipped")

        replacements = self.engine._check_recent_rebuy_delivery_failures()

        self.assertEqual(0, replacements)
        row = self.db.conn.execute("SELECT status, note FROM pool_operations WHERE id = ?", (op_id,)).fetchone()
        assert row is not None
        self.assertEqual("pending", row["status"])
        self.assertIn('"lastSkipReason": "c5_balance_insufficient"', row["note"])
        self.assertIn('"balanceInsufficientRetriedAt":', row["note"])
        pending = self.db.list_pool_operations_by_type(OP_REBUY_C5, status="pending", limit=10)
        self.assertEqual(1, len(pending))
        self.assertEqual(op_id, pending[0]["id"])

    def test_unmarked_failed_rebuy_operation_under_limit_is_not_open(self) -> None:
        self.engine.config.case_max_open_guadao_count = 100
        self.db.set_pool_status("Revolution Case", POOL_STATUS_HOLDING)
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_REBUY_C5,
            expected_price=20.0,
            note='{"sourceSellOperationId": 1, "steamListPrice": 25.0, "failedReason": "c5_api_error: 余额不足"}',
        )
        self.db.update_pool_operation(op_id, status="failed")

        self.assertFalse(self.engine._has_open_guadao_cycle())
        self.assertEqual(0, self.engine._open_case_guadao_count())

    def test_case_open_guadao_at_limit_blocks_and_notifies(self) -> None:
        self.engine.config.case_max_open_guadao_count = 1
        self.engine.serverchan = FakeServerChan()
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0}',
        )
        self.db.update_pool_operation(op_id, status="listed")

        with patch("builtins.print") as print_mock:
            self.assertTrue(self.engine._has_open_guadao_cycle())

        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("1/1", printed)
        self.assertFalse(getattr(self.engine, "_stop_requested", False))
        self.assertEqual(1, len(self.engine.serverchan.messages))

    def _add_case_listing_operation(
        self,
        *,
        market_hash_name: str,
        status: str,
        confirmation_status: str | None = None,
        quantity: int = 1,
    ) -> int:
        note = {"steamListPrice": 25.0}
        if confirmation_status is not None:
            note["confirmationStatus"] = confirmation_status
        operation_id = self.db.add_pool_operation(
            market_hash_name=market_hash_name,
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            quantity=quantity,
            asset_id=f"asset-slot-{market_hash_name}-{status}-{confirmation_status or 'none'}",
            note=json.dumps(note),
        )
        self.db.update_pool_operation(operation_id, status=status)
        return operation_id

    def test_listing_missing_unverified_occupies_case_slot_without_blocking_below_limit(self) -> None:
        self.engine.config.case_max_open_guadao_count = 4
        self.db.upsert_pool_item("Revolution Case", 2, status=POOL_STATUS_LISTED)
        self.db.upsert_pool_item("Kilowatt Case", 1, status=POOL_STATUS_LISTING_PENDING)
        self._add_case_listing_operation(
            market_hash_name="Revolution Case",
            status="listed",
            quantity=2,
        )
        self._add_case_listing_operation(
            market_hash_name="Kilowatt Case",
            status=POOL_STATUS_LISTING_PENDING,
            confirmation_status="listing_missing_unverified",
        )

        self.assertEqual(2, self.engine._open_case_guadao_count())
        self.assertEqual(1, self.engine._listing_missing_unverified_case_guadao_count())
        self.assertEqual(3, self.engine._occupied_case_guadao_slot_count())
        self.assertFalse(self.engine._has_open_guadao_cycle())
        self.assertEqual(
            3,
            self.engine._get_open_guadao_statuses()["case_open_guadao.occupied_slots"],
        )

    def test_listing_missing_unverified_blocks_when_case_risk_slots_reach_limit(self) -> None:
        self.engine.config.case_max_open_guadao_count = 3
        self.db.upsert_pool_item("Revolution Case", 2, status=POOL_STATUS_LISTED)
        self.db.upsert_pool_item("Kilowatt Case", 1, status=POOL_STATUS_LISTING_PENDING)
        self._add_case_listing_operation(
            market_hash_name="Revolution Case",
            status="listed",
            quantity=2,
        )
        self._add_case_listing_operation(
            market_hash_name="Kilowatt Case",
            status=POOL_STATUS_LISTING_PENDING,
            confirmation_status="listing_missing_unverified",
        )

        self.assertEqual(3, self.engine._occupied_case_guadao_slot_count())
        self.assertTrue(self.engine._has_open_guadao_cycle())

    def test_confirm_sent_waiting_uses_risk_slots_without_blocking_below_limit(self) -> None:
        self.engine.config.case_max_open_guadao_count = 300
        self.db.upsert_pool_item("Revolution Case", 177, status=POOL_STATUS_LISTED)
        self.db.upsert_pool_item("Kilowatt Case", 18, status=POOL_STATUS_LISTING_PENDING)
        self._add_case_listing_operation(
            market_hash_name="Revolution Case",
            status="listed",
            quantity=177,
        )
        self._add_case_listing_operation(
            market_hash_name="Kilowatt Case",
            status=POOL_STATUS_LISTING_PENDING,
            confirmation_status="confirm_sent_waiting_active_listing",
            quantity=18,
        )

        self.assertEqual(177, self.engine._open_case_guadao_count())
        self.assertEqual(18, self.engine._confirm_sent_waiting_case_guadao_count())
        self.assertEqual(195, self.engine._occupied_case_guadao_slot_count())
        self.assertFalse(self.engine._has_open_guadao_cycle())
        open_statuses = self.engine._get_open_guadao_statuses()
        self.assertEqual(
            18,
            open_statuses["case_open_guadao.confirm_sent_waiting_active"],
        )
        self.assertEqual(195, open_statuses["case_open_guadao.occupied_slots"])

    def test_other_case_listing_pending_status_remains_hard_block(self) -> None:
        self.engine.config.case_max_open_guadao_count = 150
        self.db.upsert_pool_item("Kilowatt Case", 1, status=POOL_STATUS_LISTING_PENDING)
        self._add_case_listing_operation(
            market_hash_name="Kilowatt Case",
            status=POOL_STATUS_LISTING_PENDING,
            confirmation_status="manual_required",
        )

        self.assertTrue(self.engine._has_open_guadao_cycle())

    def test_orphan_case_listing_pending_pool_status_remains_hard_block(self) -> None:
        self.engine.config.case_max_open_guadao_count = 150
        self.db.upsert_pool_item("Kilowatt Case", 1, status=POOL_STATUS_LISTING_PENDING)

        self.assertTrue(self.engine._has_open_guadao_cycle())

    def test_listing_missing_unverified_does_not_start_remote_full_capacity_release(self) -> None:
        self.engine.config.case_max_open_guadao_count = 2
        self.db.upsert_pool_item("Revolution Case", 1, status=POOL_STATUS_LISTED)
        self.db.upsert_pool_item("Kilowatt Case", 1, status=POOL_STATUS_LISTING_PENDING)
        self._add_case_listing_operation(market_hash_name="Revolution Case", status="listed")
        self._add_case_listing_operation(
            market_hash_name="Kilowatt Case",
            status=POOL_STATUS_LISTING_PENDING,
            confirmation_status="listing_missing_unverified",
        )

        with patch.object(self.engine, "_observe_case_listing_capacity") as observe_mock:
            released = self.engine._release_full_case_listing_capacity()

        self.assertEqual(0, released)
        observe_mock.assert_called_once_with(occupied=1, capacity=2, snapshot_complete=True)

    def test_listing_pending_confirmation_is_recorded_when_credentials_missing(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = None
        self.engine.settings.steam_device_id = None
        self.engine.serverchan = FakeServerChan()
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        self.assertIn('"needsConfirmation": true', sell_op["note"])
        self.assertIn('"confirmationStatus": "manual_required"', sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("listing_pending", asset["status"])
        self.assertEqual(1, self.engine._pending_confirmation_count)
        self.assertEqual(1, len(self.engine.serverchan.messages))

    def test_sellitem_pending_confirmation_error_auto_confirms_to_listed(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-from-asset"
        payload = {
            "success": False,
            "message": "already listed and waiting for confirmation",
        }

        def raise_pending_confirmation(**kwargs: object) -> dict[str, object]:
            raise SteamMarketError("sellitem pending confirmation", payload=payload)

        self.engine._sell_item_with_retry = raise_pending_confirmation  # type: ignore[method-assign]
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("listed", sell_op["status"])
        self.assertIn('"confirmationStatus": "confirmed"', sell_op["note"])
        self.assertIn('"confirmationSource": "sellitem_pending_confirmation"', sell_op["note"])
        self.assertIn('"listingId": "listing-from-asset"', sell_op["note"])
        self.assertIn('"rebuyPrice": 20.0', sell_op["note"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="deferred", limit=10))
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("listed", asset["status"])
        self.assertEqual(1, self.engine.steam_client.confirm_calls)
        self.assertEqual(0, self.engine._pending_confirmation_count)

    def test_sellitem_pending_confirmation_error_marks_sold_when_sold_before_active_seen(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        self.engine.steam_client.sale_receipts_by_asset["asset-old"] = {
            "listingId": "listing-sold",
            "receivedAmount": 21.55,
            "purchaseId": "purchase-1",
            "timeSold": "2026-06-06T00:00:00+00:00",
            "receivedCurrencyId": 23,
        }
        payload = {
            "success": False,
            "message": "already listed and waiting for confirmation",
        }

        def raise_pending_confirmation(**kwargs: object) -> dict[str, object]:
            raise SteamMarketError("sellitem pending confirmation", payload=payload)

        self.engine._sell_item_with_retry = raise_pending_confirmation  # type: ignore[method-assign]
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("sold", sell_op["status"])
        self.assertIn('"listingId": "listing-sold"', sell_op["note"])
        self.assertIn('"steamSellerNetPriceSource": "steam_history"', sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("sold", asset["status"])
        rebuy_ops = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual(1, len(rebuy_ops))
        self.assertEqual("pending", rebuy_ops[0]["status"])
        self.assertEqual(1, self.engine.steam_client.confirm_calls)
        self.assertEqual(0, self.engine._pending_confirmation_count)

    def test_existing_deferred_pending_confirmation_auto_confirms_before_cooldown(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-from-asset"
        pending_error = SteamMarketError(
            "already listed and waiting for confirmation",
            payload={"success": False, "message": "already listed and waiting for confirmation"},
        )
        self.engine._record_listing_transient_defer(
            candidate=build_guadao_candidate(),
            asset_id="asset-old",
            price=25.0,
            account=None,
            steam_id64=self.engine.steam_client.steam_id64,
            error=pending_error,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        self.assertEqual([], self.engine.steam_client.sell_calls)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("listed", sell_op["status"])
        self.assertIn('"confirmationStatus": "confirmed"', sell_op["note"])
        self.assertIn('"confirmationSource": "sellitem_pending_confirmation"', sell_op["note"])
        self.assertIn('"listingId": "listing-from-asset"', sell_op["note"])
        self.assertIn('"rebuyPrice": 20.0', sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("listed", asset["status"])
        self.assertEqual(1, self.engine.steam_client.confirm_calls)

    def test_nontransient_listing_failure_is_recorded_and_retried_after_cooldown(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.62,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        sell_attempts = 0

        def raise_listing_failure(**kwargs: object) -> dict[str, object]:
            nonlocal sell_attempts
            sell_attempts += 1
            raise SteamMarketError("nontransient sellitem failure")

        self.engine._sell_item_with_retry = raise_listing_failure  # type: ignore[method-assign]
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        with patch("builtins.print"):
            first_listed = self.engine._execute_guadao_listings(
                report,
                {"Revolution Case": POOL_STATUS_HOLDING},
            )
            second_listed = self.engine._execute_guadao_listings(
                report,
                {"Revolution Case": POOL_STATUS_HOLDING},
            )

        self.assertEqual(0, first_listed)
        self.assertEqual(0, second_listed)
        self.assertEqual(1, sell_attempts)
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("available", asset["status"])
        deferred = self.db.list_pool_operations_by_type(OP_SELL_STEAM, status="deferred", limit=10)
        self.assertEqual(1, len(deferred))
        deferred_note = json.loads(deferred[0]["note"])
        self.assertEqual("sellitem_failure", deferred_note["deferReason"])
        self.assertIn("nontransient sellitem failure", deferred_note["deferMessage"])
        self.assertIsNotNone(self.engine._active_listing_defer_state("asset-old"))

    def test_listing_confirm_failure_is_not_silent(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine.steam_client.confirm_should_fail = True
        self.engine.steam_client.pending_listing_assets["asset-old"] = "listing-1"
        self.engine.serverchan = FakeServerChan()
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        self.assertIn('"confirmationStatus": "failed"', sell_op["note"])
        self.assertIn('confirm boom', sell_op["note"])
        self.assertEqual(1, self.engine._pending_confirmation_count)
        self.assertEqual(1, len(self.engine.serverchan.messages))

    def test_listing_auto_confirm_marks_listed(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()
        self.engine.steam_client.active_listing_ids = {"listing-1"}

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTED)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("listed", sell_op["status"])
        self.assertIn('"confirmationStatus": "confirmed_late"', sell_op["note"])
        self.assertIn('"activeVerifiedAt":', sell_op["note"])
        self.assertEqual(0, self.engine.steam_client.confirm_calls)
        self.assertEqual([], self.engine.steam_client.confirm_asset_calls)
        self.assertEqual(0, self.engine._pending_confirmation_count)

    def test_listing_auto_confirm_waits_until_listing_is_active(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()
        self.engine.steam_client.pending_listing_assets["asset-old"] = "listing-1"

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        sell_note = json.loads(sell_op["note"])
        self.assertEqual(
            "confirm_sent_waiting_active_listing",
            sell_note["confirmationStatus"],
        )
        self.assertIn("confirmationSentAt", sell_note)
        self.assertEqual(sell_note["confirmationSentAt"], sell_note["listingPendingAt"])
        self.assertEqual(1, self.engine._pending_confirmation_count)

    def test_listing_without_found_confirmation_is_marked_pending(self) -> None:
        self.engine.config.dry_run = False
        self.engine.config.force_refresh_before_execution = False
        self.engine.settings.steam_identity_secret = "secret"
        self.engine.settings.steam_device_id = "device"
        self.engine.steam_client.confirm_result = 0
        self.engine.steam_client.pending_listing_assets["asset-old"] = "listing-1"
        self.engine.serverchan = FakeServerChan()
        self.engine._decide_listing = lambda candidate: ListingDecision(  # type: ignore[method-assign]
            list_price=25.0,
            listing_ratio=0.92,
            transfer_real_ratio=0.07,
            pricing=None,
        )
        report = type("Report", (), {"guadao_candidates": [build_guadao_candidate()]})()

        listed = self.engine._execute_guadao_listings(report, {"Revolution Case": POOL_STATUS_HOLDING})

        self.assertEqual(1, listed)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        self.assertIn('"confirmationStatus": "not_found"', sell_op["note"])
        self.assertEqual(1, self.engine._pending_confirmation_count)
        self.assertEqual(1, len(self.engine.serverchan.messages))

    def test_pending_confirmation_recovers_when_listing_appears_active(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","needsConfirmation":true,"confirmationStatus":"not_found"}',
        )
        self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
        self.engine.steam_client.active_listing_ids = {"listing-1"}

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(1, updated)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTED)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("listed", sell_op["status"])
        self.assertIn('"confirmationStatus": "confirmed_late"', sell_op["note"])
        self.assertIn('"activeVerifiedAt":', sell_op["note"])

    def test_pending_confirmation_retries_guard_before_staying_pending(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","needsConfirmation":true,"confirmationStatus":"not_found"}',
        )
        self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)

        def confirm_and_activate(*, asset_ids: object, listing_ids: object | None = None) -> int:
            self.engine.steam_client.confirm_calls += 1
            self.engine.steam_client.confirm_asset_calls.append(
                {"asset_ids": asset_ids, "listing_ids": listing_ids}
            )
            self.engine.steam_client.active_listing_ids.add("listing-1")
            return 1

        self.engine.steam_client.confirm_listing_assets = confirm_and_activate

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(1, updated)
        self.assertEqual(1, self.engine.steam_client.confirm_calls)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("listed", sell_op["status"])
        self.assertIn('"confirmationStatus": "confirmed_late"', sell_op["note"])
        self.assertIn('"confirmationRetryCount": 1', sell_op["note"])

    def test_account_sync_shares_active_snapshot_and_filters_due_confirmations(self) -> None:
        operation_ids: list[int] = []
        for index in range(2):
            asset_id = f"asset-shared-confirm-{index}"
            listing_id = f"listing-shared-confirm-{index}"
            op_id = self.db.add_pool_operation(
                market_hash_name=f"Shared Confirmation Case {index}",
                strategy=STRATEGY_GUADAO,
                operation_type=OP_SELL_STEAM,
                expected_price=25.0,
                asset_id=asset_id,
                note=json.dumps(
                    {
                        "listingId": listing_id,
                        "steamId64": self.engine.steam_client.steam_id64,
                        "needsConfirmation": True,
                        "confirmationStatus": "not_found",
                    }
                ),
            )
            self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
            self.engine.steam_client.pending_listing_assets[asset_id] = listing_id
            operation_ids.append(op_id)

        active_calls = 0
        original_active = self.engine.steam_client.list_active_listings

        def counted_active() -> list[object]:
            nonlocal active_calls
            active_calls += 1
            return original_active()

        def confirm_and_activate(*, asset_ids: object, listing_ids: object | None = None) -> int:
            asset_values = [str(value) for value in asset_ids]
            listing_values = [str(value) for value in (listing_ids or [])]
            self.engine.steam_client.confirm_calls += 1
            for index, asset_id in enumerate(asset_values):
                listing_id = (
                    listing_values[index]
                    if index < len(listing_values)
                    else f"listing-for-{asset_id}"
                )
                self.engine.steam_client.active_listing_assets[asset_id] = listing_id
            return len(asset_values)

        self.engine.steam_client.list_active_listings = counted_active  # type: ignore[method-assign]
        self.engine.steam_client.confirm_listing_assets = confirm_and_activate  # type: ignore[method-assign]

        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids=set(operation_ids),
            sale_operation_ids=set(),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["confirmed"])
        self.assertEqual(1, self.engine.steam_client.confirm_calls)
        self.assertLessEqual(active_calls, 2)
        rows = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id IN (?, ?) ORDER BY id",
            tuple(operation_ids),
        ).fetchall()
        self.assertEqual(["listed", "listed"], [row["status"] for row in rows])

    def test_account_sync_with_no_due_operations_does_not_request_steam(self) -> None:
        with patch.object(
            self.engine.steam_client,
            "list_active_listings",
            side_effect=AssertionError("Steam must not be called without due operations"),
        ):
            result = self.engine.run_guadao_account_sync_task(
                None,
                confirmation_operation_ids=set(),
                sale_operation_ids=set(),
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual("no_due_operations", result["reason"])

    def test_account_sync_queries_sale_evidence_only_for_due_operation(self) -> None:
        operation_ids: list[int] = []
        for index in range(2):
            op_id = self.db.add_pool_operation(
                market_hash_name=f"Due Evidence Case {index}",
                strategy=STRATEGY_GUADAO,
                operation_type=OP_SELL_STEAM,
                expected_price=25.0,
                asset_id=f"asset-due-evidence-{index}",
                note=json.dumps(
                    {
                        "listingId": f"listing-due-evidence-{index}",
                        "steamId64": self.engine.steam_client.steam_id64,
                        "activeVerifiedAt": "2026-01-01T00:00:00+00:00",
                    }
                ),
            )
            self.db.update_pool_operation(op_id, status="listed")
            operation_ids.append(op_id)

        queried: list[str] = []

        def find_receipt(listing_id: str) -> None:
            queried.append(listing_id)
            return None

        self.engine.steam_client.find_sale_receipt = find_receipt  # type: ignore[method-assign]
        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids=set(),
            sale_operation_ids={operation_ids[0]},
        )

        self.assertTrue(result["ok"])
        self.assertIn("listing-due-evidence-0", queried)
        self.assertNotIn("listing-due-evidence-1", queried)
        untouched = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (operation_ids[1],),
        ).fetchone()
        self.assertEqual("listed", untouched["status"])

    def test_account_sync_applies_sale_receipt_to_listing_missing_unverified_operation(self) -> None:
        operation_id = self.db.add_pool_operation(
            market_hash_name="Missing Evidence Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-missing-evidence",
            note=json.dumps(
                {
                    "listingId": "listing-missing-evidence",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "activeVerifiedAt": "2026-01-01T00:00:00+00:00",
                    "confirmationStatus": "listing_missing_unverified",
                    "rebuyPrice": 20.0,
                    "steamListPrice": 25.0,
                }
            ),
        )
        self.db.update_pool_operation(operation_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Missing Evidence Case", POOL_STATUS_LISTING_PENDING)
        self.engine.steam_client.sale_receipts["listing-missing-evidence"] = {
            "receivedAmount": 21.55,
            "purchaseId": "purchase-missing-evidence",
            "timeSold": "2026-07-26T12:00:00+00:00",
            "receivedCurrencyId": 23,
        }

        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids=set(),
            sale_operation_ids={operation_id},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["sold"])
        operation = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        self.assertEqual("sold", operation["status"])
        rebuys = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual(1, len(rebuys))
        self.assertEqual(operation_id, json.loads(rebuys[0]["note"])["sourceSellOperationId"])

    def test_account_sync_restores_listing_missing_unverified_when_listing_reappears(self) -> None:
        operation_id = self.db.add_pool_operation(
            market_hash_name="Reappeared Listing Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-reappeared-listing",
            note=json.dumps(
                {
                    "listingId": "listing-reappeared-listing",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "activeVerifiedAt": "2026-01-01T00:00:00+00:00",
                    "confirmationStatus": "listing_missing_unverified",
                }
            ),
        )
        self.db.update_pool_operation(operation_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Reappeared Listing Case", POOL_STATUS_LISTING_PENDING)
        self.engine.steam_client.active_listing_ids.add("listing-reappeared-listing")
        self.engine.steam_client.confirm_listing_assets = (  # type: ignore[method-assign]
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("sale-evidence recheck must not send a mobile confirmation")
            )
        )

        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids=set(),
            sale_operation_ids={operation_id},
        )

        self.assertTrue(result["ok"])
        operation = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        self.assertEqual("listed", operation["status"])
        self.assertEqual(
            "listing_active_reverified",
            json.loads(operation["note"])["confirmationStatus"],
        )

    def test_account_sync_reports_sale_evidence_lookup_failure(self) -> None:
        operation_id = self.db.add_pool_operation(
            market_hash_name="History Failure Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-history-failure",
            note=json.dumps(
                {
                    "listingId": "listing-history-failure",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "activeVerifiedAt": "2026-01-01T00:00:00+00:00",
                    "confirmationStatus": "listing_missing_unverified",
                }
            ),
        )
        self.db.update_pool_operation(operation_id, status=POOL_STATUS_LISTING_PENDING)
        self.engine.steam_client.find_sale_receipts_for_targets = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: (_ for _ in ()).throw(
                RuntimeError(f"Steam history HTTP 429 after {max_pages} pages")
            )
        )

        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids=set(),
            sale_operation_ids={operation_id},
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(1, result["historyDeferred"])
        self.assertIn("Steam history HTTP 429", result["historyError"])
        operation = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        self.assertEqual(POOL_STATUS_LISTING_PENDING, operation["status"])

    def test_account_sync_commits_active_listing_before_history_failure(self) -> None:
        active_operation_id = self.db.add_pool_operation(
            market_hash_name="Second Page Active Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-second-page-active",
            note=json.dumps(
                {
                    "listingId": "listing-second-page-active",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "confirmationStatus": "confirm_sent_waiting_active_listing",
                }
            ),
        )
        missing_operation_id = self.db.add_pool_operation(
            market_hash_name="History Still Missing Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-history-still-missing",
            note=json.dumps(
                {
                    "listingId": "listing-history-still-missing",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "confirmationStatus": "listing_missing_unverified",
                }
            ),
        )
        self.db.update_pool_operation(active_operation_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.update_pool_operation(missing_operation_id, status=POOL_STATUS_LISTING_PENDING)
        self.engine.steam_client.active_listing_assets[
            "asset-second-page-active"
        ] = "listing-second-page-active"
        history_targets: list[list[dict[str, str]]] = []

        def fail_history(
            targets: list[dict[str, str]],
            *,
            max_pages: int,
        ) -> dict[str, dict[str, object]]:
            del max_pages
            history_targets.append(list(targets))
            raise RuntimeError("Steam history HTTP 429 after first useful page")

        self.engine.steam_client.find_sale_receipts_for_targets = fail_history  # type: ignore[attr-defined]

        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids={active_operation_id},
            sale_operation_ids={missing_operation_id},
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(1, result["myListingsResolved"])
        self.assertEqual(1, result["historyDeferred"])
        self.assertEqual(
            {str(missing_operation_id)},
            {target["key"] for target in history_targets[0]},
        )
        active_row = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?",
            (active_operation_id,),
        ).fetchone()
        missing_row = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (missing_operation_id,),
        ).fetchone()
        self.assertEqual("listed", active_row["status"])
        self.assertIn("activeVerifiedAt", json.loads(active_row["note"]))
        self.assertEqual(POOL_STATUS_LISTING_PENDING, missing_row["status"])

    def test_incomplete_mylistings_uses_positive_rows_but_never_queries_history(self) -> None:
        active_operation_id = self.db.add_pool_operation(
            market_hash_name="Partial Snapshot Active Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-partial-snapshot-active",
            note=json.dumps(
                {
                    "listingId": "listing-partial-snapshot-active",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "confirmationStatus": "confirm_sent_waiting_active_listing",
                }
            ),
        )
        missing_operation_id = self.db.add_pool_operation(
            market_hash_name="Partial Snapshot Missing Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-partial-snapshot-missing",
            note=json.dumps(
                {
                    "listingId": "listing-partial-snapshot-missing",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "confirmationStatus": "listing_missing_unverified",
                }
            ),
        )
        self.db.update_pool_operation(active_operation_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.update_pool_operation(missing_operation_id, status=POOL_STATUS_LISTING_PENDING)
        self.engine.steam_client.my_listings_snapshot = lambda: SteamMyListingsSnapshot(  # type: ignore[attr-defined]
            active_listings=(
                SteamListing(
                    "listing-partial-snapshot-active",
                    "asset-partial-snapshot-active",
                    "Partial Snapshot Active Case",
                    25.0,
                    1,
                ),
            ),
            pending_listings=(),
            official_active_count=105,
            actual_active_count=100,
            pages_scanned=1,
            complete=False,
            observed_at="2026-07-28T00:00:00+00:00",
            error="second page unavailable",
        )
        self.engine.steam_client.find_sale_receipts_for_targets = (  # type: ignore[attr-defined]
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("incomplete MyListings must not authorize MyHistory")
            )
        )

        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids={active_operation_id},
            sale_operation_ids={missing_operation_id},
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertFalse(result["myListingsComplete"])
        self.assertEqual(1, result["myListingsResolved"])
        self.assertEqual([missing_operation_id], result["historyDeferredOperationIds"])
        active_row = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (active_operation_id,),
        ).fetchone()
        missing_row = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (missing_operation_id,),
        ).fetchone()
        self.assertEqual("listed", active_row["status"])
        self.assertEqual(POOL_STATUS_LISTING_PENDING, missing_row["status"])

    def test_partial_history_applies_found_receipt_and_defers_only_missing_target(self) -> None:
        operation_ids: list[int] = []
        for index in range(2):
            operation_id = self.db.add_pool_operation(
                market_hash_name=f"Partial History Case {index}",
                strategy=STRATEGY_GUADAO,
                operation_type=OP_SELL_STEAM,
                expected_price=25.0,
                asset_id=f"asset-partial-history-{index}",
                note=json.dumps(
                    {
                        "listingId": f"listing-partial-history-{index}",
                        "steamId64": self.engine.steam_client.steam_id64,
                        "confirmationStatus": "listing_missing_unverified",
                        "rebuyPrice": 20.0,
                        "steamListPrice": 25.0,
                    }
                ),
            )
            self.db.update_pool_operation(
                operation_id,
                status=POOL_STATUS_LISTING_PENDING,
            )
            operation_ids.append(operation_id)

        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: SteamSaleReceiptLookupResult(
                {
                    str(operation_ids[0]): {
                        "listingId": "listing-partial-history-0",
                        "purchaseId": "purchase-partial-history-0",
                        "timeSold": "2026-07-28T01:00:00+00:00",
                        "receivedAmount": 21.55,
                        "receivedCurrencyId": 23,
                    }
                },
                False,
                1,
                False,
                f"Steam history HTTP 429 after page 1/{max_pages}",
                "2026-07-28T02:00:00+00:00",
            )
        )

        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids=set(),
            sale_operation_ids=set(operation_ids),
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(1, result["historySold"])
        self.assertEqual(1, result["historyDeferred"])
        self.assertEqual([operation_ids[1]], result["historyDeferredOperationIds"])
        sold = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (operation_ids[0],),
        ).fetchone()
        deferred = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?",
            (operation_ids[1],),
        ).fetchone()
        self.assertEqual("sold", sold["status"])
        self.assertEqual(POOL_STATUS_LISTING_PENDING, deferred["status"])
        self.assertEqual(
            1,
            len(self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)),
        )

    def test_account_sync_batches_confirmation_and_sale_history_for_same_account(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        pending_id = self.db.add_pool_operation(
            market_hash_name="Batch Pending Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-batch-pending",
            note=json.dumps(
                {
                    "listingId": "listing-batch-pending",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "confirmationStatus": "listing_missing_unverified",
                }
            ),
        )
        listed_id = self.db.add_pool_operation(
            market_hash_name="Batch Listed Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-batch-listed",
            note=json.dumps(
                {
                    "listingId": "listing-batch-listed",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "activeVerifiedAt": "2026-01-01T00:00:00+00:00",
                }
            ),
        )
        self.db.update_pool_operation(pending_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.update_pool_operation(listed_id, status="listed")
        self.db.conn.execute(
            "UPDATE pool_operations SET created_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), listed_id),
        )
        self.db.conn.commit()
        calls: list[list[dict[str, str]]] = []

        def find_batch(
            targets: list[dict[str, str]],
            *,
            max_pages: int,
        ) -> dict[str, dict[str, object]]:
            calls.append(list(targets))
            self.assertEqual(2, max_pages)
            return {}

        self.engine.steam_client.find_sale_receipts_for_targets = find_batch  # type: ignore[attr-defined]
        self.engine.steam_client.find_sale_receipt = (  # type: ignore[method-assign]
            lambda _listing_id: (_ for _ in ()).throw(
                AssertionError("per-operation listing history lookup must not run")
            )
        )
        self.engine.steam_client.find_sale_receipt_by_asset = (  # type: ignore[method-assign]
            lambda _asset_id: (_ for _ in ()).throw(
                AssertionError("per-operation asset history lookup must not run")
            )
        )

        result = self.engine.run_guadao_account_sync_task(
            None,
            confirmation_operation_ids={pending_id},
            sale_operation_ids={listed_id},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual(
            {str(pending_id), str(listed_id)},
            {target["key"] for target in calls[0]},
        )
        pending = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?", (pending_id,)
        ).fetchone()
        listed = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?", (listed_id,)
        ).fetchone()
        self.assertEqual(POOL_STATUS_LISTING_PENDING, pending["status"])
        self.assertEqual(POOL_STATUS_LISTING_PENDING, listed["status"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))

    def test_pending_confirmation_marks_sold_when_listing_sold_before_active_seen(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","needsConfirmation":true,"confirmationStatus":"confirmed","rebuyPrice":20.0,"steamListPrice":25.0}',
        )
        self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
        self.engine.steam_client.sale_receipts["listing-1"] = {
            "receivedAmount": 21.55,
            "purchaseId": "purchase-1",
            "timeSold": "2026-06-06T00:00:00+00:00",
            "receivedCurrencyId": 23,
        }

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(1, updated)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_PENDING_REBUY)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("sold", sell_op["status"])
        self.assertIn('"steamSellerNetPriceSource": "steam_history"', sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("sold", asset["status"])
        rebuy_ops = self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)
        self.assertEqual(1, len(rebuy_ops))
        self.assertEqual("pending", rebuy_ops[0]["status"])

    def test_pending_confirmation_demotes_legacy_listed_op_when_not_active(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","needsConfirmation":true,"confirmationStatus":"confirmed"}',
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
        self.db.set_asset_status("asset-old", "listed")

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(0, updated)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        self.assertIn('"confirmationRetryStatus": "confirmed_waiting_active_listing"', sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("listing_pending", asset["status"])

    def test_aged_confirm_sent_waiting_routes_to_exact_inventory_recovery(self) -> None:
        pending_since = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        delayed_retry_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        legacy_deep_attempt = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note=json.dumps(
                {
                    "listingId": "listing-confirmed-but-missing",
                    "steamId64": self.engine.steam_client.steam_id64,
                    "needsConfirmation": True,
                    "confirmationStatus": "confirm_sent_waiting_active_listing",
                    "listingPendingAt": delayed_retry_timestamp,
                    "saleEvidenceDeepLastAttemptAt": legacy_deep_attempt,
                }
            ),
        )
        self.db.conn.execute(
            "UPDATE pool_operations SET created_at = ? WHERE id = ?",
            (pending_since, op_id),
        )
        self.db.conn.commit()
        self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
        self.db.set_asset_status("asset-old", "listing_pending")
        self.engine.steam_client.confirm_result = 0
        self.engine.steam_client.find_sale_receipts_for_targets_with_coverage = (  # type: ignore[attr-defined]
            lambda _targets, *, max_pages: SteamSaleReceiptLookupResult({}, False, max_pages)
        )
        inventory_calls: list[list[str]] = []

        def find_inventory(asset_ids: list[str]) -> SteamInventoryAssetLookupResult:
            inventory_calls.append(list(asset_ids))
            return SteamInventoryAssetLookupResult(frozenset({"asset-old"}), False, 1)

        self.engine.steam_client.find_inventory_asset_ids = find_inventory  # type: ignore[attr-defined]
        active_listings = self.engine.steam_client.list_active_listings()
        active_listing_ids, active_asset_ids = self.engine._active_listing_identity_sets(
            active_listings
        )
        operation = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?",
            (op_id,),
        ).fetchone()
        assert operation is not None
        receipt_lookup = self.engine._lookup_steam_sale_receipts_for_operations(
            self.engine.steam_client,
            [operation],
            active_listing_ids=active_listing_ids,
            active_asset_ids=active_asset_ids,
        )
        self.assertEqual(set(), receipt_lookup.deep_attempt_ids)

        self.engine._refresh_pending_listing_confirmations(
            active_listings=active_listings,
            operation_ids={op_id},
            sale_receipt_results=receipt_lookup.receipts,
            sale_receipt_deep_attempt_ids=receipt_lookup.deep_attempt_ids,
            sale_receipt_deep_attempted_at=receipt_lookup.deep_attempted_at,
        )
        self.engine._refresh_listings(
            active_listings=active_listings,
            operation_ids={op_id},
            sale_receipt_results=receipt_lookup.receipts,
            sale_receipt_deep_attempt_ids=receipt_lookup.deep_attempt_ids,
            sale_receipt_deep_attempted_at=receipt_lookup.deep_attempted_at,
            sale_receipt_lookup_succeeded=receipt_lookup.lookup_succeeded,
            sale_receipt_coverage_complete=receipt_lookup.coverage_complete,
        )

        operation = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?",
            (op_id,),
        ).fetchone()
        assert operation is not None
        note = json.loads(operation["note"])
        self.assertEqual([["asset-old"]], inventory_calls)
        self.assertEqual("canceled", operation["status"])
        self.assertEqual(
            "confirm_sent_waiting_active_listing",
            note["listingMissingRecoveryFrom"],
        )
        self.assertEqual("official_steam_inventory_same_asset", note["terminalEvidence"])
        self.assertEqual("available", self.db.get_asset("asset-old")["status"])
        self.assertEqual(POOL_STATUS_HOLDING, self.db.get_pool_status_map()["Revolution Case"])

    def test_pending_confirmation_does_not_release_from_c5_cached_inventory(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 60  # type: ignore[method-assign]
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","needsConfirmation":true,"confirmationStatus":"not_found"}',
        )
        old_created_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.db.conn.execute("UPDATE pool_operations SET created_at = ? WHERE id = ?", (old_created_at, op_id))
        self.db.conn.commit()
        self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
        self.db.set_asset_status("asset-old", "listing_pending")
        self.engine.steam_client.confirm_result = 0

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(0, updated)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        self.assertNotIn("stalePendingReleaseReason", sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("listing_pending", asset["status"])
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))

    def test_listing_missing_recheck_preserves_first_pending_timestamp(self) -> None:
        first_pending_at = (
            datetime.now(timezone.utc) - timedelta(minutes=31)
        ).isoformat()
        market_hash_name = "Preserved Pending Timestamp Case"
        asset_id = "asset-preserved-pending-timestamp"
        self.db.upsert_pool_item(
            market_hash_name,
            1,
            status=POOL_STATUS_LISTING_PENDING,
        )
        self.db.upsert_inventory_assets(
            [
                {
                    "assetId": asset_id,
                    "marketHashName": market_hash_name,
                    "steamId": self.engine.steam_client.steam_id64,
                    "ifTradable": True,
                }
            ]
        )
        operation_id = self.db.add_pool_operation(
            market_hash_name=market_hash_name,
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id=asset_id,
            note=json.dumps(
                {
                    "listingId": "listing-preserved-pending-timestamp",
                    "confirmationStatus": "listing_missing_unverified",
                    "listingPendingAt": first_pending_at,
                }
            ),
        )
        self.db.update_pool_operation(
            operation_id,
            status=POOL_STATUS_LISTING_PENDING,
        )
        operation = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        assert operation is not None
        note = json.loads(operation["note"])

        self.engine._mark_steam_listing_pending(
            operation,
            note,
            reason="listing_missing_unverified",
        )

        updated = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        assert updated is not None
        updated_note = json.loads(updated["note"])
        self.assertEqual(first_pending_at, updated_note["listingPendingAt"])
        self.assertTrue(
            self.engine._steam_sale_receipt_deep_lookup_due(
                updated,
                updated_note,
                now=datetime.now(timezone.utc),
            )
        )

    def test_listing_missing_uses_fast_then_low_frequency_bounded_deep_batch(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        now = datetime.now(timezone.utc)
        operation_ids: list[int] = []
        for index in range(2):
            market_hash_name = f"Delayed History Case {index}"
            asset_id = f"asset-delayed-history-{index}"
            listing_id = f"listing-delayed-history-{index}"
            self.db.upsert_pool_item(market_hash_name, 1, status=POOL_STATUS_LISTING_PENDING)
            self.db.upsert_inventory_assets(
                [
                    {
                        "assetId": asset_id,
                        "marketHashName": market_hash_name,
                        "steamId": self.engine.steam_client.steam_id64,
                        "ifTradable": True,
                    }
                ]
            )
            self.db.set_asset_status(asset_id, "listing_pending")
            op_id = self.db.add_pool_operation(
                market_hash_name=market_hash_name,
                strategy=STRATEGY_GUADAO,
                operation_type=OP_SELL_STEAM,
                expected_price=25.0,
                asset_id=asset_id,
                note=json.dumps(
                    {
                        "listingId": listing_id,
                        "rebuyPrice": 20.0,
                        "steamListPrice": 25.0,
                        "confirmationStatus": "listing_missing_unverified",
                        "listingPendingAt": now.isoformat(),
                    }
                ),
            )
            self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
            operation_ids.append(op_id)

        calls: list[dict[str, object]] = []

        def find_batch(
            targets: list[dict[str, str]],
            *,
            max_pages: int,
        ) -> dict[str, dict[str, object]]:
            calls.append({"targets": targets, "max_pages": max_pages})
            if max_pages < 3:
                return {}
            return {
                str(operation_ids[0]): {
                    "listingId": "listing-delayed-history-0",
                    "purchaseId": "purchase-on-page-3",
                    "timeSold": "2026-07-16T03:04:05+00:00",
                    "receivedAmount": 21.73,
                }
            }

        self.engine.steam_client.find_sale_receipts_for_targets = find_batch  # type: ignore[attr-defined]

        first_updated = self.engine._refresh_pending_listing_confirmations(
            operation_ids=set(operation_ids)
        )

        self.assertEqual(0, first_updated)
        self.assertEqual([2], [call["max_pages"] for call in calls])
        self.assertEqual(2, len(calls[0]["targets"]))
        for operation_id in operation_ids:
            row = self.db.conn.execute(
                "SELECT status FROM pool_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            self.assertEqual(POOL_STATUS_LISTING_PENDING, row["status"])

        deep_due_at = (now - timedelta(minutes=31)).isoformat()
        for operation_id in operation_ids:
            row = self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            note = json.loads(row["note"])
            note["listingPendingAt"] = deep_due_at
            self.db.update_pool_operation(operation_id, note=json.dumps(note))

        second_updated = self.engine._refresh_pending_listing_confirmations(
            operation_ids=set(operation_ids)
        )

        self.assertEqual(1, second_updated)
        self.assertEqual([2, 30], [call["max_pages"] for call in calls])
        sold = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?", (operation_ids[0],)
        ).fetchone()
        still_waiting = self.db.conn.execute(
            "SELECT status, note FROM pool_operations WHERE id = ?", (operation_ids[1],)
        ).fetchone()
        self.assertEqual("sold", sold["status"])
        self.assertEqual(POOL_STATUS_LISTING_PENDING, still_waiting["status"])
        self.assertIn("saleEvidenceDeepLastAttemptAt", still_waiting["note"])
        self.assertEqual("listing_pending", self.db.get_asset("asset-delayed-history-1")["status"])
        self.assertEqual(1, len(self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)))

        third_updated = self.engine._refresh_pending_listing_confirmations(
            operation_ids={operation_ids[1]}
        )

        self.assertEqual(0, third_updated)
        self.assertEqual([2, 30, 2], [call["max_pages"] for call in calls])
        still_waiting = self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?", (operation_ids[1],)
        ).fetchone()
        self.assertEqual(POOL_STATUS_LISTING_PENDING, still_waiting["status"])
        self.assertEqual("listing_pending", self.db.get_asset("asset-delayed-history-1")["status"])

    def test_pending_confirmation_removes_market_pending_listing_when_mobileconf_empty(self) -> None:
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"needsConfirmation":true,"confirmationStatus":"not_found"}',
        )
        self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
        self.db.set_asset_status("asset-old", "listing_pending")
        self.engine.steam_client.confirm_result = 0
        self.engine.steam_client.pending_listing_assets["asset-old"] = "pending-listing-1"

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(0, updated)
        self.assertEqual(["pending-listing-1"], self.engine.steam_client.removed_listing_ids)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("canceled", sell_op["status"])
        self.assertIn('"confirmationStatus": "market_pending_removed"', sell_op["note"])
        self.assertIn('"marketPendingListingId": "pending-listing-1"', sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("available", asset["status"])

    def test_sellitem_pending_message_is_not_released_by_c5_relistable_state(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 60  # type: ignore[method-assign]
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"needsConfirmation":true,"confirmationStatus":"not_found","sellitemPendingMessage":"already listed and waiting confirmation"}',
        )
        old_created_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.db.conn.execute("UPDATE pool_operations SET created_at = ? WHERE id = ?", (old_created_at, op_id))
        self.db.conn.commit()
        self.db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
        self.db.set_asset_status("asset-old", "listing_pending")
        self.engine.steam_client.confirm_result = 0

        updated = self.engine._refresh_pending_listing_confirmations()

        self.assertEqual(0, updated)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        self.assertIn('"confirmationRetryStatus": "not_found"', sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("listing_pending", asset["status"])

    def test_unverified_listed_operation_is_repaired_to_listing_pending_not_sold(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 0  # type: ignore[method-assign]
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0,"confirmationStatus":"failed"}',
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.db.set_asset_status("asset-old", "listed")

        with patch("builtins.print") as print_mock:
            sold = self.engine._refresh_listings()

        self.assertEqual(0, sold)
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual(POOL_STATUS_LISTING_PENDING, sell_op["status"])
        self.assertIn('"confirmationStatus": "listing_missing_unverified"', sell_op["note"])
        asset = self.db.get_asset("asset-old")
        assert asset is not None
        self.assertEqual("listing_pending", asset["status"])
        self.assertEqual([], self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10))
        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("未判定为卖出", printed)

    def test_listed_operation_with_sale_receipt_ignores_listing_wait_window(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 999 * 60  # type: ignore[method-assign]
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","rebuyPrice":20.0,"steamListPrice":25.0}',
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.engine.steam_client.sale_receipts["listing-1"] = {
            "receivedAmount": 21.55,
            "purchaseId": "purchase-1",
        }

        sold = self.engine._refresh_listings()

        self.assertEqual(1, sold)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        self.assertEqual("sold", sell_op["status"])
        self.assertIn('"steamSellerNetPriceSource": "steam_history"', sell_op["note"])
        self.assertEqual(1, len(self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)))

    def test_listed_operation_with_sale_receipt_persists_time_without_received_amount(self) -> None:
        self.engine._minimum_action_confirmation_seconds = lambda: 999 * 60  # type: ignore[method-assign]
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note=(
                '{"listingId":"listing-1","rebuyPrice":20.0,'
                '"steamListPrice":25.0,"steamSellerNetPrice":21.55}'
            ),
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.engine.steam_client.sale_receipts["listing-1"] = {
            "purchaseId": "purchase-1",
            "timeSold": "2026-06-06T00:00:00+00:00",
            "receivedCurrencyId": "2023",
        }

        sold = self.engine._refresh_listings()

        self.assertEqual(1, sold)
        sell_op = self.db.list_pool_operations_by_type(OP_SELL_STEAM, limit=10)[0]
        note = json.loads(sell_op["note"])
        self.assertEqual("sold", sell_op["status"])
        self.assertEqual("2026-06-06T00:00:00+00:00", note["steamSoldAt"])
        self.assertEqual("purchase-1", note["steamPurchaseId"])
        self.assertEqual("2023", note["steamHistoryCurrencyId"])
        self.assertNotEqual("steam_history", note.get("steamSellerNetPriceSource"))
        self.assertEqual(1, len(self.db.list_pool_operations_by_type(OP_REBUY_C5, limit=10)))

    @patch("cs2_assistant.services.executor_engine.scan_strategies")
    @patch("cs2_assistant.services.executor_engine.time.sleep")
    def test_run_once_does_not_block_for_guadao_cycle_to_close(self, sleep_mock: object, scan_mock: object) -> None:
        self.engine._sync_assets = lambda: None  # type: ignore[method-assign]
        self.engine._refresh_transfer_holdings = lambda: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_buys = lambda report, status_map: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_sells = lambda: 0  # type: ignore[method-assign]
        self.engine._refresh_transfer_sales = lambda: 0  # type: ignore[method-assign]

        report = type(
            "Report",
            (),
            {
                "guadao_candidates": [build_guadao_candidate()],
                "transfer_candidates": [],
            },
        )()
        scan_mock.return_value = report

        state = {"advance_calls": 0}

        def fake_execute(report_obj: object, status_map: dict[str, str]) -> int:
            self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTING_PENDING)
            return 1

        def fake_advance() -> tuple[int, int]:
            state["advance_calls"] += 1
            if state["advance_calls"] >= 2:
                self.db.set_pool_status("Revolution Case", POOL_STATUS_HOLDING)
                return (1, 1)
            return (0, 0)

        self.engine._execute_guadao_listings = fake_execute  # type: ignore[method-assign]
        self.engine._backfill_listing_ids = lambda: 0  # type: ignore[method-assign]
        self.engine._advance_guadao_cycle = fake_advance  # type: ignore[method-assign]

        self.engine.run_once(wait_for_cycle=True)

        self.assertEqual(0, sleep_mock.call_count)
        self.assertEqual(1, state["advance_calls"])
        pool_row = self.db.list_pool_items(status=POOL_STATUS_LISTING_PENDING)[0]
        self.assertEqual("Revolution Case", pool_row["market_hash_name"])

    @patch("cs2_assistant.services.executor_engine.scan_strategies")
    def test_run_once_reports_when_pool_item_missing_from_real_inventory(self, scan_mock: object) -> None:
        self.engine._sync_assets = lambda: None  # type: ignore[method-assign]
        self.engine._refresh_transfer_holdings = lambda: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_buys = lambda report, status_map: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_sells = lambda: 0  # type: ignore[method-assign]
        self.engine._refresh_transfer_sales = lambda: 0  # type: ignore[method-assign]
        self.engine._last_inventory_payload = {"list": []}
        self.db.upsert_pool_item("Fracture Case", 1, status=POOL_STATUS_HOLDING)

        report = type(
            "Report",
            (),
            {
                "all_evaluated": [],
                "guadao_candidates": [],
                "transfer_candidates": [],
                "guadao_count": 0,
                "transfer_count": 0,
                "missing_price_count": 0,
                "total_pool_types": 2,
            },
        )()
        scan_mock.return_value = report

        with patch("builtins.print") as print_mock:
            self.engine.run_once(wait_for_cycle=False)

        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("[扫描] 底仓池 2 个品种 | 进入评估 0 个 | 缺价 0 个 | 挂刀候选 0 个 | 导余额候选 0 个", printed)
        self.assertIn("[结果] 本轮只完成了扫描/状态检查，没有实际上架、买入或卖出。", printed)
        self.assertIn("当前真实库存里不存在", printed)
        self.assertIn("Fracture Case、Revolution Case", printed)

    @patch("cs2_assistant.services.executor_engine.scan_strategies")
    def test_run_once_reports_open_guadao_cycle_without_actions(self, scan_mock: object) -> None:
        self.engine._sync_assets = lambda: None  # type: ignore[method-assign]
        self.engine._refresh_transfer_holdings = lambda: 0  # type: ignore[method-assign]
        self.engine._advance_guadao_cycle = lambda: (0, 0)  # type: ignore[method-assign]
        self.engine._execute_transfer_buys = lambda report, status_map: 0  # type: ignore[method-assign]
        self.engine._execute_transfer_sells = lambda: 0  # type: ignore[method-assign]
        self.engine._refresh_transfer_sales = lambda: 0  # type: ignore[method-assign]
        self.engine.config.case_max_open_guadao_count = 1
        self.db.set_pool_status("Revolution Case", POOL_STATUS_LISTED)
        self.db.set_asset_status("asset-old", "listed")
        op_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=25.0,
            asset_id="asset-old",
            note='{"listingId":"listing-1","steamListPrice":25.0}',
        )
        self.db.update_pool_operation(op_id, status="listed")
        self.engine._last_inventory_payload = {
            "list": [
                {
                    "assetId": "asset-old",
                    "marketHashName": "Revolution Case",
                }
            ]
        }

        report = type(
            "Report",
            (),
            {
                "all_evaluated": [build_guadao_candidate()],
                "guadao_candidates": [build_guadao_candidate()],
                "transfer_candidates": [],
                "guadao_count": 1,
                "transfer_count": 0,
                "missing_price_count": 0,
                "total_pool_types": 1,
            },
        )()
        scan_mock.return_value = report

        with patch("builtins.print") as print_mock:
            self.engine.run_once(wait_for_cycle=False)

        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("[等待] 检测到挂刀硬阻断状态，或广义箱子风险占用槽已满，本轮暂停新上架。", printed)
        self.assertIn("[结果] 本轮只完成了扫描/状态检查，没有实际上架、买入或卖出。", printed)
        self.assertIn("挂刀执行存在阻塞状态", printed)
        self.assertIn("case_open_guadao.active_listings=1", printed)

    def test_no_action_reason_includes_lowest_listing_ratio_vs_threshold(self) -> None:
        self.engine.config.guadao_max_listing_ratio = 0.67
        report = type(
            "Report",
            (),
            {
                "all_evaluated": [
                    StrategyCandidate(
                        name="Dreams & Nightmares Case",
                        market_hash_name="Dreams & Nightmares Case",
                        inventory_count=2,
                        tradable_count=2,
                        rebuy_price=10.0,
                        rebuy_price_source="c5_batch",
                        steam_sell_price=14.0,
                        steam_price_source="steam_market",
                        steam_after_tax_price=12.17,
                        listing_ratio=0.8211,
                        transfer_real_ratio=-0.02,
                        recommended_strategies=[],
                        steam_accounts=["main-steam"],
                    )
                ],
                "guadao_candidates": [],
                "transfer_candidates": [],
                "missing_price_count": 0,
            },
        )()

        reasons = self.engine._describe_no_action_reasons(report, pool_names=["Dreams & Nightmares Case"])

        joined = "\n".join(reasons)
        self.assertIn("82.11%", joined)
        self.assertIn("67.00%", joined)
        self.assertIn("Dreams & Nightmares Case", joined)

    def test_no_action_reason_reports_account_filtered_ratio_when_threshold_met(self) -> None:
        self.engine.config.guadao_max_listing_ratio = 0.67
        report = type(
            "Report",
            (),
            {
                "all_evaluated": [build_guadao_candidate()],
                "guadao_candidates": [],
                "transfer_candidates": [],
                "missing_price_count": 0,
            },
        )()
        self.engine._guadao_skipped_by_account = [("Revolution Case", 76, 0)]

        reasons = self.engine._describe_no_action_reasons(report, pool_names=["Revolution Case"])

        joined = "\n".join(reasons)
        self.assertIn("67.00%", joined)
        self.assertIn("Revolution Case", joined)

    def test_no_action_reason_reports_all_accounts_have_no_listable_assets(self) -> None:
        self.engine.config.guadao_max_listing_ratio = 0.67
        self.engine.account_store = FakeAccountStore(
            [
                Account(id="a1", name="acc1", steam_id64="111"),
                Account(id="a2", name="acc2", steam_id64="222"),
                Account(id="a3", name="acc3", steam_id64="333"),
                Account(id="a4", name="acc4", steam_id64="444"),
            ]
        )
        self.db.set_asset_status("asset-old", "listed")
        candidate = build_guadao_candidate()
        candidate.listing_ratio = 0.60
        report = type(
            "Report",
            (),
            {
                "all_evaluated": [candidate],
                "guadao_candidates": [candidate],
                "transfer_candidates": [],
                "missing_price_count": 0,
            },
        )()

        reasons = self.engine._describe_no_action_reasons(report, pool_names=["Revolution Case"])

        joined = "\n".join(reasons)
        self.assertIn("4 个已配置 Steam 账号", joined)
        self.assertIn("本地可上架资产都是 0", joined)

    def test_no_action_reason_reports_best_local_available_ratio(self) -> None:
        self.engine.config.guadao_max_listing_ratio = 0.67
        self.engine.account_store = FakeAccountStore(
            [Account(id="a1", name="acc1", steam_id64=self.engine.steam_client.steam_id64)]
        )
        candidate = build_guadao_candidate()
        candidate.listing_ratio = 0.6753
        report = type(
            "Report",
            (),
            {
                "all_evaluated": [candidate],
                "guadao_candidates": [candidate],
                "transfer_candidates": [],
                "missing_price_count": 0,
            },
        )()

        reasons = self.engine._describe_no_action_reasons(report, pool_names=["Revolution Case"])

        joined = "\n".join(reasons)
        self.assertIn("当前账号可上架的最低品类是 Revolution Case", joined)
        self.assertIn("67.53%", joined)
        self.assertIn("高于阈值 67.00%", joined)

    def test_print_guadao_account_inventory_summary_shows_candidate_distribution(self) -> None:
        self.engine.config.guadao_max_listing_ratio = 0.67
        self.engine.account_store = FakeAccountStore(
            [
                Account(id="a1", name="acc1", steam_id64=self.engine.steam_client.steam_id64),
                Account(id="a2", name="acc2", steam_id64="222"),
            ]
        )
        candidate = build_guadao_candidate()
        candidate.listing_ratio = 0.60
        report = type("Report", (), {"guadao_candidates": [candidate]})()

        with patch("builtins.print") as print_mock:
            self.engine._print_guadao_account_inventory_summary(report)

        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("[账号库存] 挂刀候选 1 个", printed)
        self.assertIn("可上架 Revolution Case", printed)
        self.assertIn("acc1 1件", printed)

    def test_print_guadao_account_inventory_summary_hides_unconfigured_assets(self) -> None:
        self.engine.config.guadao_max_listing_ratio = 0.67
        self.engine.account_store = FakeAccountStore(
            [
                Account(id="a1", name="acc1", steam_id64=self.engine.steam_client.steam_id64),
                Account(id="a2", name="acc2", steam_id64="222"),
            ]
        )
        self.db.upsert_inventory_assets(
            [
                {
                    "assetId": "asset-unconfigured",
                    "marketHashName": "Revolution Case",
                    "steamId": "999",
                    "ifTradable": True,
                    "tradableTime": None,
                    "token": "token-unconfigured",
                    "styleToken": "style-unconfigured",
                    "price": 20.0,
                }
            ]
        )
        candidate = build_guadao_candidate()
        candidate.listing_ratio = 0.60
        report = type("Report", (), {"guadao_candidates": [candidate]})()

        with patch("builtins.print") as print_mock:
            self.engine._print_guadao_account_inventory_summary(report)

        printed = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("acc1 1", printed)
        self.assertIn("本地可上架 1", printed)
        self.assertNotIn("未配置账号/未知归属", printed)
        self.assertNotIn("未配置账号(999)", printed)


    def _add_stale_recheck_operation(
        self,
        *,
        market_hash_name: str = "Revolution Case",
        asset_id: str = "asset-old",
        listing_id: str = "listing-1",
        list_price: float = 25.0,
        steam_id64: str | None = None,
    ) -> int:
        if self.db.conn.execute(
            "SELECT 1 FROM inventory_pool WHERE market_hash_name = ?",
            (market_hash_name,),
        ).fetchone() is None:
            self.db.upsert_pool_item(market_hash_name, 1, status=POOL_STATUS_LISTED)
        else:
            self.db.set_pool_status(market_hash_name, POOL_STATUS_LISTED)
        if self.db.get_asset(asset_id) is None:
            self.db.upsert_inventory_assets(
                [
                    {
                        "assetId": asset_id,
                        "marketHashName": market_hash_name,
                        "steamId": steam_id64 or self.engine.steam_client.steam_id64,
                        "ifTradable": True,
                    }
                ]
            )
        self.db.set_asset_status(asset_id, "listed")
        note = {
            "listingId": listing_id,
            "steamListPrice": list_price,
            "rebuyPrice": 15.2,
            "guadaoMaxListingRatioAtOpen": 0.69,
            "steamNetFactorAtOpen": 0.869,
            "activeVerifiedAt": "2026-01-01T00:00:00+00:00",
            "steamId64": steam_id64 or self.engine.steam_client.steam_id64,
        }
        op_id = self.db.add_pool_operation(
            market_hash_name=market_hash_name,
            strategy=STRATEGY_GUADAO,
            operation_type=OP_SELL_STEAM,
            expected_price=list_price,
            asset_id=asset_id,
            note=json.dumps(note),
        )
        self.db.update_pool_operation(op_id, status="listed")
        stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        self.db.conn.execute(
            "UPDATE pool_operations SET created_at = ? WHERE id = ?",
            (stale_created_at, op_id),
        )
        self.db.conn.commit()
        return op_id

    def test_independent_stale_listing_recheck_removes_non_floor_listing(self) -> None:
        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("canceled", op["status"])
        self.assertEqual(["listing-1"], self.engine.steam_client.removed_listing_ids)
        self.assertEqual("available", self.db.get_asset("asset-old")["status"])
        note = json.loads(op["note"])
        self.assertEqual("removed", note["staleListedCleanupStatus"])
        self.assertAlmostEqual(20.0 / (25.0 * 0.869), float(note["staleListedCurrentRatio"]), places=6)

    def test_independent_stale_listing_recheck_keeps_nontradable_asset_locked(self) -> None:
        """Removing a listing must not make a trade-locked asset relistable."""

        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation()
        self.db.conn.execute(
            "UPDATE inventory_assets SET tradable = 0, status = 'locked' WHERE asset_id = ?",
            ("asset-old",),
        )
        self.db.conn.commit()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        assert op is not None
        note = json.loads(op["note"])
        self.assertEqual("removed", note["staleListedCleanupStatus"])
        self.assertEqual("locked", note["assetRestoredStatus"])
        self.assertEqual("locked", self.db.get_asset("asset-old")["status"])

    def test_independent_stale_listing_recheck_skips_remove_after_operation_leaves_listed(self) -> None:
        op_id = self._add_stale_recheck_operation()
        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(op["note"])
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        self.db.update_pool_operation(op_id, status="sold")
        result = self.engine._remove_stale_active_guadao_listing(
            op,
            note,
            client=self.engine.steam_client,
            active=self.engine.steam_client.list_active_listings(),
            active_listing_ids={"listing-1"},
        )

        self.assertIsNone(result)
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(
            "sold",
            self.db.conn.execute(
                "SELECT status FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["status"],
        )

    def test_independent_stale_listing_recheck_keeps_market_floor_within_tolerance(self) -> None:
        self.engine.config.stale_listed_recheck_hours = 24.0
        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 15.2, "count": 10}
        }
        op_id = self._add_stale_recheck_operation()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        note = json.loads(op["note"])
        self.assertEqual("kept_at_market_floor", note["staleListedCleanupStatus"])
        self.assertIn("staleListedNextCheckAt", note)
        self.assertEqual("listed", self.db.get_asset("asset-old")["status"])

    def test_independent_stale_listing_recheck_missing_active_listing_keeps_local_state(self) -> None:
        op_id = self._add_stale_recheck_operation()

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual("listed", self.db.get_asset("asset-old")["status"])
        self.assertEqual([], self.engine.steam_client.orderbook_calls)
        self.assertEqual([], self.engine.c5_client.price_batch_calls)

    def test_independent_stale_listing_recheck_active_asset_without_listing_id_defers(self) -> None:
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)

        class ActiveWithoutListingId:
            listing_id = ""
            asset_id = "asset-old"

        self.engine.steam_client.list_active_listings = (  # type: ignore[method-assign]
            lambda: [ActiveWithoutListingId()]
        )

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(0, result["removeAttempts"])
        self.assertEqual(1, result["deferred"])
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertEqual(
            "active listing matched asset but listing id is unavailable",
            note["staleListedCleanupReason"],
        )
        next_check_at = datetime.fromisoformat(note["staleListedNextCheckAt"])
        self.assertGreaterEqual(
            next_check_at,
            datetime.now(timezone.utc) + timedelta(minutes=9, seconds=50),
        )

    def test_independent_stale_listing_recheck_unknown_account_defers_without_steam_read(self) -> None:
        op_id = self._add_stale_recheck_operation()
        row = self.db.conn.execute(
            "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(row["note"])
        note.pop("steamId64", None)
        note.pop("steamAccountId", None)
        self.db.update_pool_operation(op_id, note=json.dumps(note))
        self.db.conn.execute(
            "UPDATE inventory_assets SET steam_id = NULL WHERE asset_id = ?",
            ("asset-old",),
        )
        self.db.conn.commit()

        def unexpected_active_loader() -> list[object]:
            raise AssertionError("unknown account attribution must not read Steam")

        self.engine.steam_client.list_active_listings = unexpected_active_loader  # type: ignore[method-assign]
        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual(0, result["accounts"])
        self.assertEqual(1, result["attributionDeferred"])
        self.assertEqual([], self.engine.steam_client.orderbook_calls)
        self.assertEqual([], self.engine.c5_client.price_batch_calls)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertEqual(
            "Steam account attribution is unavailable",
            note["staleListedCleanupReason"],
        )

    def test_independent_stale_listing_recheck_unknown_steam_id_defers_without_steam_read(self) -> None:
        account = Account(
            id="known-account",
            name="known-account",
            steam_id64=self.engine.steam_client.steam_id64,
            cookies="cookie",
        )
        self.engine.account_store = FakeAccountStore([account])
        op_id = self._add_stale_recheck_operation(steam_id64="unconfigured-steam-id")

        def unexpected_active_loader() -> list[object]:
            raise AssertionError("unconfigured SteamID must not read Steam")

        self.engine.steam_client.list_active_listings = unexpected_active_loader  # type: ignore[method-assign]
        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual(0, result["accounts"])
        self.assertEqual(1, result["attributionDeferred"])
        self.assertEqual([], self.engine.steam_client.orderbook_calls)
        self.assertEqual([], self.engine.c5_client.price_batch_calls)
        note = json.loads(op["note"])
        self.assertEqual(
            "Steam account attribution is unavailable",
            note["staleListedCleanupReason"],
        )

    def test_independent_stale_listing_recheck_mismatched_account_defers_without_steam_read(self) -> None:
        account = Account(
            id="account-one",
            name="account-one",
            steam_id64="111",
            cookies="cookie-one",
        )
        self.engine.account_store = FakeAccountStore([account])
        op_id = self._add_stale_recheck_operation(steam_id64="222")
        row = self.db.conn.execute(
            "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(row["note"])
        note["steamAccountId"] = account.id
        self.db.update_pool_operation(op_id, note=json.dumps(note))

        def unexpected_active_loader() -> list[object]:
            raise AssertionError("mismatched account attribution must not read Steam")

        self.engine.steam_client.list_active_listings = unexpected_active_loader  # type: ignore[method-assign]
        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual(0, result["accounts"])
        self.assertEqual(1, result["attributionDeferred"])
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertEqual(
            "Steam account attribution does not match operation SteamID",
            note["staleListedCleanupReason"],
        )

    def test_independent_stale_listing_recheck_price_read_failure_keeps_listing(self) -> None:
        self.engine.steam_client.orderbook_should_fail = True
        op_id = self._add_stale_recheck_operation()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertEqual("listed", self.db.get_asset("asset-old")["status"])

    def test_independent_stale_listing_recheck_non_finite_c5_price_defers(self) -> None:
        """NaN market evidence must never be interpreted as a ratio violation."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": float("nan"), "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("unavailable", note["staleListedCleanupReason"])

    def test_independent_stale_listing_recheck_non_finite_steam_floor_defers(self) -> None:
        """A non-finite Steam floor is incomplete destructive-action evidence."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [float("inf"), 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("floor price", note["staleListedCleanupReason"])

    def test_independent_stale_listing_recheck_non_finite_local_evidence_defers(self) -> None:
        """Stale local price/factor/ratio evidence must not be used to cancel."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        for field, value in (
            ("steamListPrice", float("nan")),
            ("steamNetFactorAtOpen", float("inf")),
            ("guadaoMaxListingRatioAtOpen", float("nan")),
        ):
            with self.subTest(field=field):
                op_id = self._add_stale_recheck_operation(
                    asset_id=f"asset-invalid-{field}",
                    listing_id=f"listing-invalid-{field}",
                )
                row = self.db.conn.execute(
                    "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
                ).fetchone()
                note = json.loads(row["note"])
                note[field] = value
                self.db.update_pool_operation(op_id, note=json.dumps(note, allow_nan=True))
                self.engine.steam_client.active_listing_assets[
                    f"asset-invalid-{field}"
                ] = f"listing-invalid-{field}"

                self.engine.run_guadao_stale_listing_recheck_task()

                op = self.db.conn.execute(
                    "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
                ).fetchone()
                self.assertEqual("listed", op["status"])
                self.assertEqual([], self.engine.steam_client.removed_listing_ids)
                final_note = json.loads(op["note"])
                self.assertEqual(
                    "check_deferred", final_note["staleListedCleanupStatus"]
                )

    def test_independent_stale_listing_recheck_uses_each_account_client_once(self) -> None:
        account_a = Account(
            id="account-a",
            name="account-a",
            steam_id64="111",
            cookies="cookie-a",
        )
        account_b = Account(
            id="account-b",
            name="account-b",
            steam_id64="222",
            cookies="cookie-b",
        )
        self.engine.account_store = FakeAccountStore([account_a, account_b])
        for account in (account_a, account_b):
            self.db.upsert_steam_cookie_health(
                account.id,
                status="valid",
                account_name=account.name,
                steam_id=account.steam_id64,
            )
        client_a = FakeSteamClient()
        client_a.steam_id64 = account_a.steam_id64
        client_b = FakeSteamClient()
        client_b.steam_id64 = account_b.steam_id64
        client_a.active_listing_assets["asset-a"] = "listing-a"
        client_b.active_listing_assets["asset-b"] = "listing-b"
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 15.2, "count": 10},
            "AK-47 | Redline (Field-Tested)": {"price": 15.2, "count": 10},
        }
        op_a = self._add_stale_recheck_operation(
            asset_id="asset-a",
            listing_id="listing-a",
            steam_id64=account_a.steam_id64,
        )
        op_b = self._add_stale_recheck_operation(
            market_hash_name="AK-47 | Redline (Field-Tested)",
            asset_id="asset-b",
            listing_id="listing-b",
            steam_id64=account_b.steam_id64,
        )
        for op_id, account_id in ((op_a, account_a.id), (op_b, account_b.id)):
            row = self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            note = json.loads(row["note"])
            note["steamAccountId"] = account_id
            self.db.update_pool_operation(op_id, note=json.dumps(note))

        calls: list[str] = []

        def client_for_account(account, steam_id64=None, **kwargs):
            calls.append(account.id)
            return {account_a.id: client_a, account_b.id: client_b}[account.id]

        self.engine._steam_client_for_account = client_for_account  # type: ignore[method-assign]
        result = self.engine.run_guadao_stale_listing_recheck_task()

        self.assertEqual(2, result["accounts"])
        self.assertEqual(2, result["checked"])
        self.assertCountEqual([account_a.id, account_b.id], calls)
        self.assertEqual("listed", self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?", (op_a,)
        ).fetchone()["status"])
        self.assertEqual("listed", self.db.conn.execute(
            "SELECT status FROM pool_operations WHERE id = ?", (op_b,)
        ).fetchone()["status"])

    def test_independent_stale_listing_recheck_reads_active_listings_once_per_account(self) -> None:
        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 15.2, "count": 10},
            "AK-47 | Redline (Field-Tested)": {"price": 15.2, "count": 10},
        }
        self._add_stale_recheck_operation()
        self._add_stale_recheck_operation(
            market_hash_name="AK-47 | Redline (Field-Tested)",
            asset_id="asset-second",
            listing_id="listing-2",
        )
        self.engine.steam_client.active_listing_assets.update(
            {"asset-old": "listing-1", "asset-second": "listing-2"}
        )
        original_loader = self.engine.steam_client.list_active_listings
        active_listing_calls = 0

        def counted_loader() -> list[object]:
            nonlocal active_listing_calls
            active_listing_calls += 1
            return original_loader()

        self.engine.steam_client.list_active_listings = counted_loader  # type: ignore[method-assign]
        with patch.object(
            self.engine,
            "_lookup_steam_sale_receipts_for_operations",
            side_effect=AssertionError("stale recheck must not run sale history"),
        ):
            self.engine.run_guadao_stale_listing_recheck_task()

        self.assertEqual(1, active_listing_calls)
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)

    def test_independent_stale_listing_recheck_reads_active_listings_in_safety_lane(self) -> None:
        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 15.2, "count": 10}
        }
        op_id = self._add_stale_recheck_operation()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        result = self.engine.run_guadao_stale_listing_recheck_task()

        self.assertEqual(1, result["checked"])
        self.assertEqual([True], self.engine.steam_client.list_active_listing_safety_terminal_calls)
        self.assertEqual([True], self.engine.steam_client.orderbook_safety_terminal_calls)
        self.assertEqual(
            "listed",
            self.db.conn.execute(
                "SELECT status FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()["status"],
        )

    def test_independent_stale_listing_recheck_does_not_overwrite_concurrent_sale(self) -> None:
        """A sale that wins while Steam cancellation is in flight must remain authoritative."""

        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        original_remove = self.engine.steam_client.remove_listing

        def remove_then_mark_sold(
            listing_id: str,
            *,
            execution_guard: object | None = None,
        ) -> bool:
            # Simulate the normal sync winning the local race while the remote
            # cancellation request is still in flight.
            latest = self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            latest_note = json.loads(latest["note"] or "{}")
            latest_note.update(
                {
                    "listingId": listing_id,
                    "steamSaleReceipt": {
                        "listingId": listing_id,
                        "receivedAmount": 21.73,
                    },
                    "steamSoldAt": "2026-08-07T00:00:00+00:00",
                }
            )
            self.db.update_pool_operation(
                op_id,
                status="sold",
                note=json.dumps(latest_note, ensure_ascii=False),
            )
            self.db.set_asset_status("asset-old", "sold")
            return original_remove(
                listing_id,
                execution_guard=execution_guard,
            )

        self.engine.steam_client.remove_listing = remove_then_mark_sold  # type: ignore[method-assign]

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("sold", op["status"])
        final_note = json.loads(op["note"] or "{}")
        self.assertIn("steamSaleReceipt", final_note)
        self.assertEqual("listing-1", final_note["listingId"])
        self.assertNotEqual("removed", final_note.get("staleListedCleanupStatus"))
        self.assertNotIn("assetRestoredStatus", final_note)
        self.assertEqual("sold", self.db.get_asset("asset-old")["status"])
        self.assertEqual(["listing-1"], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(
            POOL_STATUS_LISTED,
            self.db.list_pool_items(status=POOL_STATUS_LISTED)[0]["status"],
        )
        self.assertEqual(1, result["deferred"])
        self.assertEqual(0, result["removed"])
        self.assertEqual(0, result["removeFailed"])

    def test_independent_stale_listing_recheck_preserves_sale_note_on_deferred_price_read(self) -> None:
        """A deferred price read must not overwrite sale evidence written mid-check."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        def price_then_mark_sold(
            market_hash_names: list[str],
            app_id: int = 730,
        ) -> dict[str, object]:
            latest = self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            latest_note = json.loads(latest["note"] or "{}")
            latest_note["steamSaleReceipt"] = {
                "listingId": "listing-1",
                "receivedAmount": 21.73,
            }
            self.db.update_pool_operation(
                op_id,
                status="sold",
                note=json.dumps(latest_note, ensure_ascii=False),
            )
            self.db.set_asset_status("asset-old", "sold")
            return {}

        self.engine.c5_client.price_batch = price_then_mark_sold  # type: ignore[method-assign]

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("sold", op["status"])
        final_note = json.loads(op["note"] or "{}")
        self.assertIn("steamSaleReceipt", final_note)
        self.assertNotEqual("removed", final_note.get("staleListedCleanupStatus"))
        self.assertNotIn("assetRestoredStatus", final_note)
        self.assertEqual("sold", self.db.get_asset("asset-old")["status"])
        self.assertEqual(1, result["checked"])
        self.assertEqual(1, result["deferred"])
        self.assertEqual(0, result["priceDeferred"])
        self.assertEqual(0, result["removeAttempts"])

    def test_independent_stale_listing_recheck_does_not_cancel_relisted_operation(self) -> None:
        """A newer listing ID on the same operation must win the cancel race."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"
        original_remove = self.engine.steam_client.remove_listing

        def remove_then_relist(
            listing_id: str,
            *,
            execution_guard: object | None = None,
        ) -> bool:
            latest = self.db.conn.execute(
                "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
            ).fetchone()
            latest_note = json.loads(latest["note"] or "{}")
            latest_note["listingId"] = "listing-2"
            latest_note["relistedAt"] = "2026-08-07T00:00:00+00:00"
            self.db.update_pool_operation(
                op_id,
                status="listed",
                note=json.dumps(latest_note, ensure_ascii=False),
            )
            return original_remove(
                listing_id,
                execution_guard=execution_guard,
            )

        self.engine.steam_client.remove_listing = remove_then_relist  # type: ignore[method-assign]

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        final_note = json.loads(op["note"] or "{}")
        self.assertEqual("listing-2", final_note["listingId"])
        self.assertEqual("2026-08-07T00:00:00+00:00", final_note["relistedAt"])
        self.assertEqual("listed", self.db.get_asset("asset-old")["status"])
        self.assertEqual(["listing-1"], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(1, result["deferred"])
        self.assertEqual(0, result["removed"])

    def test_independent_stale_listing_recheck_does_not_cancel_new_listing_id_for_same_asset(self) -> None:
        """An asset match with a changed listing ID is not safe removal evidence."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(
            listing_id="listing-old",
            list_price=25.0,
        )
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-new"

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("listing id", note["staleListedCleanupReason"])

    def test_independent_stale_listing_recheck_defers_listing_asset_identity_conflict(self) -> None:
        """A matching listing ID with another asset ID must not be cancelled."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(
            listing_id="listing-shared",
            list_price=25.0,
        )
        self.engine.steam_client.active_listing_assets["asset-other"] = "listing-shared"

        self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("asset", note["staleListedCleanupReason"])

    def test_independent_stale_listing_recheck_c5_failure_never_removes(self) -> None:
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_should_fail = True
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(0, result["removeAttempts"])
        self.assertEqual("check_deferred", json.loads(op["note"])["staleListedCleanupStatus"])

    def test_independent_stale_listing_recheck_remove_failure_keeps_listing(self) -> None:
        """A failed remote cancellation must not advance the local operation."""

        self.engine.config.stale_listed_max_ratio_tolerance_pct = 1.5
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        self.engine.steam_client.remove_listing_should_fail = True
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(1, result["removeAttempts"])
        self.assertEqual(1, result["removeFailed"])
        self.assertEqual(0, result["removed"])
        note = json.loads(op["note"])
        self.assertEqual("remove_failed", note["staleListedCleanupStatus"])
        self.assertEqual(
            "Steam remove_listing returned false",
            note["staleListedCleanupReason"],
        )
        self.assertEqual(1, len(result["removeFailedOperations"]))
        self.assertEqual(op_id, result["removeFailedOperations"][0]["operationId"])

    def test_independent_stale_listing_recheck_currency_mismatch_never_removes(self) -> None:
        """A non-CNY orderbook is incomplete evidence and must defer safely."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 1, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(1, result["checked"])
        self.assertEqual(0, result["removeAttempts"])
        self.assertEqual(1, result["deferred"])
        self.assertEqual(1, result["priceDeferred"])
        self.assertEqual([], self.engine.c5_client.price_batch_calls)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("currency mismatch", note["staleListedCleanupReason"])

    def test_independent_stale_listing_recheck_missing_currency_never_removes(self) -> None:
        """An unlabelled orderbook is incomplete evidence and must defer safely."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(1, result["checked"])
        self.assertEqual(0, result["removeAttempts"])
        self.assertEqual(1, result["deferred"])
        self.assertEqual(1, result["priceDeferred"])
        self.assertEqual([], self.engine.c5_client.price_batch_calls)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("currency unavailable", note["staleListedCleanupReason"])

    def test_independent_stale_listing_recheck_respects_live_action_guard(self) -> None:
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"
        self.engine._new_action_guard = lambda: False

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(0, result["removeAttempts"])
        self.assertEqual(1, result["deferred"])
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertEqual(
            "executor disabled before stale listing removal",
            note["staleListedCleanupReason"],
        )

    def test_independent_stale_listing_recheck_defers_when_guard_changes_in_steam_queue(self) -> None:
        """A last-moment runtime shutdown must not turn into a remote cancel."""

        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"
        guard_calls = 0

        def changing_guard() -> bool:
            nonlocal guard_calls
            guard_calls += 1
            # First call is the executor's pre-request check; the fake Steam
            # client invokes the same guard again as the scheduler would.
            return guard_calls == 1

        self.engine._new_action_guard = changing_guard
        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(0, result["removeAttempts"])
        self.assertEqual(1, result["deferred"])
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("execution gate changed", note["staleListedCleanupReason"])

    def test_independent_stale_listing_recheck_dry_run_never_removes(self) -> None:
        self.engine.config.dry_run = True
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2400, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 20.0, "count": 1}
        }
        op_id = self._add_stale_recheck_operation(list_price=25.0)
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(0, result["removeAttempts"])
        self.assertEqual(1, result["deferred"])
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertEqual(
            "dry-run mode before stale listing removal",
            note["staleListedCleanupReason"],
        )

    def test_independent_stale_listing_recheck_active_listing_failure_never_removes(self) -> None:
        op_id = self._add_stale_recheck_operation()
        original_loader = self.engine.steam_client.list_active_listings

        def failing_loader() -> list[object]:
            raise RuntimeError("active listings unavailable")

        self.engine.steam_client.list_active_listings = failing_loader  # type: ignore[method-assign]
        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        self.assertEqual(0, result["checked"])
        self.assertEqual(0, result["removeAttempts"])
        self.engine.steam_client.list_active_listings = original_loader  # type: ignore[method-assign]

    def test_independent_stale_listing_recheck_cookie_health_failure_defers(self) -> None:
        """A health-store error must defer the account, not abort into removal."""

        op_id = self._add_stale_recheck_operation()
        account = Account(
            id="stale-health-account",
            name="stale-health-account",
            steam_id64=self.engine.steam_client.steam_id64,
            cookies="sessionid=test; steamLoginSecure=test",
        )
        self.engine.account_store = FakeAccountStore([account])
        row = self.db.conn.execute(
            "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(row["note"])
        note["steamAccountId"] = account.id
        self.db.update_pool_operation(op_id, note=json.dumps(note))

        def failing_health_loader(account_id: str) -> None:
            raise RuntimeError("cookie health database unavailable")

        self.db.get_steam_cookie_health = failing_health_loader  # type: ignore[method-assign]
        self.engine.steam_client.list_active_listings = (  # type: ignore[method-assign]
            lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("cookie health failure must not read Steam")
            )
        )

        result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual(1, result["deferred"])
        self.assertEqual([], self.engine.steam_client.removed_listing_ids)
        note = json.loads(op["note"])
        self.assertEqual("check_deferred", note["staleListedCleanupStatus"])
        self.assertIn("health unavailable", note["staleListedCleanupReason"])

    def test_independent_stale_listing_recheck_skips_listing_younger_than_48_hours(self) -> None:
        op_id = self._add_stale_recheck_operation()
        self.db.conn.execute(
            "UPDATE pool_operations SET created_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=47)).isoformat(), op_id),
        )
        self.db.conn.commit()

        def unexpected_active_loader() -> list[object]:
            raise AssertionError("listing younger than 48 hours must not read Steam")

        self.engine.steam_client.list_active_listings = unexpected_active_loader  # type: ignore[method-assign]
        with patch.object(self.engine, "_emit_guadao_local_event") as emit:
            result = self.engine.run_guadao_stale_listing_recheck_task()
        emit.assert_called_once()
        self.assertEqual("stale_listing_recheck", emit.call_args.kwargs["operation"])

        self.assertEqual(0, result["due"])
        self.assertIsInstance(result.get("summary"), str)
        self.assertTrue(result["summary"])
        self.assertEqual([], self.engine.steam_client.orderbook_calls)
        self.assertEqual([], self.engine.c5_client.price_batch_calls)

    def test_independent_stale_listing_recheck_skips_future_next_check(self) -> None:
        op_id = self._add_stale_recheck_operation()
        row = self.db.conn.execute(
            "SELECT note FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        note = json.loads(row["note"])
        note["staleListedCleanupStatus"] = "kept_at_market_floor"
        note["staleListedNextCheckAt"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        self.db.update_pool_operation(op_id, note=json.dumps(note, ensure_ascii=False))

        def unexpected_active_loader() -> list[object]:
            raise AssertionError("future stale recheck must not read Steam")

        self.engine.steam_client.list_active_listings = unexpected_active_loader  # type: ignore[method-assign]
        result = self.engine.run_guadao_stale_listing_recheck_task()

        self.assertEqual(0, result["due"])
        self.assertIsInstance(result.get("summary"), str)
        self.assertTrue(result["summary"])
        self.assertEqual([], self.engine.steam_client.orderbook_calls)
        self.assertEqual([], self.engine.c5_client.price_batch_calls)


    def test_independent_stale_listing_recheck_does_not_probe_or_relogin(self) -> None:
        self.engine.steam_client.orderbook_payload = {
            "success": 1,
            "data": {"eCurrency": 23, "rgCompactSellOrders": [2500, 20]},
        }
        self.engine.c5_client.price_batch_payload = {
            "Revolution Case": {"price": 15.2, "count": 10}
        }
        op_id = self._add_stale_recheck_operation()
        self.engine.steam_client.active_listing_assets["asset-old"] = "listing-1"

        with (
            patch.object(
                self.engine,
                "_ensure_market_client_ready",
                side_effect=AssertionError("stale task must not probe my_listings"),
            ),
            patch(
                "cs2_assistant.services.executor_engine.try_steam_auto_relogin",
                side_effect=AssertionError("stale task must not relogin"),
            ),
        ):
            result = self.engine.run_guadao_stale_listing_recheck_task()

        op = self.db.conn.execute(
            "SELECT * FROM pool_operations WHERE id = ?", (op_id,)
        ).fetchone()
        self.assertEqual("listed", op["status"])
        self.assertEqual(1, result["checked"])

if __name__ == "__main__":
    unittest.main()
