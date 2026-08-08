from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import MarketState, StrategyConfig
import cs2_assistant.services.profit_trade as profit_trade
from cs2_assistant.services.profit_trade import (
    ProfitTradeOpportunity,
    ProfitTradeScanReport,
    SteamBuyAccountSelection,
)
from cs2_assistant.services.profit_trade_long_buy import (
    build_long_buy_proposal,
    competitor_buy_reference,
    remembered_own_price_cents,
)


MARKET_NAME = "AK-47 | Redline (Field-Tested)"


def long_buy_config(**overrides: object) -> StrategyConfig:
    values: dict[str, object] = {
        "profit_trade_enabled": True,
        "profit_trade_allow_real_execution": True,
        "profit_trade_min_roi": 0.05,
        "profit_trade_balance_discount": 0.69,
        "profit_trade_c5_current_sale_net_factor": 0.99,
        "profit_trade_require_c5_recent_sales": False,
        "profit_trade_require_c5_market_depth": False,
        "profit_trade_manual_review_roi": 9999.0,
        "profit_trade_long_buy_enabled": True,
        "profit_trade_long_buy_allow_real_execution": False,
        "profit_trade_long_buy_max_active_orders": 25,
        "profit_trade_long_buy_create_fraction_per_cycle": 0.2,
        "profit_trade_long_buy_aggressive_roi_delta": 0.005,
        "profit_trade_long_buy_min_price_advantage": 0.10,
        "profit_trade_long_buy_max_price_advantage": 1.00,
    }
    values.update(overrides)
    return StrategyConfig(**values)


def orderbook(*prices: float) -> dict:
    return {
        "currencyId": 23,
        "currencyValid": True,
        "buyLevels": [
            {"price": price, "count": index + 1}
            for index, price in enumerate(prices)
        ],
        "sellLevels": [],
    }


class LongBuyRemoteClient:
    account_id = "account-a"
    steam_id64 = "steam-a"

    def __init__(self) -> None:
        self.active_orders: list[dict] = []
        self.receipts: dict[str, tuple[dict, ...]] = {}
        self.coverage_complete = True
        self.lookup_succeeded = True
        self.cancel_calls: list[str] = []
        self.create_calls: list[dict] = []
        self.wallet = 1000.0

    def my_listings(self, **_: object) -> dict:
        return {"buy_orders": [dict(row) for row in self.active_orders]}

    def find_purchase_receipts_for_targets_with_coverage(
        self,
        targets: list[dict],
        **_: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            receipts={
                str(target["key"]): tuple(
                    self.receipts.get(str(target["key"]), ())
                )
                for target in targets
            },
            coverage_complete=self.coverage_complete,
            lookup_succeeded=self.lookup_succeeded,
            error=None,
        )

    def cancel_buy_order(self, *, buy_order_id: str) -> dict:
        self.cancel_calls.append(str(buy_order_id))
        self.active_orders = [
            row
            for row in self.active_orders
            if str(row.get("buy_orderid") or "") != str(buy_order_id)
        ]
        return {"success": 1}

    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_calls.append(dict(kwargs))
        buy_order_id = f"remote-{len(self.create_calls)}"
        self.active_orders.append(
            {
                "buy_orderid": buy_order_id,
                "market_hash_name": kwargs["market_hash_name"],
                "price": kwargs["price_total"],
                "quantity": kwargs["quantity"],
                "quantity_remaining": kwargs["quantity"],
            }
        )
        return {"success": 1, "buy_orderid": buy_order_id}

    def wallet_balance(self, **_: object) -> dict:
        return {
            "balance": self.wallet,
            "delayed_balance": 0.0,
            "currency": "CNY",
            "currency_id": 23,
        }

    def order_book(self, **_: object) -> dict:
        return {
            "success": True,
            "data": {
                "eCurrency": 23,
                "rgCompactSellOrders": [15000, 1],
                "rgCompactBuyOrders": [13000, 2],
            },
        }


class TypeErrorAfterCreateEntryClient(LongBuyRemoteClient):
    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_calls.append(dict(kwargs))
        raise TypeError(
            "execution_guard failed inside create_buy_order after request dispatch"
        )


