from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.models import MarketState, STRATEGY_GUADAO, StrategyConfig, normalize_guadao_item_scope
from cs2_assistant.services.strategy import classify_strategies, scan_strategies


class FakeMarketService:
    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        return [
            MarketState(
                market_hash_name=str(item["market_hash_name"]),
                name_cn=str(item.get("name_cn") or item["market_hash_name"]),
                steam_sell_price=2.0,
                steam_price_source="test",
            )
            for item in items
        ]


class StrategyClassificationTestCase(unittest.TestCase):
    def test_guadao_scope_all_now_normalizes_to_case_only(self) -> None:
        self.assertEqual("case_only", normalize_guadao_item_scope("all"))
        self.assertEqual("case_only", normalize_guadao_item_scope(""))
        self.assertEqual("case_only", StrategyConfig().guadao_item_scope)

    def test_guadao_scope_case_only_blocks_non_cases(self) -> None:
        config = StrategyConfig(
            guadao_max_listing_ratio=0.67,
            transfer_min_real_ratio=9999,
            guadao_item_scope="case_only",
        )

        case_strategies = classify_strategies(0.60, 0.0, config, is_weapon_case=True)
        non_case_strategies = classify_strategies(0.60, 0.0, config, is_weapon_case=False)

        self.assertIn(STRATEGY_GUADAO, case_strategies)
        self.assertNotIn(STRATEGY_GUADAO, non_case_strategies)

    def test_guadao_scope_non_case_only_blocks_cases(self) -> None:
        config = StrategyConfig(
            guadao_max_listing_ratio=0.67,
            transfer_min_real_ratio=9999,
            guadao_item_scope="non_case_only",
        )

        case_strategies = classify_strategies(0.60, 0.0, config, is_weapon_case=True)
        non_case_strategies = classify_strategies(0.60, 0.0, config, is_weapon_case=False)

        self.assertNotIn(STRATEGY_GUADAO, case_strategies)
        self.assertIn(STRATEGY_GUADAO, non_case_strategies)

    def test_scan_strategies_can_reuse_existing_inventory_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                db_path=Path(temp_dir) / "assistant.db",
                c5_api_key="c5-key",
                steamdt_api_key="steamdt-key",
            )
            config = StrategyConfig(
                min_price=1.0,
                guadao_max_listing_ratio=0.90,
                transfer_min_real_ratio=9999,
            )
            inventory_payload = {
                "source": "live",
                "accounts": [{"steamId": "steam-1", "nickname": "main"}],
                "list": [
                    {
                        "assetId": "asset-1",
                        "marketHashName": "Kilowatt Case",
                        "name": "Kilowatt Case",
                        "steamId": "steam-1",
                        "ifTradable": True,
                        "price": 1.0,
                    }
                ],
            }

            with patch(
                "cs2_assistant.services.strategy.fetch_all_c5_inventories",
                side_effect=AssertionError("should not refetch inventory"),
            ), patch(
                "cs2_assistant.services.strategy.build_market_service",
                return_value=FakeMarketService(),
            ):
                report = scan_strategies(
                    settings,
                    config,
                    pool_market_hash_names=["Kilowatt Case"],
                    inventory_payload=inventory_payload,
                )

        self.assertEqual(1, len(report.all_evaluated))
        self.assertEqual("Kilowatt Case", report.all_evaluated[0].market_hash_name)


if __name__ == "__main__":
    unittest.main()
