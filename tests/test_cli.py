from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.cli import (
    _build_pool_inventory_report,
    build_parser,
    _build_guadao_discount_report,
    _build_market_price_gap_rows,
    _list_c5_steam_accounts,
    _parse_report_boundary,
    _resolve_c5_steam_id,
    _summarize_inventory_types,
    cmd_pool_case_monitor_collect,
    cmd_pool_case_monitor_report,
    cmd_pool_case_monitor_run,
)
from cs2_assistant.db import Database
from cs2_assistant.models import (
    MarketState,
    OP_SELL_STEAM,
    POOL_STATUS_LISTING_PENDING,
    STRATEGY_GUADAO,
    StrategyConfig,
)


class FakeC5Client:
    def __init__(self, payload: dict):
        self.payload = payload

    def steam_info(self) -> dict:
        return self.payload


class BalanceCommandTestCase(unittest.TestCase):
    def test_top_level_balance_command_is_removed(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["balance"])

    def test_account_balance_summarizes_all_accounts_without_executor(self) -> None:
        from cs2_assistant import cli

        class FakeSettings:
            steam_market_base_url = "https://steam.test"

        class FakeAccount:
            def __init__(self, name: str, steam_id64: str | None, cookies: str | None) -> None:
                self.id = name
                self.name = name
                self.steam_id64 = steam_id64
                self.cookies = cookies
                self.identity_secret = None
                self.device_id = None

        accounts = [
            FakeAccount("a", "76561198000000001", "sessionid=s1; steamLoginSecure=76561198000000001%7C%7Ct"),
            FakeAccount("b", "76561198000000002", "sessionid=s2; steamLoginSecure=76561198000000002%7C%7Ct"),
            FakeAccount("missing", None, None),
        ]

        class FakeStore:
            def list_accounts(self) -> list[FakeAccount]:
                return accounts

        wallet_by_steam_id = {
            "76561198000000001": {"balance": 10.0, "delayed_balance": 1.25, "currency": "CNY"},
            "76561198000000002": {"balance": 20.5, "delayed_balance": 0.0, "currency": "CNY"},
        }

        class FakeSteam:
            def __init__(self, **kwargs: object) -> None:
                self.steam_id64 = str(kwargs["steam_id64"])

            def wallet_balance(self) -> dict:
                return wallet_by_steam_id[self.steam_id64]

        def explode_executor(*args: object, **kwargs: object) -> object:
            raise AssertionError("account balance must not instantiate ExecutionEngine")

        args = build_parser().parse_args(["account", "balance"])
        with mock.patch.object(cli, "load_settings", return_value=FakeSettings()):
            with mock.patch.object(cli, "_account_store", return_value=FakeStore()):
                with mock.patch.object(cli, "SteamMarketClient", FakeSteam):
                    with mock.patch.object(cli, "ExecutionEngine", side_effect=explode_executor):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            code = args.handler(args)

        self.assertEqual(0, code)
        rendered = output.getvalue()
        self.assertIn("g_rgWalletInfo", rendered)
        self.assertIn("a", rendered)
        self.assertIn("10.00", rendered)
        self.assertIn("1.25", rendered)
        self.assertIn("b", rendered)
        self.assertIn("20.50", rendered)
        self.assertIn("missing", rendered)
        self.assertIn("skipped", rendered)
        self.assertIn("TOTAL", rendered)
        self.assertIn("30.50", rendered)
        self.assertIn("31.75", rendered)


