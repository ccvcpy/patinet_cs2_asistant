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
                steam_price_source="steam_orderbook",
            )
            for item in items
        ]


class FakeThirdPartyMarketService:
    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        return [
            MarketState(
                market_hash_name=str(item["market_hash_name"]),
                name_cn=str(item.get("name_cn") or item["market_hash_name"]),
                steam_sell_price=0.34,
                steam_price_source="steamdt",
            )
            for item in items
        ]


class StrategyClassificationTestCase(unittest.TestCase):
    def test_scan_report_preserves_every_non_evaluated_item_reason(self) -> None:
        class OutcomeMarketService:
            def refresh_items(self, items: list[dict]) -> list[MarketState]:
                states: list[MarketState] = []
                for item in items:
                    market_hash_name = str(item["market_hash_name"])
                    if market_hash_name == "Missing State Case":
                        continue
                    state = MarketState(
                        market_hash_name=market_hash_name,
                        name_cn=str(item.get("name_cn") or market_hash_name),
                        steam_sell_price=2.0,
                        steam_price_source="steam_orderbook",
                    )
                    if market_hash_name == "Queue Deferred Case":
                        state.steam_sell_price = None
                        state.steam_price_source = None
                        state.raw_json["steam_orderbook_error_type"] = "queue_timeout"
                        state.raw_json["steam_orderbook_error"] = "Steam request timed out in queue"
                    states.append(state)
                return states

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                db_path=Path(temp_dir) / "assistant.db",
                c5_api_key="c5-key",
                steamdt_api_key="steamdt-key",
            )
            config = StrategyConfig(
                dry_run=False,
                min_price=1.0,
                guadao_max_listing_ratio=0.90,
                transfer_min_real_ratio=9999,
            )
            inventory_payload = {
                "source": "live",
                "list": [
                    {
                        "assetId": "asset-ok",
                        "marketHashName": "Evaluated Case",
                        "name": "Evaluated Case",
                        "ifTradable": True,
                        "price": 1.20,
                    },
                    {
                        "assetId": "asset-low",
                        "marketHashName": "Below Min Case",
                        "name": "Below Min Case",
                        "ifTradable": True,
                        "price": 0.50,
                    },
                    {
                        "assetId": "asset-queue",
                        "marketHashName": "Queue Deferred Case",
                        "name": "Queue Deferred Case",
                        "ifTradable": True,
                        "price": 1.20,
                    },
                    {
                        "assetId": "asset-state",
                        "marketHashName": "Missing State Case",
                        "name": "Missing State Case",
                        "ifTradable": True,
                        "price": 1.20,
                    },
                ],
            }
            with patch(
                "cs2_assistant.services.strategy.build_market_service",
                return_value=OutcomeMarketService(),
            ):
                report = scan_strategies(
                    settings,
                    config,
                    inventory_payload=inventory_payload,
                )

        outcomes = {
            str(row["marketHashName"]): row
            for row in report.item_outcomes
        }
        self.assertEqual(4, report.total_pool_types)
        self.assertEqual(4, len(outcomes))
        self.assertEqual("evaluated", outcomes["Evaluated Case"]["status"])
        self.assertEqual("below_min_price", outcomes["Below Min Case"]["status"])
        self.assertEqual("queue_deferred", outcomes["Queue Deferred Case"]["status"])
        self.assertFalse(outcomes["Queue Deferred Case"]["requestSent"])
        self.assertEqual("market_state_missing", outcomes["Missing State Case"]["status"])
        self.assertEqual(0, report.missing_price_count)

    def test_guadao_scope_all_now_normalizes_to_crates_only(self) -> None:
        self.assertEqual("crates_only", normalize_guadao_item_scope("all"))
        self.assertEqual("crates_only", normalize_guadao_item_scope(""))
        self.assertEqual("crates_only", StrategyConfig().guadao_item_scope)

    def test_guadao_scope_legacy_case_only_normalizes_to_crates_only(self) -> None:
        self.assertEqual("crates_only", normalize_guadao_item_scope("case_only"))
        config = StrategyConfig.from_dict(
            {"guadaoBalance": {"guadaoItemScope": "case_only"}}
        )
        self.assertEqual("crates_only", config.guadao_item_scope)
        self.assertEqual(
            "crates_only",
            config.to_dict()["guadaoBalance"]["guadaoItemScope"],
        )

    def test_guadao_scope_crates_only_blocks_non_crates(self) -> None:
        config = StrategyConfig(
            guadao_max_listing_ratio=0.67,
            transfer_min_real_ratio=9999,
            guadao_item_scope="crates_only",
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
                    pool_market_hash_names=["Kilowatt Case", "Austin 2025 Legends Autograph Capsule"],
                    inventory_payload=inventory_payload,
                )

        self.assertEqual(1, len(report.all_evaluated))
        self.assertEqual(1, report.total_pool_types)
        self.assertEqual("Kilowatt Case", report.all_evaluated[0].market_hash_name)

    def test_scan_strategies_accepts_catalog_case_names(self) -> None:
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
                guadao_item_scope="crates_only",
            )
            inventory_payload = {
                "source": "live",
                "accounts": [{"steamId": "steam-1", "nickname": "main"}],
                "list": [
                    {
                        "assetId": "asset-1",
                        "marketHashName": "Catalog Classified Container",
                        "name": "Catalog Classified Container",
                        "steamId": "steam-1",
                        "ifTradable": True,
                        "price": 1.0,
                    }
                ],
            }

            with patch(
                "cs2_assistant.services.strategy.build_market_service",
                return_value=FakeMarketService(),
            ):
                report = scan_strategies(
                    settings,
                    config,
                    pool_market_hash_names=["Catalog Classified Container"],
                    inventory_payload=inventory_payload,
                    weapon_case_market_hash_names={"Catalog Classified Container"},
                )

        self.assertEqual(1, len(report.guadao_candidates))
        self.assertEqual("Catalog Classified Container", report.guadao_candidates[0].market_hash_name)

    def test_scan_strategies_requires_orderbook_steam_price_for_real_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                db_path=Path(temp_dir) / "assistant.db",
                c5_api_key="c5-key",
                steamdt_api_key="steamdt-key",
            )
            config = StrategyConfig(
                dry_run=False,
                min_price=1.0,
                guadao_max_listing_ratio=0.90,
                transfer_min_real_ratio=9999,
            )
            inventory_payload = {
                "source": "live",
                "list": [
                    {
                        "assetId": "asset-1",
                        "marketHashName": "Kilowatt Case",
                        "name": "Kilowatt Case",
                        "ifTradable": True,
                        "price": 1.0,
                    }
                ],
            }

            with patch(
                "cs2_assistant.services.strategy.build_market_service",
                return_value=FakeThirdPartyMarketService(),
            ):
                report = scan_strategies(
                    settings,
                    config,
                    pool_market_hash_names=["Kilowatt Case"],
                    inventory_payload=inventory_payload,
                    weapon_case_market_hash_names={"Kilowatt Case"},
                )

        self.assertEqual([], report.all_evaluated)
        self.assertEqual(0, report.missing_price_count)
        self.assertEqual("steam_price_not_orderbook", report.item_outcomes[0]["status"])

    def test_scan_strategies_allows_third_party_steam_price_for_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                db_path=Path(temp_dir) / "assistant.db",
                c5_api_key="c5-key",
                steamdt_api_key="steamdt-key",
            )
            config = StrategyConfig(
                dry_run=True,
                min_price=1.0,
                guadao_max_listing_ratio=4.00,
                transfer_min_real_ratio=9999,
            )
            inventory_payload = {
                "source": "live",
                "list": [
                    {
                        "assetId": "asset-1",
                        "marketHashName": "Kilowatt Case",
                        "name": "Kilowatt Case",
                        "ifTradable": True,
                        "price": 1.0,
                    }
                ],
            }

            with patch(
                "cs2_assistant.services.strategy.build_market_service",
                return_value=FakeThirdPartyMarketService(),
            ):
                report = scan_strategies(
                    settings,
                    config,
                    pool_market_hash_names=["Kilowatt Case"],
                    inventory_payload=inventory_payload,
                    weapon_case_market_hash_names={"Kilowatt Case"},
                )

        self.assertEqual(1, len(report.all_evaluated))
        self.assertEqual("steamdt", report.all_evaluated[0].steam_price_source)


if __name__ == "__main__":
    unittest.main()
