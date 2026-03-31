from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.cli import (
    _build_market_price_gap_rows,
    _list_c5_steam_accounts,
    _resolve_c5_steam_id,
    _summarize_inventory_types,
)
from cs2_assistant.models import MarketState


class FakeC5Client:
    def __init__(self, payload: dict):
        self.payload = payload

    def steam_info(self) -> dict:
        return self.payload


class ResolveC5SteamIdTestCase(unittest.TestCase):
    def test_prefers_explicit_steam_id(self) -> None:
        client = FakeC5Client({"steamId": "from-api"})
        self.assertEqual("manual-id", _resolve_c5_steam_id(client, "manual-id"))

    def test_uses_top_level_steam_id_when_present(self) -> None:
        client = FakeC5Client({"steamId": "top-level-id"})
        self.assertEqual("top-level-id", _resolve_c5_steam_id(client, None))

    def test_falls_back_to_preferred_bound_account(self) -> None:
        client = FakeC5Client(
            {
                "steamList": [
                    {"steamId": "ordinary-id", "autoType": 1},
                    {"steamId": "preferred-id", "autoType": 2},
                ]
            }
        )
        self.assertEqual("preferred-id", _resolve_c5_steam_id(client, None))

    def test_raises_when_no_steam_id_found(self) -> None:
        client = FakeC5Client({"steamList": []})
        with self.assertRaises(RuntimeError):
            _resolve_c5_steam_id(client, None)


class ListC5SteamAccountsTestCase(unittest.TestCase):
    def test_lists_all_accounts_and_prefers_auto_type_2_first(self) -> None:
        client = FakeC5Client(
            {
                "steamList": [
                    {"steamId": "b-id", "autoType": 1, "nickname": "B"},
                    {"steamId": "a-id", "autoType": 2, "nickname": "A"},
                ]
            }
        )
        accounts = _list_c5_steam_accounts(client)
        self.assertEqual(["a-id", "b-id"], [account["steamId"] for account in accounts])

    def test_falls_back_to_top_level_steam_id(self) -> None:
        client = FakeC5Client({"steamId": "top-level-id", "nickname": "Top"})
        accounts = _list_c5_steam_accounts(client)
        self.assertEqual("top-level-id", accounts[0]["steamId"])


class SummarizeInventoryTypesTestCase(unittest.TestCase):
    def test_groups_same_market_hash_name_across_accounts(self) -> None:
        summaries = _summarize_inventory_types(
            [
                {
                    "marketHashName": "Revolution Case",
                    "name": "变革武器箱",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "itemId": "c5-item",
                    "price": 2.10,
                },
                {
                    "marketHashName": "Revolution Case",
                    "name": "变革武器箱",
                    "steamId": "steam-b",
                    "ifTradable": False,
                    "itemId": "c5-item",
                    "price": 2.11,
                },
                {
                    "marketHashName": "Sticker | MOUZ | Budapest 2025",
                    "name": "印花 | MOUZ | 2025年布达佩斯锦标赛",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "price": 0.02,
                },
            ]
        )

        self.assertEqual(2, len(summaries))
        case_summary = next(row for row in summaries if row["market_hash_name"] == "Revolution Case")
        self.assertEqual(2, case_summary["inventory_count"])
        self.assertEqual(1, case_summary["tradable_count"])
        self.assertEqual(["steam-a", "steam-b"], case_summary["steam_ids"])
        self.assertEqual("c5-item", case_summary["c5_item_id"])
        self.assertEqual(2.10, case_summary["reference_price"])


class BuildMarketPriceGapRowsTestCase(unittest.TestCase):
    def test_collects_items_with_c5_price_but_missing_steam_price(self) -> None:
        rows = _build_market_price_gap_rows(
            [
                MarketState(
                    market_hash_name="Rezan The Ready | Sabre",
                    name_cn="准备就绪的列赞 | 军刀",
                    c5_sell_price=136.9,
                    steam_sell_price=None,
                    c5_price_source="inventory_price",
                ),
                MarketState(
                    market_hash_name="Kilowatt Case",
                    name_cn="千瓦武器箱",
                    c5_sell_price=1.62,
                    steam_sell_price=1.66,
                    c5_price_source="c5_batch",
                    steam_price_source="steamdt",
                ),
            ],
            attempted_sources=["steamdt", "csqaq"],
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("Rezan The Ready | Sabre", rows[0]["marketHashName"])
        self.assertEqual(["steamdt", "csqaq"], rows[0]["steamSourcesAttempted"])


if __name__ == "__main__":
    unittest.main()