class SteamConfirmCommandTestCase(unittest.TestCase):
    def test_steam_confirm_uses_program_asset_targets(self) -> None:
        from cs2_assistant import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "assistant.db"
            db = Database(db_path)
            db.initialize()
            op_id = db.add_pool_operation(
                market_hash_name="Kilowatt Case",
                strategy=STRATEGY_GUADAO,
                operation_type=OP_SELL_STEAM,
                expected_price=2.0,
                asset_id="asset-program",
                note=json.dumps(
                    {
                        "listingId": "listing-program",
                        "needsConfirmation": True,
                        "confirmationStatus": "not_found",
                        "steamId64": "76561198000000001",
                    }
                ),
            )
            db.update_pool_operation(op_id, status=POOL_STATUS_LISTING_PENDING)
            skipped_id = db.add_pool_operation(
                market_hash_name="AK-47 | Redline (Field-Tested)",
                strategy=STRATEGY_GUADAO,
                operation_type=OP_SELL_STEAM,
                expected_price=20.0,
                asset_id="asset-other-account",
                note=json.dumps(
                    {
                        "listingId": "listing-other-account",
                        "needsConfirmation": True,
                        "confirmationStatus": "not_found",
                        "steamId64": "76561198000000002",
                    }
                ),
            )
            db.update_pool_operation(skipped_id, status=POOL_STATUS_LISTING_PENDING)
            db.close()

            class FakeSettings:
                steam_market_base_url = "https://steam.test"
                steam_cookies = None
                steam_identity_secret = None
                steam_device_id = None
                db_path = Path(tmpdir) / "assistant.db"

            class FakeSteam:
                steam_id64 = "76561198000000001"

                def __init__(self) -> None:
                    self.calls: list[dict[str, object]] = []

                def confirm_listing_assets(self, *, asset_ids: object, listing_ids: object | None = None) -> int:
                    self.calls.append({"asset_ids": asset_ids, "listing_ids": listing_ids})
                    return 1

                def confirm_all(self) -> int:
                    raise AssertionError("steam confirm must not call confirm_all")

            fake_steam = FakeSteam()
            args = build_parser().parse_args(["steam", "confirm"])
            with mock.patch.object(cli, "load_settings", return_value=FakeSettings()):
                with mock.patch.object(cli, "_build_steam_client", return_value=fake_steam):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = args.handler(args)

            self.assertEqual(0, code)
            self.assertEqual(
                [{"asset_ids": ["asset-program"], "listing_ids": None}],
                fake_steam.calls,
            )
            self.assertIn("Confirmed 1 listings", output.getvalue())


