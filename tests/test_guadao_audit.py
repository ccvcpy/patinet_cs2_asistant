from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cs2_assistant.config import Settings
from cs2_assistant.services.guadao_audit import (
    SUPPORTED_EXPORT_FORMATS,
    TERMINAL_STATUSES,
    cancel_guadao_audit_run,
    create_guadao_audit_run,
    export_guadao_audit,
    get_guadao_audit_run,
    initialize_guadao_audit_schema,
    list_guadao_audit_rows,
    retry_guadao_audit_run,
    run_guadao_audit,
)


START = "2026-07-19T15:20:00+08:00"
END = "2026-07-28T23:50:00+08:00"
SOLD_AT = "2026-07-20T08:00:00+08:00"
PURCHASED_AT = "2026-07-21T08:00:00+08:00"


class GuadaoAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "assistant.db"
        self.settings = Settings(db_path=self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE pool_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_hash_name TEXT NOT NULL,
                strategy TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                expected_price REAL,
                actual_price REAL,
                asset_id TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE profit_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_no TEXT,
                market_hash_name TEXT,
                status TEXT,
                b_asset_id TEXT,
                steam_listing_id TEXT,
                steam_buy_price REAL,
                steam_balance_discount REAL,
                steam_real_cost REAL,
                note TEXT,
                created_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE inventory_pool (market_hash_name TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE inventory_assets (asset_id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE strategy_config (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO inventory_pool VALUES ('sentinel-item', 'holding');
            INSERT INTO inventory_assets VALUES ('sentinel-asset', 'tradable');
            INSERT INTO strategy_config VALUES ('guadaoMaxListingRatio', '0.70');
            """
        )
        self.conn.commit()
        initialize_guadao_audit_schema(self.settings)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _add_sell(
        self,
        *,
        item: str = "Kilowatt Case",
        listing_id: str = "listing-1",
        purchase_id: str = "sale-purchase-1",
        asset_id: str = "asset-1",
        net: str = "8.69",
        include_official_time: bool = True,
    ) -> int:
        note = {
            "listingId": listing_id,
            "steamPurchaseId": purchase_id,
            "steamAccountId": "steam-1",
            "steamListPrice": "10.00",
            "steamSellerNetPrice": net,
        }
        if include_official_time:
            note["steamSoldAt"] = SOLD_AT
        cursor = self.conn.execute(
            """
            INSERT INTO pool_operations (
                market_hash_name, strategy, operation_type, status, quantity,
                expected_price, actual_price, asset_id, note, created_at, completed_at
            ) VALUES (?, 'guadao', 'sell_on_steam', 'sold', 1, 10, 10, ?, ?, ?, ?)
            """,
            (
                item,
                asset_id,
                json.dumps(note, ensure_ascii=False),
                "2026-07-19T00:00:00+00:00",
                # Deliberately inside the range. Strict audit must never use it
                # when steamSoldAt is absent.
                "2026-07-20T00:01:00+00:00",
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def _add_rebuy(
        self,
        sell_id: int,
        *,
        order_id: str = "c5-order-1",
        status: str = "completed",
        expected: str = "5.00",
        actual: str = "5.01",
        note_extra: dict[str, object] | None = None,
    ) -> int:
        note: dict[str, object] = {
            "sourceSellOperationId": sell_id,
            "c5OrderId": order_id,
            "c5FinalStatus": "c5_success" if status == "completed" else status,
        }
        note.update(note_extra or {})
        cursor = self.conn.execute(
            """
            INSERT INTO pool_operations (
                market_hash_name, strategy, operation_type, status, quantity,
                expected_price, actual_price, note, created_at, completed_at
            ) VALUES ('Kilowatt Case', 'guadao', 'rebuy_on_c5', ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                status,
                expected,
                actual,
                json.dumps(note, ensure_ascii=False),
                "2026-07-20T00:10:00+00:00",
                "2026-07-20T00:20:00+00:00" if status == "completed" else None,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def _add_profit_purchase(self) -> None:
        note = {
            "steamAccountId": "steam-1",
            "steamPurchaseReceipt": {
                "purchaseId": "buy-purchase-1",
                "listingId": "buy-listing-1",
            },
        }
        self.conn.execute(
            """
            INSERT INTO profit_trades (
                trade_no, market_hash_name, status, b_asset_id,
                steam_listing_id, steam_buy_price, steam_balance_discount,
                steam_real_cost, note, created_at
            ) VALUES ('PT-1', 'Purchased Item', 'steam_bought', 'buy-asset-1',
                      'buy-listing-1', 20.00, 0.70, 14.00, ?, ?)
            """,
            (json.dumps(note), PURCHASED_AT),
        )
        self.conn.commit()

    @staticmethod
    def _steam_payload(*, coverage: bool = True, net: str = "8.69") -> dict[str, object]:
        return {
            "coverageComplete": coverage,
            "accounts": [
                {
                    "accountId": "steam-1",
                    "coverageComplete": coverage,
                    "sales": [
                        {
                            "accountId": "steam-1",
                            "listingId": "listing-1",
                            "purchaseId": "sale-purchase-1",
                            "assetId": "asset-1",
                            "marketHashName": "Kilowatt Case",
                            "soldAt": SOLD_AT,
                            "grossAmount": "10.00",
                            "netAmount": net,
                            "currencyId": 23,
                            "quantity": 1,
                        }
                    ],
                    "purchases": [
                        {
                            "accountId": "steam-1",
                            "listingId": "buy-listing-1",
                            "purchaseId": "buy-purchase-1",
                            "assetId": "buy-asset-1",
                            "marketHashName": "Purchased Item",
                            "purchasedAt": PURCHASED_AT,
                            "paidAmount": "20.00",
                            "currencyId": 23,
                        }
                    ],
                }
            ],
            "errors": [] if coverage else ["history pagination incomplete"],
        }

    @staticmethod
    def _c5_payload(*orders: dict[str, object], coverage: bool = True) -> dict[str, object]:
        return {
            "coverageComplete": coverage,
            "orders": list(orders),
            "errors": [] if coverage else ["buyer_order_detail unavailable"],
        }

    @staticmethod
    def _balance_payload(total: str = "2491.61", *, coverage: bool = True) -> dict[str, object]:
        return {
            "coverageComplete": coverage,
            "accounts": [
                {
                    "accountId": "steam-1",
                    "availableBalance": total,
                    "pendingBalance": "0.00",
                    "currencyId": 23,
                    "coverageComplete": coverage,
                }
            ],
            "errors": [] if coverage else ["wallet unavailable"],
        }

    def _new_run(self, *, reported_ratio: str | None = None) -> str:
        run = create_guadao_audit_run(
            self.settings,
            start_at=START,
            end_at=END,
            account_ids=["steam-1"],
            expected_account_count=1,
            reported_comprehensive_ratio=reported_ratio,
        )
        return str(run["requestId"])

    def _run(
        self,
        request_id: str,
        *,
        steam: dict[str, object],
        c5: dict[str, object],
        balance: dict[str, object],
    ) -> dict[str, object]:
        return run_guadao_audit(
            self.settings,
            request_id,
            steam_evidence_provider=lambda **_: steam,
            c5_evidence_provider=lambda **_: c5,
            balance_evidence_provider=lambda **_: balance,
        )

    def test_complete_matching_evidence_passes_and_uses_remote_c5_price(self) -> None:
        sell_id = self._add_sell()
        self._add_rebuy(sell_id, actual="5.01")
        self._add_profit_purchase()
        ratio = str(Decimal("5.00") / Decimal("8.69"))
        request_id = self._new_run(reported_ratio=ratio)

        result = self._run(
            request_id,
            steam=self._steam_payload(),
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "statusName": "completed",
                    "marketHashName": "Kilowatt Case",
                    "actualAmount": "5.00",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload(),
        )

        self.assertEqual("passed", result["status"])
        self.assertTrue(result["summary"]["evidenceComplete"])
        tables = list_guadao_audit_rows(self.settings, request_id)
        self.assertEqual(1, len(tables["steam_sales"]))
        self.assertEqual(1, len(tables["rebuys"]))
        self.assertEqual("c5_success", tables["rebuys"][0]["destination"])
        # The local submitted price is 5.01; strict result must use detail=5.00.
        self.assertEqual("5.00", tables["rebuys"][0]["effectiveAmount"])
        self.assertEqual("0.00", tables["wallet_discount"][0]["balanceDifference"])

    def test_complete_evidence_with_real_difference_fails(self) -> None:
        sell_id = self._add_sell(net="8.68")
        self._add_rebuy(sell_id)
        self._add_profit_purchase()
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )

        result = self._run(
            request_id,
            steam=self._steam_payload(net="8.69"),
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload(),
        )

        self.assertEqual("failed", result["status"])
        sale_row = list_guadao_audit_rows(
            self.settings, request_id, table="steam_sales"
        )[0]
        self.assertEqual("failed", sale_row["verdict"])
        self.assertEqual("-0.01", sale_row["netDifference"])

    def test_missing_official_coverage_is_inconclusive_even_when_values_differ(self) -> None:
        sell_id = self._add_sell(net="1.00")
        self._add_rebuy(sell_id)
        self._add_profit_purchase()
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )

        result = self._run(
            request_id,
            steam=self._steam_payload(coverage=False),
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload(),
        )

        self.assertEqual("inconclusive", result["status"])
        self.assertFalse(result["summary"]["evidenceComplete"])
        self.assertTrue(result["summary"]["evidenceGaps"])

    def test_missing_steam_sold_at_never_falls_back_to_completed_at(self) -> None:
        self._add_sell(include_official_time=False)
        request_id = self._new_run(reported_ratio="0")
        result = self._run(
            request_id,
            steam={
                "coverageComplete": True,
                "accounts": [
                    {
                        "accountId": "steam-1",
                        "coverageComplete": True,
                        "sales": [],
                        "purchases": [],
                    }
                ],
            },
            c5=self._c5_payload(),
            balance=self._balance_payload("2502.92"),
        )

        self.assertEqual("inconclusive", result["status"])
        rows = list_guadao_audit_rows(self.settings, request_id, table="steam_sales")
        self.assertEqual(1, len(rows))
        self.assertEqual("local_sale_missing_official_time", rows[0]["reason"])

    def test_matched_sale_missing_steam_sold_at_is_inconclusive(self) -> None:
        sell_id = self._add_sell(include_official_time=False)
        self._add_rebuy(sell_id)
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )
        steam = self._steam_payload()
        steam["accounts"][0]["purchases"] = []

        result = self._run(
            request_id,
            steam=steam,
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "marketHashName": "Kilowatt Case",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload("2511.61"),
        )

        self.assertEqual("inconclusive", result["status"])
        row = result["tables"]["steam_sales"][0]
        self.assertEqual("inconclusive", row["verdict"])
        self.assertIn("program_official_sale_time_missing", row["reason"])

    def test_missing_local_sale_amount_is_inconclusive_not_failed(self) -> None:
        sell_id = self._add_sell()
        row = self.conn.execute(
            "SELECT note FROM pool_operations WHERE id = ?", (sell_id,)
        ).fetchone()
        note = json.loads(row["note"])
        note.pop("steamSellerNetPrice")
        self.conn.execute(
            "UPDATE pool_operations SET note = ? WHERE id = ?",
            (json.dumps(note, ensure_ascii=False), sell_id),
        )
        self.conn.commit()
        self._add_rebuy(sell_id)
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )
        steam = self._steam_payload()
        steam["accounts"][0]["purchases"] = []

        result = self._run(
            request_id,
            steam=steam,
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "marketHashName": "Kilowatt Case",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload("2511.61"),
        )

        self.assertEqual("inconclusive", result["status"])
        row = result["tables"]["steam_sales"][0]
        self.assertEqual("inconclusive", row["verdict"])
        self.assertIn("program_net_missing", row["reason"])

    def test_missing_expected_c5_detail_is_inconclusive(self) -> None:
        sell_id = self._add_sell()
        self._add_rebuy(sell_id)
        request_id = self._new_run(reported_ratio="0")
        steam = self._steam_payload()
        steam["accounts"][0]["purchases"] = []

        result = self._run(
            request_id,
            steam=steam,
            c5=self._c5_payload(),
            balance=self._balance_payload("2511.61"),
        )

        self.assertEqual("inconclusive", result["status"])
        row = result["tables"]["rebuys"][0]
        self.assertEqual("inconclusive", row["verdict"])
        self.assertIn("c5_order_detail_missing", row["reason"])

    def test_conflicting_sale_identifier_is_a_real_difference(self) -> None:
        sell_id = self._add_sell(purchase_id="wrong-purchase-id")
        self._add_rebuy(sell_id)
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )
        steam = self._steam_payload()
        steam["accounts"][0]["purchases"] = []

        result = self._run(
            request_id,
            steam=steam,
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "marketHashName": "Kilowatt Case",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload("2511.61"),
        )

        self.assertEqual("failed", result["status"])
        row = result["tables"]["steam_sales"][0]
        self.assertEqual("failed", row["verdict"])
        self.assertIn("purchase_id_mismatch", row["reason"])

    def test_replacement_chain_counts_only_final_success(self) -> None:
        sell_id = self._add_sell()
        failed_id = self._add_rebuy(
            sell_id,
            order_id="c5-old",
            status="c5_failed",
            expected="4.00",
            actual="4.00",
            note_extra={"c5FinalStatus": "c5_failed"},
        )
        self._add_rebuy(
            sell_id,
            order_id="c5-new",
            status="completed",
            expected="5.00",
            actual="5.02",
            note_extra={"replacementForRebuyOperationId": failed_id},
        )
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )

        result = self._run(
            request_id,
            steam={
                **self._steam_payload(),
                "accounts": [
                    {
                        **self._steam_payload()["accounts"][0],
                        "purchases": [],
                    }
                ],
            },
            c5=self._c5_payload(
                {
                    "orderId": "c5-old",
                    "status": 11,
                    "actualAmount": "4.00",
                    "source": "buyer_order_detail",
                },
                {
                    "orderId": "c5-new",
                    "status": 10,
                    "actualAmount": "5.00",
                    "source": "buyer_order_detail",
                },
            ),
            balance=self._balance_payload("2511.61"),
        )

        self.assertEqual("passed", result["status"])
        rows = list_guadao_audit_rows(self.settings, request_id, table="rebuys")
        self.assertEqual(1, len(rows))
        self.assertEqual("5.00", rows[0]["effectiveAmount"])
        self.assertEqual(2, rows[0]["attemptCount"])

    def test_default_c5_provider_accepts_one_resolved_order_alias(self) -> None:
        sell_id = self._add_sell()
        self._add_rebuy(
            sell_id,
            order_id="asset-order-alias",
            note_extra={"c5TradeOrderId": "trade-order-alias"},
        )
        self.settings.c5_api_key = "test-key"
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )
        steam = self._steam_payload()
        steam["accounts"][0]["purchases"] = []

        class FakeC5Client:
            def __init__(self) -> None:
                self.detail_calls: list[str] = []

            def buyer_order_status(self, **_: object) -> dict[str, object]:
                return {"list": [], "pages": 1}

            def buyer_order_detail(self, order_id: str) -> dict[str, object]:
                self.detail_calls.append(order_id)
                if order_id == "asset-order-alias":
                    raise RuntimeError("this alias is not accepted by detail")
                return {
                    "orderId": "canonical-order-id",
                    "status": 10,
                    "actualPay": "5.00",
                    "marketHashName": "Kilowatt Case",
                }

        fake_client = FakeC5Client()
        with patch(
            "cs2_assistant.services.guadao_audit.C5GameClient",
            return_value=fake_client,
        ):
            result = run_guadao_audit(
                self.settings,
                request_id,
                steam_evidence_provider=lambda **_: steam,
                balance_evidence_provider=lambda **_: self._balance_payload("2511.61"),
            )

        self.assertEqual("passed", result["status"])
        self.assertEqual(
            ["asset-order-alias", "trade-order-alias"], fake_client.detail_calls
        )
        self.assertEqual(
            "5.00", result["tables"]["rebuys"][0]["effectiveAmount"]
        )

    def test_default_steam_providers_remain_p3_shared_scheduler_calls(self) -> None:
        request_id = self._new_run(reported_ratio="0")
        account = SimpleNamespace(
            id="steam-1",
            name="main",
            steam_id64="76561198000000001",
            cookies="sessionid=test",
            identity_secret=None,
            device_id=None,
        )
        account_store = SimpleNamespace(list_accounts=lambda: [account])

        class FakeSteamClient:
            def __init__(self) -> None:
                self.history_calls: list[dict[str, object]] = []
                self.balance_calls: list[dict[str, object]] = []

            def market_history(self, **kwargs: object) -> dict[str, object]:
                self.history_calls.append(dict(kwargs))
                return {
                    "events": [],
                    "purchases": {},
                    "assets": {},
                    "total_count": 0,
                }

            def wallet_balance(self, **kwargs: object) -> dict[str, object]:
                self.balance_calls.append(dict(kwargs))
                return {
                    "balance": "2502.92",
                    "delayed_balance": "0.00",
                    "currency_id": 23,
                    "currency": "CNY",
                }

        fake_client = FakeSteamClient()
        with (
            patch(
                "cs2_assistant.services.guadao_audit.AccountStore",
                return_value=account_store,
            ),
            patch(
                "cs2_assistant.services.guadao_audit.SteamMarketClient",
                return_value=fake_client,
            ),
        ):
            result = run_guadao_audit(
                self.settings,
                request_id,
                c5_evidence_provider=lambda **_: self._c5_payload(),
            )

        self.assertEqual("passed", result["status"])
        self.assertTrue(fake_client.history_calls)
        self.assertTrue(fake_client.balance_calls)
        self.assertTrue(
            all(call.get("safety_terminal", False) is False for call in fake_client.history_calls)
        )
        self.assertTrue(
            all(call.get("safety_terminal", False) is False for call in fake_client.balance_calls)
        )

    def test_terminal_status_contract_matches_frontend(self) -> None:
        self.assertEqual(
            {"passed", "failed", "inconclusive", "cancelled"},
            TERMINAL_STATUSES,
        )

    def test_remote_c5_item_mismatch_breaks_per_item_conservation(self) -> None:
        sell_id = self._add_sell()
        self._add_rebuy(sell_id)
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )
        steam = self._steam_payload()
        steam["accounts"][0]["purchases"] = []

        result = self._run(
            request_id,
            steam=steam,
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "marketHashName": "Wrong Item",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload("2511.61"),
        )

        self.assertEqual("failed", result["status"])
        rebuy = result["tables"]["rebuys"][0]
        self.assertEqual("Wrong Item", rebuy["marketHashName"])
        self.assertIn("c5_item_mismatch", rebuy["reason"])
        conservation = {
            row["marketHashName"]: row["quantityDifference"]
            for row in result["tables"]["item_conservation"]
        }
        self.assertEqual({"Kilowatt Case": 1, "Wrong Item": -1}, conservation)

    def test_all_six_rebuy_destinations_and_current_price_rules(self) -> None:
        cases = (
            {
                "name": "manual",
                "status": "completed",
                "order_id": "",
                "note": {
                    "c5FinalStatus": "manual_external_completed",
                    "manualExternalRebuyCompletedAt": SOLD_AT,
                    "manualExternalRebuySource": "other-platform",
                },
                "c5": self._c5_payload(),
                "reported": str(Decimal("5.01") / Decimal("8.69")),
                "destination": "manual_complete",
                "amount": "5.01",
                "run_status": "passed",
            },
            {
                "name": "delivery",
                "status": "delivery_pending",
                "order_id": "c5-order-1",
                "note": {"c5FinalStatus": "pending"},
                "c5": self._c5_payload(
                    {
                        "orderId": "c5-order-1",
                        "status": 1,
                        "actualPay": "5.00",
                        "marketHashName": "Kilowatt Case",
                        "source": "buyer_order_detail",
                    }
                ),
                "reported": str(Decimal("5.00") / Decimal("8.69")),
                "destination": "c5_delivery_pending",
                "amount": "5.00",
                "run_status": "passed",
            },
            {
                "name": "pending-refrozen",
                "status": "pending",
                "order_id": "",
                "note": {
                    "manualRebuyRefrozenPrice": "5.00",
                    "manualRebuyRefrozenRatio": str(Decimal("5.00") / Decimal("8.69")),
                },
                "c5": self._c5_payload(),
                "reported": str(Decimal("5.00") / Decimal("8.69")),
                "destination": "pending_rebuy",
                "amount": "5.00",
                "run_status": "passed",
            },
            {
                "name": "submission-unconfirmed",
                "status": "c5_submission_unconfirmed",
                "order_id": "",
                "note": {"c5FinalStatus": "c5_submission_unconfirmed"},
                "c5": self._c5_payload(),
                "reported": "0",
                "destination": "c5_submission_unconfirmed",
                "amount": None,
                "run_status": "passed",
            },
            {
                "name": "exception",
                "status": "manual_required",
                "order_id": "",
                "note": {"c5FinalStatus": "manual_required"},
                "c5": self._c5_payload(),
                "reported": "0",
                "destination": "exception",
                "amount": None,
                "run_status": "failed",
            },
        )

        for case in cases:
            with self.subTest(case=case["name"]):
                self.conn.execute("DELETE FROM pool_operations")
                self.conn.execute("DELETE FROM profit_trades")
                self.conn.commit()
                sell_id = self._add_sell()
                self._add_rebuy(
                    sell_id,
                    order_id=case["order_id"],
                    status=case["status"],
                    expected="4.00",
                    note_extra=case["note"],
                )
                request_id = self._new_run(reported_ratio=case["reported"])
                steam = self._steam_payload()
                steam["accounts"][0]["purchases"] = []

                result = self._run(
                    request_id,
                    steam=steam,
                    c5=case["c5"],
                    balance=self._balance_payload("2511.61"),
                )

                self.assertEqual(case["run_status"], result["status"])
                row = result["tables"]["rebuys"][0]
                self.assertEqual(case["destination"], row["destination"])
                self.assertEqual(case["amount"], row.get("effectiveAmount"))

    def test_wallet_and_discount_formulas_use_decimal_and_saved_purchase_discount(self) -> None:
        sell_id = self._add_sell()
        self._add_rebuy(sell_id)
        self._add_profit_purchase()
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )
        result = self._run(
            request_id,
            steam=self._steam_payload(),
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload(),
        )

        wallet = result["tables"]["wallet_discount"][0]
        self.assertEqual("2502.92", wallet["initialBalance"])
        self.assertEqual("8.69", wallet["officialSaleNet"])
        self.assertEqual("20.00", wallet["officialPurchaseSpend"])
        self.assertEqual("2491.61", wallet["predictedEndingBalance"])
        self.assertEqual("14.0000", wallet["purchaseRealCost"])
        self.assertEqual("1746.4740", wallet["predictedEndingRealValue"])
        expected_discount = Decimal("1746.474") / Decimal("2491.61")
        self.assertEqual(expected_discount, Decimal(wallet["endingBalanceDiscount"]))

    def test_wallet_summary_honors_the_configured_cent_tolerance(self) -> None:
        sell_id = self._add_sell()
        self._add_rebuy(sell_id)
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )
        steam = self._steam_payload()
        steam["accounts"][0]["purchases"] = []

        result = self._run(
            request_id,
            steam=steam,
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "marketHashName": "Kilowatt Case",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload("2511.62"),
        )

        self.assertEqual("passed", result["status"])
        self.assertEqual("0.01", result["tables"]["wallet_discount"][0]["balanceDifference"])
        self.assertTrue(result["summary"]["walletReconciled"])

    def test_service_never_writes_trading_tables(self) -> None:
        sell_id = self._add_sell()
        self._add_rebuy(sell_id)
        self._add_profit_purchase()
        table_names = (
            "pool_operations",
            "profit_trades",
            "inventory_pool",
            "inventory_assets",
            "strategy_config",
        )
        before = {
            name: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in table_names
        }
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )

        self._run(
            request_id,
            steam=self._steam_payload(),
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload(),
        )

        after = {
            name: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in table_names
        }
        self.assertEqual(before, after)

    def test_cancel_retry_and_export_contract(self) -> None:
        cancelled_id = self._new_run(reported_ratio="0")
        cancelled = cancel_guadao_audit_run(self.settings, cancelled_id)
        self.assertEqual("cancelled", cancelled["status"])

        sell_id = self._add_sell()
        self._add_rebuy(sell_id)
        request_id = self._new_run(
            reported_ratio=str(Decimal("5.00") / Decimal("8.69"))
        )
        self._run(
            request_id,
            steam={
                **self._steam_payload(),
                "accounts": [
                    {
                        **self._steam_payload()["accounts"][0],
                        "purchases": [],
                    }
                ],
            },
            c5=self._c5_payload(
                {
                    "orderId": "c5-order-1",
                    "status": 10,
                    "actualAmount": "5.00",
                    "source": "buyer_order_detail",
                }
            ),
            balance=self._balance_payload("2511.61"),
        )

        for format_name in ("json", "csv", "markdown"):
            exported = export_guadao_audit(self.settings, request_id, format_name)
            self.assertTrue(exported["content"])
            self.assertIn(request_id, exported["filename"])
            if format_name == "markdown":
                self.assertIn("endingBalanceDiscount", exported["content"])
                self.assertIn("c5Success", exported["content"])
        self.assertEqual({"json", "csv", "markdown"}, SUPPORTED_EXPORT_FORMATS)
        with self.assertRaises(ValueError):
            export_guadao_audit(self.settings, request_id, "xlsx")

        retried = retry_guadao_audit_run(self.settings, request_id)
        self.assertEqual("pending", retried["status"])
        self.assertEqual(request_id, retried["retryOfRequestId"])
        self.assertNotEqual(request_id, retried["requestId"])
        self.assertEqual("passed", get_guadao_audit_run(self.settings, request_id)["status"])


if __name__ == "__main__":
    unittest.main()
