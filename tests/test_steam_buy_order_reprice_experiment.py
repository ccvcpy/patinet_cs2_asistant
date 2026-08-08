from __future__ import annotations

import unittest

from cs2_assistant.diagnostics.steam_buy_order_reprice_experiment import (
    build_create_buy_order_data,
    buy_order_snapshot,
    candidate_order_snapshots,
    classify_transition,
    orderbook_min_sell_cents,
    public_response_payload,
)


class SteamBuyOrderRepriceExperimentTest(unittest.TestCase):
    def test_buy_order_snapshot_reads_current_mylistings_shape(self) -> None:
        row = {
            "buy_orderid": "123",
            "price": "21",
            "quantity": "1",
            "quantity_remaining": "1",
            "description": {"market_hash_name": "Glove Case"},
        }
        self.assertEqual(
            {
                "buyOrderId": "123",
                "marketHashName": "Glove Case",
                "priceCents": 21,
                "priceSource": "price",
                "quantity": "1",
                "quantityRemaining": "1",
            },
            buy_order_snapshot(row),
        )

    def test_candidate_orders_filter_without_retaining_raw_payload(self) -> None:
        payload = {
            "buy_orders": [
                {
                    "buy_orderid": "2",
                    "price": 22,
                    "description": {"market_hash_name": "Other"},
                },
                {
                    "buy_orderid": "1",
                    "price": 21,
                    "description": {"market_hash_name": "Glove Case"},
                },
            ]
        }
        rows = candidate_order_snapshots(payload, "Glove Case")
        self.assertEqual(1, len(rows))
        self.assertEqual("1", rows[0]["buyOrderId"])
        self.assertNotIn("description", rows[0])

    def test_classification_distinguishes_all_identity_transitions(self) -> None:
        unchanged = [{"buyOrderId": "old", "priceCents": 21}]
        changed = [{"buyOrderId": "old", "priceCents": 22}]
        second = [
            {"buyOrderId": "old", "priceCents": 21},
            {"buyOrderId": "new", "priceCents": 22},
        ]
        replaced = [{"buyOrderId": "new", "priceCents": 22}]
        kwargs = {
            "original_order_id": "old",
            "original_price_cents": 21,
            "requested_price_cents": 22,
        }
        self.assertEqual(
            "original_order_unchanged",
            classify_transition(orders_after=unchanged, **kwargs),
        )
        self.assertEqual(
            "same_id_price_changed",
            classify_transition(orders_after=changed, **kwargs),
        )
        self.assertEqual(
            "second_order_created",
            classify_transition(orders_after=second, **kwargs),
        )
        self.assertEqual(
            "old_order_replaced_with_new_id",
            classify_transition(orders_after=replaced, **kwargs),
        )

    def test_extra_old_id_is_only_added_for_explicit_probe(self) -> None:
        ordinary = build_create_buy_order_data(
            session_id="sensitive",
            market_hash_name="Glove Case",
            price_total=22,
        )
        explicit = build_create_buy_order_data(
            session_id="sensitive",
            market_hash_name="Glove Case",
            price_total=22,
            buy_order_id_value="old",
        )
        self.assertNotIn("buy_orderid", ordinary)
        self.assertEqual("old", explicit["buy_orderid"])
        self.assertEqual(22, explicit["price_total"])

    def test_public_response_keeps_only_decision_evidence(self) -> None:
        public = public_response_payload(
            {
                "success": 29,
                "message": "duplicate order",
                "buy_orderid": "123",
                "wallet_info": {"wallet_balance": 999},
            },
            http_status=200,
            kind="steam_error",
        )
        self.assertEqual("123", public["buyOrderId"])
        self.assertNotIn("wallet_info", public)

    def test_orderbook_parser_supports_flat_and_nested_compact_rows(self) -> None:
        self.assertEqual(
            10001,
            orderbook_min_sell_cents({"data": {"rgCompactSellOrders": [10001, 2]}}),
        )
        self.assertEqual(
            10002,
            orderbook_min_sell_cents(
                {"data": {"rgCompactSellOrders": [[10002, 2], [10003, 5]]}}
            ),
        )


if __name__ == "__main__":
    unittest.main()