class ResolveC5SteamIdTestCase(unittest.TestCase):
    def test_config_proxy_command_is_removed(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["config", "proxy", "none"])

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
                    "name": "Revolution Case",
                    "steamId": "steam-a",
                    "ifTradable": True,
                    "itemId": "c5-item",
                    "price": 2.10,
                },
                {
                    "marketHashName": "Revolution Case",
                    "name": "Revolution Case",
                    "steamId": "steam-b",
                    "ifTradable": False,
                    "itemId": "c5-item",
                    "price": 2.11,
                },
                {
                    "marketHashName": "Sticker | MOUZ | Budapest 2025",
                    "name": "Sticker | MOUZ | Budapest 2025",
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


class FakeCatalogDb:
    def get_item(self, market_hash_name: str):
        return None


class PoolInventoryReportTestCase(unittest.TestCase):
    def test_reports_tradable_and_next_week_case_cooldowns(self) -> None:
        report = _build_pool_inventory_report(
            {
                "source": "live",
                "accountCount": 2,
                "accounts": [
                    {"steamId": "steam-a", "nickname": "A"},
                    {"steamId": "steam-b", "nickname": "B"},
                ],
                "list": [
                    {
                        "marketHashName": "Kilowatt Case",
                        "name": "Kilowatt Case",
                        "steamId": "steam-a",
                        "ifTradable": True,
                    },
                    {
                        "marketHashName": "Kilowatt Case",
                        "name": "Kilowatt Case",
                        "steamId": "steam-b",
                        "ifTradable": False,
                        "tradableTime": "2026-06-05T04:00:00+00:00",
                    },
                    {
                        "marketHashName": "Sticker Capsule",
                        "name": "Sticker Capsule",
                        "steamId": "steam-b",
                        "ifTradable": False,
                        "tradableTime": None,
                    },
                    {
                        "marketHashName": "AK-47 | Redline (Field-Tested)",
                        "name": "AK-47 | Redline (Field-Tested)",
                        "steamId": "steam-a",
                        "ifTradable": True,
                    },
                ],
            },
            db=FakeCatalogDb(),
            scope="case_only",
            days=7,
            now=datetime(2026, 6, 4, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(2, report["totalTypes"])
        self.assertEqual(1, report["tradableCount"])
        self.assertEqual(2, report["cooldownCount"])
        self.assertEqual(1, report["futureCooldownCount"])
        self.assertEqual(1, report["unknownCooldownCount"])
        kilowatt = next(row for row in report["rows"] if row["marketHashName"] == "Kilowatt Case")
        self.assertEqual(2, kilowatt["totalCount"])
        self.assertEqual(1, kilowatt["tradableCount"])
        self.assertIn("A 1", kilowatt["tradableAccountSummary"])
        june_5 = next(day for day in report["daily"] if day["date"] == "2026-06-05")
        self.assertEqual(1, june_5["count"])
        self.assertEqual("Kilowatt Case", june_5["items"][0]["marketHashName"])


class BuildMarketPriceGapRowsTestCase(unittest.TestCase):
    def test_collects_items_with_c5_price_but_missing_steam_price(self) -> None:
        rows = _build_market_price_gap_rows(
            [
                MarketState(
                    market_hash_name="Rezan The Ready | Sabre",
                    name_cn="Rezan The Ready | Sabre",
                    c5_sell_price=136.9,
                    steam_sell_price=None,
                    c5_price_source="inventory_price",
                ),
                MarketState(
                    market_hash_name="Kilowatt Case",
                    name_cn="Kilowatt Case",
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


class ParserTestCase(unittest.TestCase):
    def test_t_profit_scan_supports_bottom(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["t-profit", "scan", "--bottom", "10", "--inventory-filter", "has_tradable"])
        self.assertEqual(10, args.bottom)
        self.assertEqual("t-profit", args.command)
        self.assertEqual("has_tradable", args.inventory_filter)

    def test_notify_t_profit_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["notify", "t-profit", "--once"])
        self.assertEqual("notify", args.command)
        self.assertEqual("t-profit", args.notify_command)
        self.assertTrue(args.once)

    def test_pool_item_command_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["pool", "item"])
        self.assertEqual("pool", args.command)
        self.assertEqual("item", args.pool_command)
        self.assertEqual(7, args.days)
        self.assertIsNone(args.scope)

    def test_pool_without_subcommand_fails(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["pool"])

    def test_old_pool_subcommands_are_removed_from_parser(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["pool", "scan"])

    def test_pool_guadao_report_parses_hour_precision(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "pool",
                "guadao-report",
                "--from",
                "2026-05-10T08",
                "--to",
                "2026-05-10T17",
            ]
        )
        self.assertEqual("pool", args.command)
        self.assertEqual("guadao-report", args.pool_command)
        self.assertEqual("2026-05-10T08", args.date_from)
        self.assertEqual("2026-05-10T17", args.date_to)

    def test_pool_case_monitor_collect_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["pool", "case-monitor", "collect", "--item", "Kilowatt Case", "--limit", "2"])
        self.assertEqual("pool", args.command)
        self.assertEqual("case-monitor", args.pool_command)
        self.assertEqual("collect", args.case_monitor_command)
        self.assertEqual(["Kilowatt Case"], args.market_hash_names)
        self.assertEqual(2, args.limit)
        self.assertIs(args.handler, cmd_pool_case_monitor_collect)

    def test_pool_case_monitor_run_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["pool", "case-monitor", "run", "--once"])
        self.assertEqual("pool", args.command)
        self.assertEqual("case-monitor", args.pool_command)
        self.assertEqual("run", args.case_monitor_command)
        self.assertTrue(args.once)
        self.assertEqual(5.0, args.interval_minutes)
        self.assertIs(args.handler, cmd_pool_case_monitor_run)

    def test_pool_case_monitor_report_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["pool", "case-monitor", "report", "--hours", "168", "--top", "5"])
        self.assertEqual("pool", args.command)
        self.assertEqual("case-monitor", args.pool_command)
        self.assertEqual("report", args.case_monitor_command)
        self.assertEqual(168.0, args.hours)
        self.assertEqual(5, args.top)
        self.assertEqual("all", args.recommendation_type)
        self.assertIs(args.handler, cmd_pool_case_monitor_report)

    def test_parse_report_boundary_expands_hour_precision(self) -> None:
        start = _parse_report_boundary("2026-05-10T08", is_end=False)
        end = _parse_report_boundary("2026-05-10T17", is_end=True)
        self.assertEqual("2026-05-10 08:00:00", start.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual("2026-05-10 17:59:59", end.strftime("%Y-%m-%d %H:%M:%S"))

    def test_parse_report_boundary_expands_minute_precision(self) -> None:
        start = _parse_report_boundary("2026-05-10T08:15", is_end=False)
        end = _parse_report_boundary("2026-05-10T17:23", is_end=True)
        self.assertEqual("2026-05-10 08:15:00", start.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual("2026-05-10 17:23:59", end.strftime("%Y-%m-%d %H:%M:%S"))

    def test_pool_sync_command_is_removed(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["pool", "sync"])

    def test_catalog_sync_csgo_api_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["catalog", "sync-csgo-api", "--category", "crates"])
        self.assertEqual("catalog", args.command)
        self.assertEqual("sync-csgo-api", args.catalog_command)
        self.assertEqual(["crates"], args.categories)

    def test_steam_test_list_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["steam", "test-list", "--asset-id", "asset-1", "--price", "999"])
        self.assertEqual("steam", args.command)
        self.assertEqual("test-list", args.steam_command)
        self.assertEqual("asset-1", args.asset_id)
        self.assertEqual(999.0, args.price)

    def test_executor_start_parses_new_max_transfer_buy_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["executor", "start", "--max-transfer-buy", "7"])
        self.assertEqual("executor", args.command)
        self.assertEqual("start", args.executor_command)
        self.assertEqual(7, args.max_transfer_buy)

    def test_executor_start_parses_legacy_max_buy_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["executor", "start", "--max-buy", "4"])
        self.assertEqual("executor", args.command)
        self.assertEqual("start", args.executor_command)
        self.assertEqual(4, args.max_transfer_buy)


