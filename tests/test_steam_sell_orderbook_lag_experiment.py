from __future__ import annotations

import unittest

from cs2_assistant.diagnostics.steam_sell_orderbook_lag_experiment import (
    choose_high_unique_price,
    compact_order_levels,
)


class SteamSellOrderbookLagExperimentTest(unittest.TestCase):
    def test_compact_sell_levels_reads_every_price_level(self) -> None:
        levels = compact_order_levels(
            {"data": {"rgCompactSellOrders": [42, 2, 117, 1, 230, 3]}},
            "rgCompactSellOrders",
        )
        self.assertEqual({42: 2, 117: 1, 230: 3}, levels)

    def test_choose_high_price_is_not_floor_and_is_unique(self) -> None:
        plan = choose_high_unique_price(
            floor_cents=42,
            existing_levels={42: 2, 200: 1},
            fee_minimum_cents=7,
            seller_minimum_cents=7,
        )
        self.assertGreaterEqual(plan.buyer_total_cents, 200)
        self.assertNotEqual(42, plan.buyer_total_cents)
        self.assertNotIn(plan.buyer_total_cents, {42, 200})
        self.assertEqual(
            plan.buyer_total_cents,
            plan.seller_net_cents + plan.fee_cents,
        )


if __name__ == "__main__":
    unittest.main()