class DisableBeforeCreateHttpClient(LongBuyRemoteClient):
    def __init__(
        self,
        settings: Settings,
        disabled_config: StrategyConfig,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.disabled_config = disabled_config
        self.guard_results: list[bool] = []
        self.remote_post_calls = 0

    def create_buy_order(self, **kwargs: object) -> dict:
        self.create_calls.append(dict(kwargs))
        profit_trade.save_strategy_config(self.settings, self.disabled_config)
        guard = kwargs.get("execution_guard")
        allowed = bool(guard()) if callable(guard) else True
        self.guard_results.append(allowed)
        if not allowed:
            raise profit_trade.SteamRequestGuardRejected(
                "test guard rejected before HTTP"
            )
        self.remote_post_calls += 1
        return {"success": 1, "buy_orderid": "unexpected-remote-order"}


class LongBuyC5Client:
    def __init__(self, price: float = 100.0) -> None:
        self.price = price

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        return {
            name: {"price": self.price, "count": 10}
            for name in market_hash_names
        }


class LowValueLongBuyMarketService:
    """Fresh authoritative state for an existing order below minItemValue."""

    def __init__(self, *, c5_price: float = 4.0, steam_price: float = 100.0) -> None:
        self.c5_price = c5_price
        self.steam_price = steam_price
        self.calls: list[list[dict]] = []

    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        self.calls.append(list(items))
        return [
            MarketState(
                market_hash_name=str(item["market_hash_name"]),
                name_cn=str(item.get("name_cn") or item["market_hash_name"]),
                c5_sell_price=self.c5_price,
                c5_sell_count=10,
                c5_price_source="c5_batch",
                steam_sell_price=self.steam_price,
                steam_price_source="steam_orderbook",
                raw_json={"steam_orderbook_snapshot": orderbook(5.0)},
            )
            for item in items
        ]


class CrossedLongBuyMarketService:
    """A crossed Steam book with a configurable seller price."""

    def __init__(self, *, steam_price: float = 140.0) -> None:
        self.calls: list[list[dict]] = []
        self.steam_price = steam_price

    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        self.calls.append(list(items))
        return [
            MarketState(
                market_hash_name=str(item["market_hash_name"]),
                name_cn=str(item.get("name_cn") or item["market_hash_name"]),
                c5_sell_price=100.0,
                c5_sell_count=10,
                c5_price_source="c5_batch",
                # With the default 140.00 seller price, 98.67 C5 net / 140.00
                # - 0.69 = 1.48%, below the normal 5.00% ROI gate.
                steam_sell_price=self.steam_price,
                steam_price_source="steam_orderbook",
                raw_json={
                    "steam_orderbook_snapshot": {
                        "currencyId": 23,
                        "currencyValid": True,
                        "buyLevels": [{"price": 140.0, "count": 1}],
                        "sellLevels": [{"price": self.steam_price, "count": 1}],
                        "crossed": True,
                    }
                },
            )
            for item in items
        ]


class EmptyAccountStore:
    def __init__(self, *_: object, **__: object) -> None:
        pass

    def list_accounts(self) -> list:
        return []

    def get_current(self) -> None:
        return None


class ProfitTradeLongBuyPolicyTestCase(unittest.TestCase):
    def test_pricing_branches_use_integer_cents(self) -> None:
        config = long_buy_config()

        no_competitor = build_long_buy_proposal(
            config,
            c5_price_batch=100.0,
            orderbook_snapshot=orderbook(),
            quantity=4,
        )
        self.assertEqual("standard_no_competitor", no_competitor["decision"])
        # A CNY 100.00 price_batch reference becomes a real initial C5 listing
        # of floor(100.00 * 0.9967) = 99.67, then C5 net proceeds of 98.6733.
        # Long-buy safety must use those actual proceeds rather than 99.00.
        self.assertEqual(13334, no_competitor["targetPriceCents"])
        self.assertAlmostEqual(98.67, no_competitor["c5ExpectedNetPrice"])

        standard = build_long_buy_proposal(
            config,
            c5_price_batch=100.0,
            orderbook_snapshot=orderbook(130.0),
            quantity=4,
        )
        self.assertEqual("standard_safe_price", standard["decision"])
        self.assertEqual(13334, standard["targetPriceCents"])

        aggressive = build_long_buy_proposal(
            config,
            c5_price_batch=100.0,
            orderbook_snapshot=orderbook(134.0),
            quantity=4,
        )
        self.assertEqual(
            "aggressive_competitor_advantage",
            aggressive["decision"],
        )
        self.assertEqual(13424, aggressive["targetPriceCents"])

        low_queue = build_long_buy_proposal(
            config,
            c5_price_batch=100.0,
            orderbook_snapshot=orderbook(135.0),
            quantity=4,
        )
        self.assertEqual("standard_low_queue", low_queue["decision"])
        self.assertEqual(13334, low_queue["targetPriceCents"])

    def test_safe_price_floors_initial_c5_listing_before_net_factor(self) -> None:
        proposal = build_long_buy_proposal(
            long_buy_config(),
            c5_price_batch=100.01,
            orderbook_snapshot=orderbook(),
            quantity=1,
        )

        # The real initial listing is floor(100.01 * 0.9967, CNY cent) = 99.67.
        # Applying the 0.99 C5 net factor before that cent-floor would instead
        # produce a one-cent-too-high safe bid here.
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertAlmostEqual(98.67, proposal["c5ExpectedNetPrice"])
        self.assertEqual(13334, proposal["standardSafePriceCents"])

    def test_own_current_and_recent_previous_prices_are_excluded(self) -> None:
        own = {
            "bid_price_cents": 13400,
            "previous_bid_price_cents": 13300,
            "previous_price_expires_at": "2999-01-01T00:00:00+00:00",
        }
        remembered = remembered_own_price_cents(own)
        self.assertEqual([13400, 13300], remembered)
        competitor = competitor_buy_reference(
            orderbook(134.0, 133.0, 132.0, 131.0, 130.0),
            own_price_cents=remembered,
        )
        self.assertEqual(132.0, competitor["price"])
        self.assertEqual("self_price_excluded", competitor["status"])
        self.assertEqual([134.0, 133.0], competitor["excludedOwnPrices"])

    def test_missing_external_level_is_explicit(self) -> None:
        competitor = competitor_buy_reference(
            orderbook(134.0, 133.0),
            own_price_cents=[13400, 13300],
        )
        self.assertIsNone(competitor["price"])
        self.assertEqual("missing_external_level", competitor["status"])


class ProfitTradeLongBuyDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.temp_dir.name) / "assistant.db",
            serverchan_sendkey=None,
        )
        self.db = Database(self.settings.db_path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def _create_order(
        self,
        *,
        request_id: str = "request-1",
        quantity: int = 2,
    ) -> int:
        return self.db.create_profit_trade_long_buy_order(
            market_hash_name=MARKET_NAME,
            steam_account_id="account-a",
            steam_id="steam-a",
            create_request_id=request_id,
            bid_price_cents=13400,
            quantity=quantity,
            c5_price_batch=100.0,
            c5_expected_net_price=99.0,
            balance_discount=0.69,
            standard_roi=0.05,
            aggressive_roi=0.045,
            standard_safe_price_cents=13378,
            aggressive_safe_price_cents=13469,
            competitor_buy_price_cents=13400,
            competitor_buy_status="raw",
            worst_case_roi=0.0488,
            source_scan_id="scan-1",
            wallet_before=1000.0,
        )

    def _selection(
        self,
        client: LongBuyRemoteClient,
    ) -> SteamBuyAccountSelection:
        return SteamBuyAccountSelection(
            account=None,
            client=client,
            wallet_balance=client.wallet,
            reserved_balance=0.0,
            spendable_balance=client.wallet,
            wallet=client.wallet_balance(),
            wallet_is_live=True,
        )

    def _proposal(self, config: StrategyConfig) -> dict:
        proposal = build_long_buy_proposal(
            config,
            c5_price_batch=100.0,
            orderbook_snapshot=orderbook(130.0),
            quantity=1,
        )
        assert proposal is not None
        return proposal

    @staticmethod
    def _low_value_inventory_payload(
        *,
        reference_price: float = 4.0,
    ) -> dict:
        return {
            "source": "fixture",
            "list": [
                {
                    "assetId": f"asset-{index}",
                    "marketHashName": MARKET_NAME,
                    "steamId": "steam-a",
                    "ifTradable": True,
                    # Deliberately below minItemValue.  A live managed order
                    # must still get its current risk-maintenance snapshot;
                    # a brand-new low-value market must not.
                    "price": reference_price,
                    "token": f"token-{index}",
                    "styleToken": f"style-{index}",
                }
                for index in range(2)
            ],
        }

    def _scan_low_value_existing_long_buy(
        self,
        config: StrategyConfig,
        *,
        market_service: LowValueLongBuyMarketService | None = None,
    ) -> tuple[ProfitTradeScanReport, LowValueLongBuyMarketService]:
        service = market_service or LowValueLongBuyMarketService()
        report = profit_trade.scan_profit_trade_opportunities(
            self.settings,
            config,
            inventory_payload=self._low_value_inventory_payload(),
            market_service=service,
            c5_client=LongBuyC5Client(price=4.0),
        )
        return report, service

    def test_one_live_order_per_item_and_purchase_idempotency(self) -> None:
        order_id = self._create_order()
        with self.assertRaises(Exception):
            self._create_order(request_id="request-2")
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-1",
        )
        first = self.db.record_profit_trade_long_buy_fill(
            long_buy_order_id=order_id,
            steam_account_id="account-a",
            purchase_id="purchase-1",
            listing_id="listing-1",
            market_hash_name=MARKET_NAME,
            paid_total_cents=12000,
            asset_id="old-b",
            new_asset_id="new-b",
            purchased_at="2026-07-28T01:00:00+00:00",
            evidence={"source": "test"},
        )
        duplicate = self.db.record_profit_trade_long_buy_fill(
            long_buy_order_id=order_id,
            steam_account_id="account-a",
            purchase_id="purchase-1",
            listing_id="listing-1",
            market_hash_name=MARKET_NAME,
            paid_total_cents=12000,
            asset_id="old-b",
            new_asset_id="new-b",
            purchased_at="2026-07-28T01:00:00+00:00",
            evidence={"source": "test"},
        )
        self.assertTrue(first["inserted"])
        self.assertFalse(duplicate["inserted"])
        self.assertEqual(1, self.db.count_profit_trade_long_buy_fills(order_id))

    def test_reconcile_partial_fill_and_imports_actual_paid_total(self) -> None:
        order_id = self._create_order()
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-1",
        )
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-1",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 2,
                "quantity_remaining": 1,
            }
        ]
        client.receipts[str(order_id)] = (
            {
                "purchaseId": "purchase-1",
                "listingId": "listing-1",
                "marketHashName": MARKET_NAME,
                "paidTotal": 120.0,
                "currencyId": 23,
                "timePurchased": 1785200400,
                "newAssetId": "new-b",
            },
        )

        reconciled = profit_trade._reconcile_profit_trade_long_buy_account(
            self.db,
            self.settings,
            orders=[self.db.get_profit_trade_long_buy_order(order_id)],
            client=client,
        )
        order = self.db.get_profit_trade_long_buy_order(order_id)
        self.assertEqual("partial", order["state"])
        self.assertEqual(1, order["filled_quantity"])
        self.assertEqual(1, order["remaining_quantity"])
        self.assertEqual(1, len(reconciled.new_fill_ids))

        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=1,
            evaluated_count=1,
            opportunity_count=0,
            missing_price_count=0,
            skipped_count=1,
            opportunities=[],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=[
                {
                    "assetId": "asset-a",
                    "marketHashName": MARKET_NAME,
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "token": "token-a",
                    "styleToken": "style-a",
                }
            ],
            watch_records=[],
        )
        imported = profit_trade._process_pending_profit_trade_long_buy_fills(
            self.settings,
            long_buy_config(),
            scanned=scanned,
        )
        self.assertEqual(1, len(imported.imported_steam_bought_trade_ids))
        trade = self.db.get_profit_trade(
            imported.imported_steam_bought_trade_ids[0]
        )
        self.assertEqual("steam_bought", trade["status"])
        self.assertAlmostEqual(120.0, trade["steam_buy_price"])
        note = json.loads(trade["note"])
        self.assertEqual("createbuyorder_long_term", note["steamBuyMethod"])
        self.assertEqual("purchase-1", note["steamPurchaseReceipt"]["purchaseId"])

        imported_again = profit_trade._process_pending_profit_trade_long_buy_fills(
            self.settings,
            long_buy_config(),
            scanned=scanned,
        )
        self.assertEqual([], imported_again.imported_trade_ids)

    def test_creating_order_recovers_only_one_exact_remote_match(self) -> None:
        order_id = self._create_order(quantity=1)
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "remote-recovered",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]

        reconciled = profit_trade._reconcile_profit_trade_long_buy_account(
            self.db,
            self.settings,
            orders=[self.db.get_profit_trade_long_buy_order(order_id)],
            client=client,
        )

        order = self.db.get_profit_trade_long_buy_order(order_id)
        self.assertEqual([], reconciled.uncertain_order_ids)
        self.assertEqual("active", order["state"])
        self.assertEqual("remote-recovered", order["buy_order_id"])

    def test_creating_order_with_multiple_exact_remote_matches_is_uncertain(
        self,
    ) -> None:
        order_id = self._create_order(quantity=1)
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": remote_id,
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
            for remote_id in ("remote-a", "remote-b")
        ]

        reconciled = profit_trade._reconcile_profit_trade_long_buy_account(
            self.db,
            self.settings,
            orders=[self.db.get_profit_trade_long_buy_order(order_id)],
            client=client,
        )

        order = self.db.get_profit_trade_long_buy_order(order_id)
        self.assertEqual([order_id], reconciled.uncertain_order_ids)
        self.assertEqual("terminal_uncertain", order["state"])
        self.assertIn("multiple same-item same-price", order["terminal_reason"])

    def test_missing_remote_order_requires_two_complete_observations(self) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-missing",
        )
        client = LongBuyRemoteClient()

        first = profit_trade._reconcile_profit_trade_long_buy_account(
            self.db,
            self.settings,
            orders=[self.db.get_profit_trade_long_buy_order(order_id)],
            client=client,
        )
        first_order = self.db.get_profit_trade_long_buy_order(order_id)
        self.assertEqual([order_id], first.uncertain_order_ids)
        self.assertEqual("terminal_uncertain", first_order["state"])
        self.assertTrue(
            json.loads(first_order["note_json"])["remoteAbsenceObservedAt"]
        )

        second = profit_trade._reconcile_profit_trade_long_buy_account(
            self.db,
            self.settings,
            orders=[self.db.get_profit_trade_long_buy_order(order_id)],
            client=client,
        )
        second_order = self.db.get_profit_trade_long_buy_order(order_id)
        self.assertEqual([], second.uncertain_order_ids)
        self.assertEqual("auto_cancelled", second_order["state"])

    def test_terminal_uncertain_alert_is_attempted_only_once_per_order(self) -> None:
        order_id = self._create_order(quantity=1)
        order = self.db.get_profit_trade_long_buy_order(order_id)

        with patch.object(
            profit_trade,
            "_send_profit_trade_long_buy_uncertain_alert",
            return_value=True,
        ) as send_alert:
            first = profit_trade._mark_profit_trade_long_buy_uncertain(
                self.db,
                self.settings,
                order,
                reason="first uncertainty",
            )
            profit_trade._mark_profit_trade_long_buy_uncertain(
                self.db,
                self.settings,
                first,
                reason="second uncertainty",
            )

        self.assertEqual(1, send_alert.call_count)
        note = json.loads(
            self.db.get_profit_trade_long_buy_order(order_id)["note_json"]
        )
        self.assertTrue(note["terminalUncertainAlertAttempted"])
        self.assertTrue(note["terminalUncertainAlertSent"])

    def test_confirmed_fill_without_old_a_becomes_manual_required(self) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="filled",
            buy_order_id="buy-1",
        )
        self.db.record_profit_trade_long_buy_fill(
            long_buy_order_id=order_id,
            steam_account_id="account-a",
            purchase_id="purchase-1",
            listing_id="listing-1",
            market_hash_name=MARKET_NAME,
            paid_total_cents=12000,
            asset_id="old-b",
            new_asset_id="new-b",
            purchased_at="2026-07-28T01:00:00+00:00",
            evidence={"receipt": {"purchaseId": "purchase-1", "paidTotal": 120.0}},
        )
        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=0,
            evaluated_count=0,
            opportunity_count=0,
            missing_price_count=0,
            skipped_count=0,
            opportunities=[],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=[],
            watch_records=[],
        )
        imported = profit_trade._process_pending_profit_trade_long_buy_fills(
            self.settings,
            long_buy_config(),
            scanned=scanned,
        )
        self.assertEqual(1, len(imported.manual_trade_ids))
        trade = self.db.get_profit_trade(imported.manual_trade_ids[0])
        self.assertEqual("manual_required", trade["status"])
        self.assertIn("no executable old A", trade["error"])

    def test_cancel_fill_race_never_reports_safe_cancel(self) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-1",
        )
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-1",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]

        def cancel_with_fill(*, buy_order_id: str) -> dict:
            client.cancel_calls.append(buy_order_id)
            client.active_orders = []
            client.receipts[str(order_id)] = (
                {
                    "purchaseId": "race-fill",
                    "listingId": "listing-race",
                    "marketHashName": MARKET_NAME,
                    "paidTotal": 120.0,
                    "currencyId": 23,
                    "timePurchased": 1785200400,
                },
            )
            return {"success": 1}

        client.cancel_buy_order = cancel_with_fill  # type: ignore[method-assign]
        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        outcome, fill_ids = profit_trade._safe_cancel_profit_trade_long_buy_order(
            self.db,
            self.settings,
            config,
            order=self.db.get_profit_trade_long_buy_order(order_id),
            client=client,
            reason="test direct purchase",
        )
        self.assertEqual("purchased", outcome)
        self.assertEqual(1, len(fill_ids))
        self.assertEqual("filled", self.db.get_profit_trade_long_buy_order(order_id)["state"])

    def test_direct_purchase_gate_cancels_active_order_only_at_final_gate(self) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-1",
        )
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-1",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]
        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)

        gate = profit_trade._prepare_profit_trade_long_buy_for_direct_purchase(
            self.db,
            self.settings,
            config,
            market_hash_name=MARKET_NAME,
            steam_client=client,
            new_action_guard=None,
        )

        self.assertTrue(gate["ok"], gate)
        self.assertEqual("cancelled", gate["outcome"])
        self.assertEqual(["buy-1"], client.cancel_calls)
        self.assertIn(
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
            {"cancelled", "auto_cancelled"},
        )

    def test_create_runtime_type_error_is_never_retried(self) -> None:
        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        client = TypeErrorAfterCreateEntryClient()

        order_id = profit_trade._create_profit_trade_long_buy_order_remote(
            self.db,
            self.settings,
            config,
            market_hash_name=MARKET_NAME,
            proposal=self._proposal(config),
            quantity=1,
            selection=self._selection(client),
            source_scan_id="scan-type-error",
        )

        self.assertEqual(1, len(client.create_calls))
        self.assertEqual(
            "terminal_uncertain",
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
        )

    def test_latest_config_blocks_create_before_http(self) -> None:
        enabled_config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        disabled_config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=False,
        )
        profit_trade.save_strategy_config(self.settings, enabled_config)
        client = DisableBeforeCreateHttpClient(self.settings, disabled_config)

        order_id = profit_trade._create_profit_trade_long_buy_order_remote(
            self.db,
            self.settings,
            enabled_config,
            market_hash_name=MARKET_NAME,
            proposal=self._proposal(enabled_config),
            quantity=1,
            selection=self._selection(client),
            source_scan_id="scan-disable-race",
        )

        self.assertEqual([False], client.guard_results)
        self.assertEqual(0, client.remote_post_calls)
        self.assertEqual(
            "failed",
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
        )

    def test_latest_config_blocks_cancel_without_changing_order_state(self) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-1",
        )
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-1",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]
        stale_enabled_config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(
            self.settings,
            long_buy_config(
                profit_trade_long_buy_allow_real_execution=False,
            ),
        )

        outcome, fill_ids = profit_trade._safe_cancel_profit_trade_long_buy_order(
            self.db,
            self.settings,
            stale_enabled_config,
            order=self.db.get_profit_trade_long_buy_order(order_id),
            client=client,
            reason="test latest-config gate",
        )

        self.assertEqual("blocked", outcome)
        self.assertEqual([], fill_ids)
        self.assertEqual([], client.cancel_calls)
        self.assertEqual(
            "active",
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
        )

    def test_capacity_counts_manual_orders_and_fails_closed(self) -> None:
        client = LongBuyRemoteClient()
        client.wallet = 10.0
        client.active_orders = [
            {
                "buy_orderid": "manual-1",
                "market_hash_name": "Other Item",
                "price": 9900,
                "quantity": 10,
                "quantity_remaining": 10,
            }
        ]
        selection = SteamBuyAccountSelection(
            account=None,
            client=client,
            wallet_balance=10.0,
            reserved_balance=0.0,
            spendable_balance=10.0,
            wallet=client.wallet_balance(),
            wallet_is_live=True,
        )
        with self.assertRaisesRegex(RuntimeError, "10x"):
            profit_trade._validate_profit_trade_long_buy_capacity(
                self.settings,
                long_buy_config(),
                selection=selection,
                market_hash_name=MARKET_NAME,
                bid_price_cents=1000,
                quantity=2,
            )

    def test_capacity_paginates_more_than_one_hundred_active_buy_orders(self) -> None:
        class PagedClient(LongBuyRemoteClient):
            def __init__(self) -> None:
                super().__init__()
                self.my_listings_calls: list[tuple[int, int]] = []

            def my_listings(
                self,
                *,
                start: int = 0,
                count: int = 100,
                **_: object,
            ) -> dict:
                self.my_listings_calls.append((start, count))
                return {
                    "buy_orders": [
                        dict(row)
                        for row in self.active_orders[start : start + count]
                    ],
                    "total_buy_orders": len(self.active_orders),
                }

        client = PagedClient()
        client.wallet = 10.0
        client.active_orders = [
            {
                "buy_orderid": f"manual-{index}",
                "market_hash_name": f"Other Item {index}",
                "price": 99,
                "quantity": 1,
                "quantity_remaining": 1,
            }
            for index in range(101)
        ]

        with self.assertRaisesRegex(RuntimeError, "10x"):
            profit_trade._validate_profit_trade_long_buy_capacity(
                self.settings,
                long_buy_config(),
                selection=self._selection(client),
                market_hash_name=MARKET_NAME,
                bid_price_cents=100,
                quantity=1,
            )

        self.assertEqual([(0, 100), (100, 100)], client.my_listings_calls)

    def test_incomplete_buy_order_snapshot_never_auto_cancels_missing_order(self) -> None:
        class BrokenSecondPageClient(LongBuyRemoteClient):
            def __init__(self) -> None:
                super().__init__()
                self.my_listings_calls: list[tuple[int, int]] = []

            def my_listings(
                self,
                *,
                start: int = 0,
                count: int = 100,
                **_: object,
            ) -> dict:
                self.my_listings_calls.append((start, count))
                if start >= 100:
                    raise RuntimeError("second buy-order page unavailable")
                return {
                    "buy_orders": [
                        {
                            "buy_orderid": f"other-{index}",
                            "market_hash_name": f"Other Item {index}",
                            "price": 100,
                            "quantity": 1,
                            "quantity_remaining": 1,
                        }
                        for index in range(100)
                    ],
                    "total_buy_orders": 101,
                }

        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-missing",
        )
        client = BrokenSecondPageClient()

        first = profit_trade._reconcile_profit_trade_long_buy_account(
            self.db,
            self.settings,
            orders=[self.db.get_profit_trade_long_buy_order(order_id)],
            client=client,
        )
        first_order = self.db.get_profit_trade_long_buy_order(order_id)
        self.assertEqual([order_id], first.uncertain_order_ids)
        self.assertEqual("terminal_uncertain", first_order["state"])
        self.assertIn("snapshot is incomplete", first_order["terminal_reason"])
        first_note = json.loads(first_order["note_json"])
        self.assertFalse(first_note["lastRemoteBuyOrdersComplete"])
        self.assertNotIn("remoteAbsenceObservedAt", first_note)

        second = profit_trade._reconcile_profit_trade_long_buy_account(
            self.db,
            self.settings,
            orders=[self.db.get_profit_trade_long_buy_order(order_id)],
            client=client,
        )
        second_order = self.db.get_profit_trade_long_buy_order(order_id)
        self.assertEqual([order_id], second.uncertain_order_ids)
        self.assertEqual("terminal_uncertain", second_order["state"])
        self.assertNotIn(
            "remote_auto_cancel_confirmed",
            [
                event["event_type"]
                for event in self.db.list_profit_trade_long_buy_events(order_id)
            ],
        )
        self.assertEqual(
            [(0, 100), (100, 100), (0, 100), (100, 100)],
            client.my_listings_calls,
        )

    def test_observation_mode_never_creates_or_cancels(self) -> None:
        proposal = build_long_buy_proposal(
            long_buy_config(),
            c5_price_batch=100.0,
            orderbook_snapshot=orderbook(130.0),
            quantity=4,
        )
        proposal.update(
            {
                "eligible": True,
                "sourceScanId": "scan-1",
            }
        )
        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=1,
            evaluated_count=1,
            opportunity_count=0,
            missing_price_count=0,
            skipped_count=1,
            opportunities=[],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=[
                {
                    "assetId": "asset-a",
                    "marketHashName": MARKET_NAME,
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "token": "token-a",
                    "styleToken": "style-a",
                }
            ],
            watch_records=[
                {
                    "market_hash_name": MARKET_NAME,
                    "raw": {"longBuyProposal": proposal},
                }
            ],
        )
        client = LongBuyRemoteClient()
        cycle = profit_trade._run_profit_trade_long_buy_cycle(
            self.settings,
            long_buy_config(
                profit_trade_allow_real_execution=True,
                profit_trade_long_buy_allow_real_execution=False,
            ),
            scanned=scanned,
            steam_client=client,
            c5_client=LongBuyC5Client(),
            new_action_guard=None,
        )
        self.assertEqual([], cycle.created_order_ids)
        self.assertEqual([], client.create_calls)
        self.assertEqual([], client.cancel_calls)

    def test_disabled_long_buy_writes_block_direct_purchase_without_cancel(self) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-active",
        )
        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=1,
            evaluated_count=1,
            opportunity_count=1,
            missing_price_count=0,
            skipped_count=0,
            opportunities=[
                ProfitTradeOpportunity(
                    market_hash_name=MARKET_NAME,
                    name=MARKET_NAME,
                    asset_id="asset-a",
                    steam_id="steam-a",
                    token="token-a",
                    style_token="style-a",
                    steam_buy_price=100.0,
                    steam_price_source="steam_orderbook",
                    c5_listing_price=80.0,
                    c5_price_source="c5_batch",
                    c5_expected_net_price=79.2,
                    steam_real_cost=69.0,
                    expected_profit=10.2,
                    expected_roi=0.102,
                    inventory_count=1,
                    tradable_count=1,
                )
            ],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=[],
            watch_records=[],
        )
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-active",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]

        cycle = profit_trade._run_profit_trade_long_buy_cycle(
            self.settings,
            long_buy_config(
                profit_trade_allow_real_execution=True,
                profit_trade_long_buy_allow_real_execution=False,
            ),
            scanned=scanned,
            steam_client=client,
            c5_client=LongBuyC5Client(),
            new_action_guard=None,
        )

        self.assertEqual([], client.cancel_calls)
        self.assertIn(MARKET_NAME, cycle.direct_purchase_block_reasons)
        self.assertEqual(
            "active",
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
        )

    def test_real_cycle_defers_direct_purchase_cancel_to_final_buy_gate(self) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-active",
        )
        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=1,
            evaluated_count=1,
            opportunity_count=1,
            missing_price_count=0,
            skipped_count=0,
            opportunities=[
                ProfitTradeOpportunity(
                    market_hash_name=MARKET_NAME,
                    name=MARKET_NAME,
                    asset_id="asset-a",
                    steam_id="steam-a",
                    token="token-a",
                    style_token="style-a",
                    steam_buy_price=100.0,
                    steam_price_source="steam_orderbook",
                    c5_listing_price=80.0,
                    c5_price_source="c5_batch",
                    c5_expected_net_price=79.2,
                    steam_real_cost=69.0,
                    expected_profit=10.2,
                    expected_roi=0.102,
                    inventory_count=1,
                    tradable_count=1,
                )
            ],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=[],
            watch_records=[],
        )
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-active",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]
        config = long_buy_config(
            profit_trade_allow_real_execution=True,
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)

        cycle = profit_trade._run_profit_trade_long_buy_cycle(
            self.settings,
            config,
            scanned=scanned,
            steam_client=client,
            c5_client=LongBuyC5Client(),
            new_action_guard=None,
        )

        self.assertEqual([], client.cancel_calls)
        self.assertNotIn(MARKET_NAME, cycle.direct_purchase_block_reasons)
        self.assertEqual(
            "active",
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
        )

    def test_crossed_book_does_not_reprice_existing_long_buy(self) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-crossed",
        )
        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=1,
            evaluated_count=1,
            opportunity_count=0,
            missing_price_count=0,
            skipped_count=1,
            opportunities=[],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=[],
            watch_records=[
                {
                    "market_hash_name": MARKET_NAME,
                    "raw": {
                        "longBuyProposal": {
                            "eligible": False,
                            "c5ExpectedNetPrice": 50.0,
                            "balanceDiscount": 0.69,
                            "aggressiveRoi": 0.045,
                        },
                        "steamOrderbook": {"crossed": True},
                    },
                }
            ],
        )
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-crossed",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]

        cycle = profit_trade._run_profit_trade_long_buy_cycle(
            self.settings,
            config,
            scanned=scanned,
            steam_client=client,
            c5_client=LongBuyC5Client(),
            new_action_guard=None,
        )

        self.assertEqual([], cycle.replaced_order_ids)
        self.assertEqual([], cycle.created_order_ids)
        self.assertEqual([], client.cancel_calls)
        self.assertEqual(
            "active",
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
        )

    def test_crossed_book_holds_existing_long_buy_even_when_seller_is_executable(
        self,
    ) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-crossed",
        )
        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-a",
                    "marketHashName": MARKET_NAME,
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 100.0,
                    "token": "token-a",
                    "styleToken": "style-a",
                }
            ],
        }
        scanned = profit_trade.scan_profit_trade_opportunities(
            self.settings,
            config,
            inventory_payload=inventory_payload,
            # The C5 net is about 98.67; a 100.00 Steam seller price is
            # normally executable, but the public book is explicitly crossed.
            market_service=CrossedLongBuyMarketService(steam_price=100.0),
            c5_client=LongBuyC5Client(price=100.0),
        )

        self.assertEqual(1, len(scanned.opportunities))
        self.assertEqual(1, len(scanned.watch_records))
        proposal = scanned.watch_records[0]["raw"]["longBuyProposal"]
        self.assertEqual("hold", proposal["recommendedAction"])
        self.assertIn("Steam 盘口交叉", proposal["blockedReason"])

        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-crossed",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]
        cycle = profit_trade._run_profit_trade_long_buy_cycle(
            self.settings,
            config,
            scanned=scanned,
            steam_client=client,
            c5_client=LongBuyC5Client(price=100.0),
            new_action_guard=None,
        )

        self.assertIn(MARKET_NAME, cycle.direct_purchase_block_reasons)
        self.assertEqual([], client.cancel_calls)
        final_gate = profit_trade._prepare_profit_trade_long_buy_for_direct_purchase(
            self.db,
            self.settings,
            config,
            market_hash_name=MARKET_NAME,
            steam_client=client,
            new_action_guard=None,
            orderbook_crossed=True,
        )
        self.assertFalse(final_gate["ok"])
        self.assertEqual("blocked", final_gate["outcome"])
        self.assertEqual([], client.cancel_calls)
        self.assertEqual(
            "active",
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
        )

    def test_crossed_without_existing_long_buy_routes_to_original_direct_purchase(
        self,
    ) -> None:
        """Without an old order, the original ROI-gated purchase path owns the decision."""

        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-a",
                    "marketHashName": MARKET_NAME,
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 100.0,
                    "token": "token-a",
                    "styleToken": "style-a",
                }
            ],
        }
        scanned = profit_trade.scan_profit_trade_opportunities(
            self.settings,
            config,
            inventory_payload=inventory_payload,
            market_service=CrossedLongBuyMarketService(steam_price=100.0),
            c5_client=LongBuyC5Client(price=100.0),
        )

        self.assertEqual(1, len(scanned.opportunities))
        self.assertEqual(MARKET_NAME, scanned.opportunities[0].market_hash_name)
        self.assertEqual(1, len(scanned.watch_records))
        proposal = scanned.watch_records[0]["raw"]["longBuyProposal"]
        self.assertFalse(proposal["eligible"])
        self.assertEqual("none", proposal["recommendedAction"])

        client = LongBuyRemoteClient()
        cycle = profit_trade._run_profit_trade_long_buy_cycle(
            self.settings,
            config,
            scanned=scanned,
            steam_client=client,
            c5_client=LongBuyC5Client(price=100.0),
            new_action_guard=None,
        )

        self.assertNotIn(MARKET_NAME, cycle.direct_purchase_block_reasons)
        self.assertEqual([], cycle.created_order_ids)
        final_gate = profit_trade._prepare_profit_trade_long_buy_for_direct_purchase(
            self.db,
            self.settings,
            config,
            market_hash_name=MARKET_NAME,
            steam_client=client,
            new_action_guard=None,
            orderbook_crossed=True,
        )
        self.assertTrue(final_gate["ok"])
        self.assertEqual("not_present", final_gate["outcome"])

    def test_crossed_below_roi_never_creates_a_new_long_buy(self) -> None:
        """Crossed books may be observed, but cannot open new Steam buy orders."""

        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        market_service = CrossedLongBuyMarketService()
        inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-a",
                    "marketHashName": MARKET_NAME,
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 100.0,
                    "token": "token-a",
                    "styleToken": "style-a",
                }
            ],
        }
        scanned = profit_trade.scan_profit_trade_opportunities(
            self.settings,
            config,
            inventory_payload=inventory_payload,
            market_service=market_service,
            c5_client=LongBuyC5Client(price=100.0),
        )

        self.assertEqual(1, len(market_service.calls))
        self.assertEqual([], scanned.opportunities)
        self.assertEqual(1, len(scanned.watch_records))
        watch = scanned.watch_records[0]
        proposal = watch["raw"]["longBuyProposal"]
        self.assertEqual("below_min_roi", watch["execution_status"])
        self.assertFalse(proposal["eligible"])
        self.assertFalse(proposal["executionAllowed"])
        self.assertIn("Steam 盘口交叉", proposal["blockedReason"])

        # The execution-cycle guard independently blocks stale/crafted watch
        # proposals, so a future scan regression cannot create a real order.
        client = LongBuyRemoteClient()
        original_store = profit_trade.AccountStore
        profit_trade.AccountStore = EmptyAccountStore  # type: ignore[assignment]
        try:
            cycle = profit_trade._run_profit_trade_long_buy_cycle(
                self.settings,
                config,
                scanned=scanned,
                steam_client=client,
                c5_client=LongBuyC5Client(price=100.0),
                new_action_guard=None,
            )
        finally:
            profit_trade.AccountStore = original_store

        self.assertEqual([], cycle.created_order_ids, cycle.errors)
        self.assertEqual([], client.create_calls)
        self.assertEqual(0, self.db.count_live_profit_trade_long_buy_orders())

    def test_real_cycle_creates_at_most_five_of_twenty_five_slots(self) -> None:
        watch_records: list[dict] = []
        inventory_items: list[dict] = []
        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        for index in range(7):
            name = f"Test Item {index}"
            proposal = build_long_buy_proposal(
                config,
                c5_price_batch=100.0,
                orderbook_snapshot=orderbook(130.0),
                quantity=4,
            )
            proposal.update({"eligible": True, "sourceScanId": "scan-1"})
            watch_records.append(
                {
                    "market_hash_name": name,
                    "raw": {"longBuyProposal": proposal},
                }
            )
            for asset_index in range(4):
                inventory_items.append(
                    {
                        "assetId": f"asset-{index}-{asset_index}",
                        "marketHashName": name,
                        "steamId": "steam-a",
                        "ifTradable": True,
                        "token": f"token-{index}-{asset_index}",
                        "styleToken": f"style-{index}-{asset_index}",
                    }
                )
        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=len(inventory_items),
            evaluated_count=7,
            opportunity_count=0,
            missing_price_count=0,
            skipped_count=7,
            opportunities=[],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=inventory_items,
            watch_records=watch_records,
        )
        client = LongBuyRemoteClient()
        original_store = profit_trade.AccountStore
        profit_trade.AccountStore = EmptyAccountStore  # type: ignore[assignment]
        try:
            cycle = profit_trade._run_profit_trade_long_buy_cycle(
                self.settings,
                config,
                scanned=scanned,
                steam_client=client,
                c5_client=LongBuyC5Client(),
                new_action_guard=None,
            )
        finally:
            profit_trade.AccountStore = original_store
        self.assertEqual(5, len(cycle.created_order_ids), cycle.errors)
        self.assertEqual(5, len(client.create_calls))
        self.assertTrue(
            all(call["quantity"] == 4 for call in client.create_calls)
        )

    def test_equal_roi_creation_prefers_c5_liquidity_then_executable_assets(self) -> None:
        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
            profit_trade_long_buy_max_active_orders=1,
            profit_trade_long_buy_create_fraction_per_cycle=1.0,
        )
        profit_trade.save_strategy_config(self.settings, config)
        slow_name = "Test Item Slow"
        liquid_name = "Test Item Liquid"
        watch_records: list[dict] = []
        inventory_items: list[dict] = []
        for name, recent_sold, on_sale, purchase_count, asset_count in (
            (slow_name, 1, 1, 1, 4),
            (liquid_name, 10, 20, 15, 1),
        ):
            proposal = build_long_buy_proposal(
                config,
                c5_price_batch=100.0,
                orderbook_snapshot=orderbook(130.0),
                quantity=asset_count,
            )
            proposal.update({"eligible": True, "sourceScanId": "scan-liquidity"})
            watch_records.append(
                {
                    "market_hash_name": name,
                    "c5_recent_sold_count": recent_sold,
                    "c5_on_sale_count": on_sale,
                    "c5_purchase_count": purchase_count,
                    "raw": {
                        "longBuyProposal": proposal,
                        "manualExecutableQuantity": asset_count,
                    },
                }
            )
            for asset_index in range(asset_count):
                inventory_items.append(
                    {
                        "assetId": f"{name}-{asset_index}",
                        "marketHashName": name,
                        "steamId": "steam-a",
                        "ifTradable": True,
                        "token": f"token-{name}-{asset_index}",
                        "styleToken": f"style-{name}-{asset_index}",
                    }
                )
        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=len(inventory_items),
            evaluated_count=2,
            opportunity_count=0,
            missing_price_count=0,
            skipped_count=2,
            opportunities=[],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=inventory_items,
            watch_records=watch_records,
        )
        client = LongBuyRemoteClient()
        original_store = profit_trade.AccountStore
        profit_trade.AccountStore = EmptyAccountStore  # type: ignore[assignment]
        try:
            cycle = profit_trade._run_profit_trade_long_buy_cycle(
                self.settings,
                config,
                scanned=scanned,
                steam_client=client,
                c5_client=LongBuyC5Client(),
                new_action_guard=None,
            )
        finally:
            profit_trade.AccountStore = original_store

        self.assertEqual(1, len(cycle.created_order_ids), cycle.errors)
        self.assertEqual(liquid_name, client.create_calls[0]["market_hash_name"])

    def test_unsafe_order_is_cancelled_and_rebuilt_lower_same_cycle(self) -> None:
        order_id = self.db.create_profit_trade_long_buy_order(
            market_hash_name=MARKET_NAME,
            steam_account_id="account-a",
            steam_id="steam-a",
            create_request_id="unsafe-order",
            bid_price_cents=14000,
            quantity=2,
            c5_price_batch=110.0,
            c5_expected_net_price=108.9,
            balance_discount=0.69,
            standard_roi=0.05,
            aggressive_roi=0.045,
            standard_safe_price_cents=14716,
            aggressive_safe_price_cents=14816,
            competitor_buy_price_cents=13900,
            competitor_buy_status="raw",
            worst_case_roi=0.0879,
            source_scan_id="scan-old",
            wallet_before=1000.0,
        )
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-old",
        )
        config = long_buy_config(
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        proposal = build_long_buy_proposal(
            config,
            c5_price_batch=100.0,
            orderbook_snapshot=orderbook(130.0),
            quantity=2,
            own_price_cents=[14000],
        )
        proposal.update({"eligible": False, "sourceScanId": "scan-new"})
        scanned = ProfitTradeScanReport(
            generated_at="2026-07-28T01:00:00+00:00",
            inventory_source="fixture",
            inventory_count=2,
            evaluated_count=1,
            opportunity_count=0,
            missing_price_count=0,
            skipped_count=1,
            opportunities=[],
            created_trade_ids=[],
            locked_trade_ids=[],
            notes=[],
            inventory_items=[
                {
                    "assetId": f"asset-{index}",
                    "marketHashName": MARKET_NAME,
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "token": f"token-{index}",
                    "styleToken": f"style-{index}",
                }
                for index in range(2)
            ],
            watch_records=[
                {
                    "market_hash_name": MARKET_NAME,
                    "raw": {
                        "longBuyProposal": proposal,
                        "steamOrderbook": orderbook(130.0),
                    },
                }
            ],
        )
        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-old",
                "market_hash_name": MARKET_NAME,
                "price": 14000,
                "quantity": 2,
                "quantity_remaining": 2,
            }
        ]
        original_store = profit_trade.AccountStore
        profit_trade.AccountStore = EmptyAccountStore  # type: ignore[assignment]
        try:
            cycle = profit_trade._run_profit_trade_long_buy_cycle(
                self.settings,
                config,
                scanned=scanned,
                steam_client=client,
                c5_client=LongBuyC5Client(price=100.0),
                new_action_guard=None,
            )
        finally:
            profit_trade.AccountStore = original_store
        self.assertEqual([order_id], cycle.replaced_order_ids, cycle.errors)
        old_order = self.db.get_profit_trade_long_buy_order(order_id)
        replacement = self.db.get_profit_trade_long_buy_order(
            cycle.created_order_ids[0]
        )
        self.assertEqual("cancelled", old_order["state"])
        self.assertEqual("active", replacement["state"])
        self.assertLess(replacement["bid_price_cents"], 14000)
        self.assertEqual(14000, replacement["previous_bid_price_cents"])
        self.assertEqual(order_id, replacement["replaces_order_id"])

    def test_min_item_value_does_not_hide_unsafe_existing_long_buy(self) -> None:
        order_id = self._create_order(quantity=2)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-old",
        )
        config = long_buy_config(
            profit_trade_min_item_value=5.0,
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)

        scanned, market_service = self._scan_low_value_existing_long_buy(config)

        self.assertEqual(1, len(market_service.calls))
        self.assertEqual([], scanned.opportunities)
        self.assertEqual(1, len(scanned.watch_records))
        watch = scanned.watch_records[0]
        self.assertEqual(MARKET_NAME, watch["market_hash_name"])
        self.assertEqual("below_min_item_value", watch["execution_status"])
        self.assertIsNotNone(watch["raw"]["longBuyOrder"])
        self.assertIsNotNone(watch["raw"]["longBuyProposal"])

        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-old",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 2,
                "quantity_remaining": 2,
            }
        ]
        original_store = profit_trade.AccountStore
        profit_trade.AccountStore = EmptyAccountStore  # type: ignore[assignment]
        try:
            cycle = profit_trade._run_profit_trade_long_buy_cycle(
                self.settings,
                config,
                scanned=scanned,
                steam_client=client,
                c5_client=LongBuyC5Client(price=4.0),
                new_action_guard=None,
            )
        finally:
            profit_trade.AccountStore = original_store

        self.assertEqual([order_id], cycle.replaced_order_ids, cycle.errors)
        self.assertEqual(["buy-old"], client.cancel_calls)
        self.assertEqual(1, len(client.create_calls))
        old_order = self.db.get_profit_trade_long_buy_order(order_id)
        replacement = self.db.get_profit_trade_long_buy_order(
            cycle.created_order_ids[0]
        )
        self.assertEqual("cancelled", old_order["state"])
        self.assertEqual("active", replacement["state"])
        self.assertLess(replacement["bid_price_cents"], 13400)
        self.assertEqual(order_id, replacement["replaces_order_id"])

    def test_min_item_value_existing_long_buy_stays_visible_in_observation_mode(
        self,
    ) -> None:
        order_id = self._create_order(quantity=1)
        self.db.update_profit_trade_long_buy_order(
            order_id,
            event_type="remote_created",
            state="active",
            buy_order_id="buy-old",
        )
        config = long_buy_config(
            profit_trade_min_item_value=5.0,
            profit_trade_long_buy_allow_real_execution=False,
        )
        profit_trade.save_strategy_config(self.settings, config)

        scanned, _ = self._scan_low_value_existing_long_buy(config)
        self.assertEqual([], scanned.opportunities)
        self.assertEqual(1, len(scanned.watch_records))
        watch = scanned.watch_records[0]
        self.assertEqual("below_min_item_value", watch["execution_status"])
        self.assertEqual("hold", watch["raw"]["longBuyProposal"]["recommendedAction"])

        client = LongBuyRemoteClient()
        client.active_orders = [
            {
                "buy_orderid": "buy-old",
                "market_hash_name": MARKET_NAME,
                "price": 13400,
                "quantity": 1,
                "quantity_remaining": 1,
            }
        ]
        cycle = profit_trade._run_profit_trade_long_buy_cycle(
            self.settings,
            config,
            scanned=scanned,
            steam_client=client,
            c5_client=LongBuyC5Client(price=4.0),
            new_action_guard=None,
        )

        self.assertEqual([], cycle.created_order_ids)
        self.assertEqual([], cycle.replaced_order_ids)
        self.assertEqual([], client.create_calls)
        self.assertEqual([], client.cancel_calls)
        self.assertEqual(
            "active",
            self.db.get_profit_trade_long_buy_order(order_id)["state"],
        )

    def test_min_item_value_still_blocks_new_low_value_long_buy(self) -> None:
        config = long_buy_config(
            profit_trade_min_item_value=5.0,
            profit_trade_long_buy_allow_real_execution=True,
        )
        profit_trade.save_strategy_config(self.settings, config)
        market_service = LowValueLongBuyMarketService()
        scanned = profit_trade.scan_profit_trade_opportunities(
            self.settings,
            config,
            # It passes the inventory-reference prefilter, then must still be
            # rejected by the current price_batch-based evaluation because no
            # live managed long-buy order exists.
            inventory_payload=self._low_value_inventory_payload(
                reference_price=100.0,
            ),
            market_service=market_service,
            c5_client=LongBuyC5Client(price=4.0),
        )
        client = LongBuyRemoteClient()
        cycle = profit_trade._run_profit_trade_long_buy_cycle(
            self.settings,
            config,
            scanned=scanned,
            steam_client=client,
            c5_client=LongBuyC5Client(price=4.0),
            new_action_guard=None,
        )

        self.assertEqual(1, len(market_service.calls))
        self.assertEqual([], scanned.opportunities)
        self.assertEqual([], scanned.watch_records)
        self.assertEqual([], cycle.created_order_ids)
        self.assertEqual([], client.create_calls)
        self.assertEqual(0, self.db.count_live_profit_trade_long_buy_orders())


if __name__ == "__main__":
    unittest.main()