class StrategyConfigCompatTestCase(unittest.TestCase):
    def test_from_dict_prefers_new_max_transfer_buy_key(self) -> None:
        config = StrategyConfig.from_dict(
            {
                "maxTransferBuyPerCycle": 7,
                "maxBuyPerCycle": 3,
            }
        )
        self.assertEqual(7, config.max_transfer_buy_per_cycle)

    def test_from_dict_accepts_legacy_max_buy_key(self) -> None:
        config = StrategyConfig.from_dict({"maxBuyPerCycle": 5})
        self.assertEqual(5, config.max_transfer_buy_per_cycle)

    def test_to_dict_writes_new_max_transfer_buy_key(self) -> None:
        config = StrategyConfig(max_transfer_buy_per_cycle=6)
        payload = config.to_dict()
        self.assertEqual(6, payload["maxTransferBuyPerCycle"])
        self.assertNotIn("maxBuyPerCycle", payload)


class GuadaoDiscountReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "assistant.db"
        self.db = Database(self.db_path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def _add_closed_cycle(
        self,
        market_hash_name: str,
        *,
        steam_price: float,
        cash: float,
        rebuy_note_extra: dict[str, object] | None = None,
    ) -> int:
        sell_id = self.db.add_pool_operation(
            market_hash_name=market_hash_name,
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=steam_price,
            asset_id=f"asset-{market_hash_name}",
            note=json.dumps(
                {
                    "listingId": f"listing-{market_hash_name}",
                    "rebuyPrice": cash,
                    "steamListPrice": steam_price,
                    "steamSoldAt": "2026-01-01T00:00:00+00:00",
                    "strategy": "guadao",
                }
            ),
        )
        self.db.update_pool_operation(sell_id, status="sold")
        rebuy_note = {
            "sourceListing": f"listing-{market_hash_name}",
            "sourceSellOperationId": sell_id,
            "steamListPrice": steam_price,
        }
        if rebuy_note_extra:
            rebuy_note.update(rebuy_note_extra)
        rebuy_id = self.db.add_pool_operation(
            market_hash_name=market_hash_name,
            strategy="guadao",
            operation_type="rebuy_on_c5",
            expected_price=cash,
            note=json.dumps(rebuy_note),
        )
        self.db.update_pool_operation(rebuy_id, status="completed", actual_price=cash)
        return rebuy_id

    def _set_completed_at(self, op_id: int, completed_at: str) -> None:
        self.db.conn.execute("UPDATE pool_operations SET completed_at = ? WHERE id = ?", (completed_at, op_id))
        self.db.conn.commit()

    def test_build_guadao_discount_report_summarizes_multiple_items(self) -> None:
        self._add_closed_cycle("Kilowatt Case", steam_price=100.0, cash=60.0)
        self._add_closed_cycle("Revolution Case", steam_price=200.0, cash=130.0)

        report = _build_guadao_discount_report(
            self.db,
            start_utc="2000-01-01T00:00:00+00:00",
            end_utc="2100-01-01T00:00:00+00:00",
            steam_net_factor=0.869,
        )

        self.assertEqual(2, report["summary"]["count"])
        self.assertEqual(300.0, report["summary"]["steamGross"])
        self.assertEqual(260.7, report["summary"]["steamNet"])
        self.assertEqual(190.0, report["summary"]["cash"])
        self.assertEqual(2, report["steamSoldInRange"]["summary"]["count"])
        self.assertEqual(260.7, report["steamSoldInRange"]["summary"]["steamNet"])
        self.assertAlmostEqual(190.0 / 260.7, report["summary"]["totalDiscountRatio"])
        self.assertAlmostEqual(190.0 / 300.0, report["summary"]["faceDiscountRatio"])
        self.assertEqual(["Kilowatt Case", "Revolution Case"], sorted(row["marketHashName"] for row in report["items"]))

    def test_guadao_discount_report_separates_rebuy_time_from_steam_wallet_time(self) -> None:
        old_sell_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=100.0,
            asset_id="asset-old",
            note=json.dumps({"steamListPrice": 100.0, "steamSoldAt": "2026-05-31T17:00:00+00:00"}),
        )
        self.db.update_pool_operation(old_sell_id, status="sold", actual_price=100.0)
        self._set_completed_at(old_sell_id, "2026-05-31T17:00:00+00:00")
        old_rebuy_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="rebuy_on_c5",
            expected_price=60.0,
            note=json.dumps({"sourceSellOperationId": old_sell_id, "steamListPrice": 100.0}),
        )
        self.db.update_pool_operation(old_rebuy_id, status="completed", actual_price=60.0)
        self._set_completed_at(old_rebuy_id, "2026-05-31T19:00:00+00:00")

        current_sell_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=200.0,
            asset_id="asset-current",
            note=json.dumps({"steamListPrice": 200.0, "steamSoldAt": "2026-05-31T18:30:00+00:00"}),
        )
        self.db.update_pool_operation(current_sell_id, status="sold", actual_price=200.0)
        self._set_completed_at(current_sell_id, "2026-05-31T18:30:00+00:00")
        late_rebuy_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy="guadao",
            operation_type="rebuy_on_c5",
            expected_price=130.0,
            note=json.dumps({"sourceSellOperationId": current_sell_id, "steamListPrice": 200.0}),
        )
        self.db.update_pool_operation(late_rebuy_id, status="completed", actual_price=130.0)
        self._set_completed_at(late_rebuy_id, "2026-06-01T16:30:00+00:00")

        report = _build_guadao_discount_report(
            self.db,
            start_utc="2026-05-31T18:00:00+00:00",
            end_utc="2026-06-01T15:59:59+00:00",
            steam_net_factor=0.869,
        )

        self.assertEqual(1, report["summary"]["count"])
        self.assertEqual(86.9, report["summary"]["steamNet"])
        self.assertEqual(1, report["closedFromSellOutsideRange"]["count"])
        self.assertEqual(86.9, report["closedFromSellOutsideRange"]["steamNet"])
        self.assertEqual(1, report["steamSoldInRange"]["summary"]["count"])
        self.assertEqual(173.8, report["steamSoldInRange"]["summary"]["steamNet"])
        self.assertEqual(0, report["steamSoldReconciliation"]["closed"]["count"])
        self.assertEqual(1, report["steamSoldReconciliation"]["unclosed"]["count"])
        self.assertEqual(173.8, report["steamSoldReconciliation"]["unclosed"]["steamNet"])
        self.assertEqual(
            report["steamSoldInRange"]["summary"]["steamNet"],
            report["steamSoldReconciliation"]["closed"]["steamNet"]
            + report["steamSoldReconciliation"]["unclosed"]["steamNet"]
            + report["steamSoldReconciliation"]["ignored"]["steamNet"],
        )

    def test_guadao_discount_report_uses_steam_sold_at_for_wallet_time(self) -> None:
        early_sell_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=100.0,
            asset_id="asset-early",
            note=json.dumps(
                {
                    "steamListPrice": 100.0,
                    "steamSellerNetPrice": 86.9,
                    "steamSoldAt": "2026-06-22T04:50:00+00:00",
                }
            ),
        )
        self.db.update_pool_operation(early_sell_id, status="sold", actual_price=100.0)
        self._set_completed_at(early_sell_id, "2026-06-22T06:30:00+00:00")

        current_sell_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=200.0,
            asset_id="asset-current",
            note=json.dumps(
                {
                    "steamListPrice": 200.0,
                    "steamSellerNetPrice": 173.8,
                    "steamSoldAt": "2026-06-22T06:10:00+00:00",
                }
            ),
        )
        self.db.update_pool_operation(current_sell_id, status="sold", actual_price=200.0)
        self._set_completed_at(current_sell_id, "2026-06-22T08:30:00+00:00")

        report = _build_guadao_discount_report(
            self.db,
            start_utc="2026-06-22T05:06:00+00:00",
            end_utc="2026-06-22T07:00:00+00:00",
            steam_net_factor=0.869,
        )

        self.assertEqual(1, report["steamSoldInRange"]["summary"]["count"])
        self.assertEqual(173.8, report["steamSoldInRange"]["summary"]["steamNet"])
        self.assertEqual(["Revolution Case"], [row["marketHashName"] for row in report["steamSoldInRange"]["items"]])

    def test_guadao_discount_report_counts_only_confirmed_c5_success_for_new_orders(self) -> None:
        self._add_closed_cycle(
            "Kilowatt Case",
            steam_price=100.0,
            cash=60.0,
            rebuy_note_extra={"c5OrderId": "order-ok", "c5FinalStatus": "c5_success"},
        )
        self._add_closed_cycle(
            "Revolution Case",
            steam_price=200.0,
            cash=130.0,
            rebuy_note_extra={"c5OrderId": "order-pending"},
        )
        self._add_closed_cycle(
            "Fever Case",
            steam_price=300.0,
            cash=190.0,
            rebuy_note_extra={"c5OrderId": "order-failed", "c5FinalStatus": "c5_failed"},
        )

        report = _build_guadao_discount_report(
            self.db,
            start_utc="2000-01-01T00:00:00+00:00",
            end_utc="2100-01-01T00:00:00+00:00",
            steam_net_factor=0.869,
        )

        self.assertEqual(1, report["summary"]["count"])
        self.assertEqual(100.0, report["summary"]["steamGross"])
        self.assertEqual(86.9, report["summary"]["steamNet"])
        self.assertEqual(60.0, report["summary"]["cash"])
        self.assertEqual(["Kilowatt Case"], [row["marketHashName"] for row in report["items"]])

    def test_guadao_discount_report_counts_successful_replacement_once(self) -> None:
        original_id = self._add_closed_cycle(
            "Kilowatt Case",
            steam_price=100.0,
            cash=60.0,
            rebuy_note_extra={"c5OrderId": "order-old", "c5FinalStatus": "c5_failed"},
        )
        self.db.update_pool_operation(original_id, status="c5_failed")
        self._add_closed_cycle(
            "Kilowatt Case",
            steam_price=100.0,
            cash=61.0,
            rebuy_note_extra={
                "c5OrderId": "order-replacement",
                "c5FinalStatus": "c5_success",
                "replacementForRebuyOperationId": original_id,
            },
        )

        report = _build_guadao_discount_report(
            self.db,
            start_utc="2000-01-01T00:00:00+00:00",
            end_utc="2100-01-01T00:00:00+00:00",
            steam_net_factor=0.869,
        )

        self.assertEqual(1, report["summary"]["count"])
        self.assertEqual(61.0, report["summary"]["cash"])

    def test_guadao_discount_report_summarizes_current_unclosed_sold_steam_balance(self) -> None:
        closed_sell_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=100.0,
            asset_id="asset-closed",
            note=json.dumps({"steamListPrice": 100.0}),
        )
        self.db.update_pool_operation(closed_sell_id, status="sold", actual_price=100.0)
        rebuy_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="rebuy_on_c5",
            expected_price=60.0,
            note=json.dumps({"sourceSellOperationId": closed_sell_id, "steamListPrice": 100.0}),
        )
        self.db.update_pool_operation(rebuy_id, status="completed", actual_price=60.0)
        open_sell_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=200.0,
            asset_id="asset-open",
            note=json.dumps({"steamListPrice": 200.0}),
        )
        self.db.update_pool_operation(open_sell_id, status="sold", actual_price=200.0)
        pending_rebuy_id = self.db.add_pool_operation(
            market_hash_name="Revolution Case",
            strategy="guadao",
            operation_type="rebuy_on_c5",
            expected_price=130.0,
            note=json.dumps({"sourceSellOperationId": open_sell_id, "steamListPrice": 200.0}),
        )
        self.db.update_pool_operation(pending_rebuy_id, status="skipped")
        balance_sell_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=300.0,
            asset_id="asset-balance",
            note=json.dumps({"steamListPrice": 300.0}),
        )
        self.db.update_pool_operation(balance_sell_id, status="sold", actual_price=300.0)
        balance_rebuy_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="rebuy_on_c5",
            expected_price=190.0,
            note=json.dumps(
                {
                    "sourceSellOperationId": balance_sell_id,
                    "steamListPrice": 300.0,
                    "skipReason": "c5_balance_insufficient",
                }
            ),
        )
        self.db.update_pool_operation(balance_rebuy_id, status="skipped")
        old_balance_sell_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="sell_on_steam",
            expected_price=400.0,
            asset_id="asset-old-balance",
            note=json.dumps({"steamListPrice": 400.0}),
        )
        self.db.update_pool_operation(old_balance_sell_id, status="sold", actual_price=400.0)
        old_balance_rebuy_id = self.db.add_pool_operation(
            market_hash_name="Kilowatt Case",
            strategy="guadao",
            operation_type="rebuy_on_c5",
            expected_price=250.0,
            note=json.dumps(
                {
                    "sourceSellOperationId": old_balance_sell_id,
                    "steamListPrice": 400.0,
                    "failedReason": 'c5_api_error: {"errorCode": 70001, "errorMsg": "浣欓涓嶈冻"}',
                }
            ),
        )
        self.db.update_pool_operation(old_balance_rebuy_id, status="failed")

        report = _build_guadao_discount_report(
            self.db,
            start_utc="2000-01-01T00:00:00+00:00",
            end_utc="2100-01-01T00:00:00+00:00",
            steam_net_factor=0.869,
        )

        unclosed = report["unclosedSoldSteam"]["summary"]
        self.assertEqual(1, unclosed["count"])
        self.assertEqual(200.0, unclosed["steamGross"])
        self.assertEqual(173.8, unclosed["steamNet"])


if __name__ == "__main__":
    unittest.main()
