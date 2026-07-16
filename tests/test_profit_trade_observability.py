from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database, PROFIT_TRADE_OBSERVABILITY_TABLES
from cs2_assistant.models import MarketState, StrategyConfig
from cs2_assistant.services.profit_trade import (
    STEAM_BUY_LISTING_RETRY_ATTEMPTS,
    build_profit_trade_interruption_timeline_payload,
    build_profit_trade_interruptions_payload,
    build_profit_trade_roi_history_payload,
    build_profit_trade_roi_watch_payload,
    scan_profit_trade_opportunities,
    set_profit_trade_interruption_acknowledged,
)


MARKET_HASH_NAME = "USP-S | Tropical Breeze (Factory New)"


def profit_config(**overrides: object) -> StrategyConfig:
    values: dict[str, object] = {
        "profit_trade_enabled": True,
        "profit_trade_min_roi": 0.08,
        "profit_trade_min_item_value": 5.0,
        "profit_trade_require_c5_recent_sales": False,
        "profit_trade_require_c5_market_depth": False,
        "profit_trade_manual_review_roi": 0.20,
        "profit_trade_sticker_slab_status": "active",
        "profit_trade_sticker_status": "active",
        "profit_trade_balance_discount": 0.69,
    }
    values.update(overrides)
    return StrategyConfig(**values)


class FixedMarketService:
    def __init__(self, *, steam_price: float = 100.0, c5_price: float = 75.0) -> None:
        self.steam_price = steam_price
        self.c5_price = c5_price

    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        return [
            MarketState(
                market_hash_name=str(item["market_hash_name"]),
                name_cn=str(item.get("name_cn") or item["market_hash_name"]),
                steam_sell_price=self.steam_price,
                steam_price_source="steam_orderbook",
                c5_sell_price=self.c5_price,
                c5_price_source="c5_batch",
            )
            for item in items
        ]


class FailingMarketService:
    def refresh_items(self, items: list[dict]) -> list[MarketState]:
        raise RuntimeError("global market refresh failed")


class LowDepthC5Client:
    def price_statistics_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict:
        return {
            name: {
                "marketHashName": name,
                "currentSellPrice": 75.0,
                "onSaleCount": 1,
                "purchaseMaxPrice": 70.0,
                "purchaseCount": 1,
            }
            for name in market_hash_names
        }


class ProfitTradeObservabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.temp_dir.name) / "assistant.db")
        self.inventory_payload = {
            "source": "fixture",
            "list": [
                {
                    "assetId": "asset-tropical",
                    "marketHashName": MARKET_HASH_NAME,
                    "name": "USP消音版 | 椰风花语（崭新出厂）",
                    "steamId": "76561199119018953",
                    "ifTradable": True,
                    "price": 75.0,
                    "token": "safe-test-token",
                    "styleToken": "safe-test-style-token",
                }
            ],
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _open_db(self) -> Database:
        db = Database(self.settings.db_path)
        db.initialize()
        return db

    def test_positive_roi_below_minimum_is_watched_but_never_executable(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_min_roi=0.08),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            record=True,
            lock_asset=True,
        )

        self.assertEqual(0, report.opportunity_count)
        self.assertEqual([], report.created_trade_ids)
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual(1, watch["total"])
        self.assertGreater(watch["items"][0]["expectedRoi"], 0)
        self.assertEqual("observe_only", watch["items"][0]["executionStatus"])
        self.assertEqual("below_min_roi", watch["items"][0]["executionStatusCode"])

        db = self._open_db()
        try:
            self.assertEqual([], db.list_profit_trades(limit=10))
            self.assertIsNone(db.get_active_asset_reservation("asset-tropical"))
        finally:
            db.close()

    def test_positive_roi_blocked_by_c5_depth_is_still_watched(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_min_roi=0.01,
                profit_trade_require_c5_market_depth=True,
                profit_trade_c5_min_on_sale_count=3,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            c5_client=LowDepthC5Client(),
            record=True,
        )

        self.assertEqual(0, report.opportunity_count)
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual("blocked", watch["items"][0]["executionStatus"])
        self.assertEqual("c5_risk_blocked", watch["items"][0]["executionStatusCode"])
        self.assertEqual("blocked_low_c5_listing_depth", watch["items"][0]["riskStatus"])

    def test_manual_review_market_evaluation_is_watched_and_not_auto_locked(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(
                profit_trade_min_roi=0.01,
                profit_trade_manual_review_roi=0.04,
            ),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            record=True,
            lock_asset=True,
        )

        self.assertEqual(1, report.opportunity_count)
        self.assertEqual([], report.locked_trade_ids)
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual("manual_review", watch["items"][0]["executionStatus"])
        db = self._open_db()
        try:
            trade = db.get_profit_trade(report.created_trade_ids[0])
            self.assertEqual("manual_required", trade["status"])
            self.assertIsNone(db.get_active_asset_reservation("asset-tropical"))
        finally:
            db.close()

    def test_executable_observation_links_to_live_trade_status_and_completion(self) -> None:
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_min_roi=0.05),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            record=True,
            lock_asset=True,
        )

        self.assertEqual(1, report.opportunity_count)
        trade_id = report.created_trade_ids[0]
        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual(trade_id, watch["items"][0]["latestTrade"]["tradeId"])
        self.assertEqual("locked", watch["items"][0]["latestTrade"]["status"])

        history = build_profit_trade_roi_history_payload(self.settings, MARKET_HASH_NAME)
        linked = history["items"][0]["relatedTrade"]
        self.assertEqual(trade_id, linked["tradeId"])
        self.assertEqual("locked", linked["status"])

        db = self._open_db()
        try:
            db.update_profit_trade(
                trade_id,
                status="steam_bought",
                step_key="steam_bought",
                step_index=3,
                steam_buy_price=100.0,
                note=json.dumps(
                    {
                        **json.loads(db.get_profit_trade(trade_id)["note"]),
                        "steamBuySucceededAt": "2026-07-14T12:16:49+00:00",
                    },
                    ensure_ascii=False,
                ),
            )
        finally:
            db.close()

        bought = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
        )["items"][0]["relatedTrade"]
        self.assertEqual("steam_bought", bought["status"])
        self.assertEqual("2026-07-14T12:16:49+00:00", bought["steamBoughtAt"])

        db = self._open_db()
        try:
            db.update_profit_trade(
                trade_id,
                status="completed",
                step_key="settled",
                step_index=6,
                c5_sold_net_price=80.0,
                realized_profit=11.0,
                realized_roi=0.11,
                completed_at="2026-07-14T14:19:38+00:00",
            )
        finally:
            db.close()

        completed = build_profit_trade_roi_history_payload(
            self.settings,
            MARKET_HASH_NAME,
        )["items"][0]["relatedTrade"]
        self.assertEqual("completed", completed["status"])
        self.assertEqual(80.0, completed["c5SoldNetPrice"])
        self.assertEqual(11.0, completed["realizedProfit"])
        self.assertEqual(0.11, completed["realizedRoi"])

    def test_completed_scan_exits_stale_watch_and_preserves_history(self) -> None:
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
        )
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=60.0),
        )

        self.assertEqual(0, build_profit_trade_roi_watch_payload(self.settings)["total"])
        inactive = build_profit_trade_roi_watch_payload(self.settings, active=False)
        self.assertEqual(1, inactive["total"])
        self.assertEqual("exited", inactive["items"][0]["executionStatus"])
        self.assertIn("not positive", inactive["items"][0]["exitReason"])
        history = build_profit_trade_roi_history_payload(self.settings, MARKET_HASH_NAME)
        self.assertEqual(["exited", "entered"], [item["eventType"] for item in history["items"]])
        self.assertLess(history["items"][0]["expectedRoi"], 0)

    def test_global_scan_failure_does_not_clear_existing_watch(self) -> None:
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
        )

        with self.assertRaisesRegex(RuntimeError, "global market refresh failed"):
            scan_profit_trade_opportunities(
                self.settings,
                profit_config(),
                inventory_payload=self.inventory_payload,
                market_service=FailingMarketService(),
            )

        watch = build_profit_trade_roi_watch_payload(self.settings)
        self.assertEqual(1, watch["total"])
        history = build_profit_trade_roi_history_payload(self.settings, MARKET_HASH_NAME)
        self.assertEqual(1, history["total"])

    def test_protected_item_is_removed_from_current_watch_without_execution(self) -> None:
        scan_profit_trade_opportunities(
            self.settings,
            profit_config(),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
        )
        report = scan_profit_trade_opportunities(
            self.settings,
            profit_config(profit_trade_protected_market_hash_names=[MARKET_HASH_NAME]),
            inventory_payload=self.inventory_payload,
            market_service=FixedMarketService(c5_price=75.0),
            record=True,
        )

        self.assertEqual(0, report.evaluated_count)
        self.assertEqual(0, report.opportunity_count)
        self.assertEqual([], report.created_trade_ids)
        self.assertEqual(0, build_profit_trade_roi_watch_payload(self.settings)["total"])

    def test_state_transition_and_event_are_one_transaction(self) -> None:
        db = self._open_db()
        try:
            trade_id = db.add_profit_trade(
                trade_no="PT-observability-transaction",
                market_hash_name=MARKET_HASH_NAME,
            )
            db.conn.execute(
                """
                CREATE TRIGGER fail_profit_trade_transition
                BEFORE INSERT ON profit_trade_state_events
                WHEN NEW.event_type = 'transition'
                BEGIN
                    SELECT RAISE(ABORT, 'forced state event failure');
                END
                """
            )
            db.conn.commit()

            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced state event failure"):
                db.update_profit_trade(
                    trade_id,
                    status="locked",
                    step_key="asset_locked",
                    step_index=2,
                )

            trade = db.get_profit_trade(trade_id)
            self.assertEqual("candidate", trade["status"])
            self.assertEqual("discovered", trade["step_key"])
            self.assertEqual(1, len(db.list_profit_trade_state_events(trade_id)))
        finally:
            db.close()

    def test_historical_trade_without_events_gets_only_truthful_snapshot(self) -> None:
        db = self._open_db()
        try:
            trade_id = db.add_profit_trade(
                trade_no="PT-observability-history",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="HTTP 429 before purchase",
            )
            db.conn.execute("DELETE FROM profit_trade_state_events WHERE trade_id = ?", (trade_id,))
            db.conn.commit()
        finally:
            db.close()

        timeline = build_profit_trade_interruption_timeline_payload(self.settings, trade_id)
        self.assertEqual(1, len(timeline["events"]))
        event = timeline["events"][0]
        self.assertEqual("historical_snapshot", event["eventType"])
        self.assertTrue(event["isSnapshot"])
        self.assertEqual("cancelled", event["statusTo"])
        self.assertEqual("asset_locked", event["stepKeyTo"])
        self.assertIsNone(event["statusFrom"])

    def test_acknowledgement_filters_and_refuses_uncertain_buy_order(self) -> None:
        db = self._open_db()
        try:
            safe_trade_id = db.add_profit_trade(
                trade_no="PT-observability-safe-ack",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="search_listings HTTP 429",
            )
            unsafe_trade_id = db.add_profit_trade(
                trade_no="PT-observability-unsafe-ack",
                market_hash_name="Dreams & Nightmares Case",
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                note='{"steamBuyMethod":"createbuyorder","steamBuyOrderId":"buy-order-live"}',
            )
        finally:
            db.close()

        safe = set_profit_trade_interruption_acknowledged(
            self.settings,
            safe_trade_id,
            acknowledged=True,
            reason="user reviewed the 429 evidence",
        )
        self.assertTrue(safe["ok"])
        default_list = build_profit_trade_interruptions_payload(self.settings)
        self.assertEqual([unsafe_trade_id], [item["id"] for item in default_list["items"]])
        acknowledged_list = build_profit_trade_interruptions_payload(
            self.settings,
            acknowledged="only",
        )
        self.assertEqual([safe_trade_id], [item["id"] for item in acknowledged_list["items"]])

        unsafe = set_profit_trade_interruption_acknowledged(
            self.settings,
            unsafe_trade_id,
            acknowledged=True,
            reason="must not disappear silently",
        )
        self.assertFalse(unsafe["ok"])
        self.assertTrue(unsafe["conflict"])
        self.assertTrue(unsafe["requiresRemoteResolution"])
        still_visible = build_profit_trade_interruptions_payload(self.settings)
        self.assertIn(unsafe_trade_id, [item["id"] for item in still_visible["items"]])

        restored = set_profit_trade_interruption_acknowledged(
            self.settings,
            safe_trade_id,
            acknowledged=False,
            reason="restore to active problem list",
        )
        self.assertTrue(restored["ok"])
        visible = build_profit_trade_interruptions_payload(self.settings)
        self.assertIn(safe_trade_id, [item["id"] for item in visible["items"]])

    def test_interruption_search_supports_chinese_name_note_and_js_iso_time(self) -> None:
        db = self._open_db()
        try:
            trade_id = db.add_profit_trade(
                trade_no="PT-observability-chinese-search",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="Steam listings search failed",
                note=(
                    '{"name":"USP消音版 | 椰风花语（崭新出厂）",'
                    '"cancelSource":"profit_trade_search_listings",'
                    '"cancelReason":"Steam HTTP 429"}'
                ),
            )
            row = db.get_profit_trade(trade_id)
            boundary = str(row["completed_at"] or row["updated_at"])
        finally:
            db.close()

        js_boundary = boundary.replace("+00:00", "Z")
        payload = build_profit_trade_interruptions_payload(
            self.settings,
            keyword="椰风花语",
            from_time=js_boundary,
            to_time=js_boundary,
        )
        self.assertEqual(1, payload["total"])
        self.assertEqual(trade_id, payload["items"][0]["id"])
        self.assertEqual("profit_trade_search_listings", payload["items"][0]["cancelSource"])
        self.assertEqual("Steam HTTP 429", payload["items"][0]["cancelReason"])
        self.assertEqual(1, payload["summary"]["total"])

    def test_id103_style_history_projects_not_sent_only_from_strict_pre_buy_evidence(self) -> None:
        db = self._open_db()
        try:
            id103_style = db.add_profit_trade(
                trade_no="PT-20260711-85483d1c68",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                note=(
                    '{"cancelSource":"profit_trade_pre_buy_cancel",'
                    '"cancelReason":"automatic run cancelled locked trade before Steam buy after error: '
                    'Steam listings search failed: 429 Too Many Requests"}'
                ),
            )
            ambiguous = db.add_profit_trade(
                trade_no="PT-ambiguous-asset-locked-cancel",
                market_hash_name="AK-47 | Redline (Field-Tested)",
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                note='{"cancelSource":"user_cancelled","cancelReason":"user stopped the trade"}',
            )
            purchase_evidence = db.add_profit_trade(
                trade_no="PT-pre-buy-source-with-purchase-evidence",
                market_hash_name="Dreams & Nightmares Case",
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                note=(
                    '{"cancelSource":"profit_trade_pre_buy_cancel",'
                    '"cancelReason":"legacy inconsistent record",'
                    '"steamBuyRequestedAt":"2026-07-11T07:03:45+00:00"}'
                ),
            )
            listing_price_guard = db.add_profit_trade(
                trade_no="PT-listing-price-guard",
                market_hash_name="USP-S | Tropical Breeze (Factory New)",
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                steam_listing_id="507367553952201686",
                error=(
                    "Steam listing price moved too much above orderbook before buy: "
                    "17.17 > 15.87 * 1.0100"
                ),
                note=(
                    '{"cancelSource":"profit_trade_buy_listing_price_guard",'
                    '"cancelReason":"Steam listing price moved too much above orderbook '
                    'before buy: 17.17 > 15.87 * 1.0100"}'
                ),
            )
        finally:
            db.close()

        projected = build_profit_trade_interruption_timeline_payload(self.settings, id103_style)["trade"]
        self.assertIs(projected["purchaseRequestSent"], False)
        self.assertIs(projected["listingIdObtained"], False)
        self.assertIs(projected["note"]["purchaseRequestSent"], False)
        self.assertIs(projected["note"]["listingIdObtained"], False)

        unknown = build_profit_trade_interruption_timeline_payload(self.settings, ambiguous)["trade"]
        self.assertIsNone(unknown["purchaseRequestSent"])
        self.assertIsNone(unknown["listingIdObtained"])
        self.assertNotIn("purchaseRequestSent", unknown["note"])

        sent = build_profit_trade_interruption_timeline_payload(self.settings, purchase_evidence)["trade"]
        self.assertIs(sent["purchaseRequestSent"], True)

        guarded = build_profit_trade_interruption_timeline_payload(
            self.settings,
            listing_price_guard,
        )["trade"]
        self.assertIs(guarded["listingIdObtained"], True)
        self.assertIs(guarded["purchaseRequestSent"], False)
        self.assertEqual("507367553952201686", guarded["steamListingId"])

        db = self._open_db()
        try:
            original = db.get_profit_trade(id103_style)
            original_note = json.loads(str(original["note"] or "{}"))
        finally:
            db.close()
        self.assertNotIn("purchaseRequestSent", original_note)
        self.assertNotIn("listingIdObtained", original_note)

    def test_interruption_query_rejects_non_interruption_status_and_bad_ack_filter(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid.*status"):
            build_profit_trade_interruptions_payload(
                self.settings,
                statuses=("completed",),
            )
        with self.assertRaisesRegex(ValueError, "acknowledged"):
            build_profit_trade_interruptions_payload(
                self.settings,
                acknowledged="anything",
            )

    def test_existing_database_is_backed_up_before_additive_observability_upgrade(self) -> None:
        db = self._open_db()
        try:
            trade_id = db.add_profit_trade(
                trade_no="PT-observability-before-upgrade",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
            )
            db.conn.executescript(
                """
                DROP TABLE profit_trade_acknowledgements;
                DROP TABLE profit_trade_state_events;
                DROP TABLE profit_trade_roi_observations;
                DROP TABLE profit_trade_roi_watch;
                DROP TABLE profit_trade_runtime_state;
                """
            )
            db.conn.commit()
        finally:
            db.close()

        upgraded = Database(self.settings.db_path)
        try:
            upgraded.initialize()
            upgraded.initialize()
            tables = {
                str(row[0])
                for row in upgraded.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            upgraded.close()

        self.assertTrue(
            {
                "profit_trade_roi_watch",
                "profit_trade_roi_observations",
                "profit_trade_state_events",
                "profit_trade_acknowledgements",
            }.issubset(tables)
        )
        backups = list(
            self.settings.db_path.parent.glob(
                f"{self.settings.db_path.stem}.pre-profit-trade-observability-*{self.settings.db_path.suffix}"
            )
        )
        self.assertEqual(1, len(backups))
        backup = sqlite3.connect(backups[0])
        try:
            row = backup.execute("SELECT trade_no FROM profit_trades WHERE id = ?", (trade_id,)).fetchone()
            backup_tables = {
                str(value[0])
                for value in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            backup.close()
        self.assertEqual("PT-observability-before-upgrade", row[0])
        self.assertFalse(PROFIT_TRADE_OBSERVABILITY_TABLES.intersection(backup_tables))

    def test_original_three_purchase_attempt_policy_is_unchanged(self) -> None:
        self.assertEqual(3, STEAM_BUY_LISTING_RETRY_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
