from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cs2_assistant.accounts import Account
from cs2_assistant.diagnostics.steam_matching_experiment import (
    AccountRole,
    Candidate,
    JsonlWriter,
    PricePlan,
    TrialSpec,
    TrialState,
    build_price_plan,
    classify_trial,
    compact_orderbook_levels,
    orderbook_currency_context_matches,
    parse_orderbook,
    seller_net_for_exact_total,
    stable_unique_a_bid,
    steam_fee_breakdown,
)


class SteamMatchingExperimentTest(unittest.TestCase):
    def test_low_cent_fee_mapping_is_exact(self) -> None:
        self.assertEqual(
            (7, 7, 21),
            steam_fee_breakdown(7, fee_minimum_cents=7),
        )
        self.assertEqual(
            (7, 14),
            seller_net_for_exact_total(
                21,
                fee_minimum_cents=7,
                seller_minimum_cents=7,
            ),
        )
        self.assertEqual(
            (9, 14),
            seller_net_for_exact_total(
                23,
                fee_minimum_cents=7,
                seller_minimum_cents=7,
            ),
        )

    def test_build_price_plan_uses_minimum_sell_and_one_cent_bid_steps(self) -> None:
        plan = build_price_plan(
            {"minSellCents": 24, "maxBuyCents": None},
            wallet_market_minimum_cents=7,
            wallet_fee_minimum_cents=7,
        )
        self.assertEqual(21, plan.buyer_total_cents)
        self.assertEqual(22, plan.a_bid_cents)
        self.assertEqual(23, plan.c_bid_cents)
        self.assertEqual(7, plan.seller_net_cents)

    def test_build_price_plan_steps_above_existing_highest_bid(self) -> None:
        plan = build_price_plan(
            {"minSellCents": 30, "maxBuyCents": 22},
            wallet_market_minimum_cents=7,
            wallet_fee_minimum_cents=7,
        )
        self.assertEqual(21, plan.buyer_total_cents)
        self.assertEqual(23, plan.a_bid_cents)
        self.assertEqual(24, plan.c_bid_cents)

    def test_build_price_plan_rejects_floor_without_test_spread(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not leave room"):
            build_price_plan(
                {"minSellCents": 23, "maxBuyCents": None},
                wallet_market_minimum_cents=7,
                wallet_fee_minimum_cents=7,
            )

    def test_parse_flat_compact_orderbook(self) -> None:
        parsed = parse_orderbook(
            {
                "data": {
                    "rgCompactSellOrders": [21, 5, 22, 10],
                    "rgCompactBuyOrders": [19, 1, 18, 4],
                    "cSellOrders": 15,
                    "cBuyOrders": 5,
                }
            }
        )
        self.assertEqual(21, parsed["minSellCents"])
        self.assertEqual(5, parsed["minSellCount"])
        self.assertEqual(19, parsed["maxBuyCents"])
        self.assertEqual(1, parsed["maxBuyCount"])

    def test_compact_orderbook_levels_are_preserved_for_evidence(self) -> None:
        self.assertEqual(
            {
                "compactSellOrders": [21, 5, 22, 10],
                "compactBuyOrders": [19, 1, 18, 4],
            },
            compact_orderbook_levels(
                {
                    "data": {
                        "rgCompactSellOrders": [21, 5, 22, 10],
                        "rgCompactBuyOrders": [19, 1, 18, 4],
                    }
                }
            ),
        )

    def test_unique_bid_requires_three_consecutive_single_orders(self) -> None:
        samples = [
            {"maxBuyCents": 19, "maxBuyCount": 1},
            {"maxBuyCents": 19, "maxBuyCount": 1},
            {"maxBuyCents": 19, "maxBuyCount": 1},
        ]
        self.assertTrue(stable_unique_a_bid(samples, 19))
        samples[-1]["maxBuyCount"] = 2
        self.assertFalse(stable_unique_a_bid(samples, 19))

    def test_observer_currency_context_rejects_usd_cny_mismatch(self) -> None:
        cny = {"minSellCents": 58, "maxBuyCents": 47}
        usd = {"minSellCents": 11, "maxBuyCents": 8}
        moved_one_cent = {"minSellCents": 59, "maxBuyCents": 46}
        self.assertFalse(orderbook_currency_context_matches(cny, usd))
        self.assertTrue(orderbook_currency_context_matches(cny, moved_one_cent))

    def test_classification_distinguishes_existing_and_later_buyer(self) -> None:
        accounts = [Account(id=str(i), name=name) for i, name in enumerate("SACDE")]
        roles = AccountRole(*accounts)
        state = TrialState(
            trial=TrialSpec("late", "createbuyorder", Candidate("Skin", "1", "S")),
            roles=roles,
            price=PricePlan(23, None, 21, 22, 7, 14, 21, 7, 7),
        )
        state.terminal_buyer = roles.buyer_a.name
        self.assertEqual(
            "existing_high_bid_matched_before_public_listing",
            classify_trial(state),
        )
        state.terminal_buyer = roles.buyer_c.name
        self.assertEqual("later_higher_buy_order_overtook", classify_trial(state))

    def test_jsonl_writer_refuses_no_data_but_writes_safe_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            writer = JsonlWriter(path)
            writer.start()
            writer.emit({"event": "safe", "assetId": "1"})
            writer.close()
            text = path.read_text(encoding="utf-8")
            self.assertIn('"event":"safe"', text)
            self.assertNotIn("cookie", text.lower())

    def test_experiment_event_refuses_sensitive_field(self) -> None:
        from cs2_assistant.diagnostics.steam_matching_experiment import SteamMatchingExperiment

        with tempfile.TemporaryDirectory() as tmp:
            experiment = SteamMatchingExperiment(output_dir=Path(tmp))
            with self.assertRaisesRegex(ValueError, "sensitive"):
                experiment.event("trial", "unsafe", cookies="secret")

    def test_steam_429_aborts_all_trials(self) -> None:
        from cs2_assistant.clients.steam_market import SteamMarketError
        from cs2_assistant.diagnostics.steam_matching_experiment import SteamMatchingExperiment

        accounts = [Account(id=str(i), name=name) for i, name in enumerate("SACDE")]
        state = TrialState(
            trial=TrialSpec("control", "none", Candidate("Skin", "1", "S")),
            roles=AccountRole(*accounts),
            price=PricePlan(23, None, 21, 22, 7, 14, 21, 7, 7),
        )
        with tempfile.TemporaryDirectory() as tmp:
            experiment = SteamMatchingExperiment(output_dir=Path(tmp))
            experiment.writer.start()
            try:
                with self.assertRaises(SteamMarketError):
                    experiment._safe_call(
                        state,
                        accounts[1],
                        "orderbook",
                        lambda: (_ for _ in ()).throw(
                            SteamMarketError("rate limited", status_code=429)
                        ),
                    )
                self.assertTrue(experiment.abort_all.is_set())
                self.assertTrue(state.errors)
            finally:
                experiment.writer.close()

    def test_terminal_check_does_not_query_c_history_before_c_action(self) -> None:
        from cs2_assistant.diagnostics.steam_matching_experiment import SteamMatchingExperiment

        accounts = [Account(id=str(i), name=name) for i, name in enumerate("SACDE")]
        state = TrialState(
            trial=TrialSpec("late", "createbuyorder", Candidate("Skin", "1", "S")),
            roles=AccountRole(*accounts),
            price=PricePlan(30, 22, 23, 24, 7, 14, 21, 7, 7),
        )
        state.b_sale_receipt = {"listingId": "10", "purchaseId": "11"}
        operations: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            experiment = SteamMatchingExperiment(output_dir=Path(tmp))
            experiment._safe_call = (  # type: ignore[method-assign]
                lambda _state, _account, operation, _fn: operations.append(operation) or None
            )
            self.assertFalse(experiment._find_terminal(state))
        self.assertEqual(["history_a"], operations)


if __name__ == "__main__":
    unittest.main()
