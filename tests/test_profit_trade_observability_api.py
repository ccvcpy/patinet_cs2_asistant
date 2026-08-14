from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.models import CatalogItem
from cs2_assistant.services.strategy import load_strategy_config
import cs2_assistant.services.web_api as web_api


MARKET_HASH_NAME = "USP-S | Tropical Breeze (Factory New)"
LOOPBACK_OPENER = build_opener(ProxyHandler({}))


class OneRequestServer(HTTPServer):
    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.handle_request()


class FakeSweeper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def close(self) -> None:
        return


class FakeLogger:
    pass


class FakeRuntimeController:
    """No-worker runtime used when a route test must not start background I/O."""

    def __init__(self) -> None:
        self.wake_calls = 0
        self.manual_status_requests: list[str] = []
        self.selection_refresh_calls = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def wake(self) -> None:
        self.wake_calls += 1

    def profit_trade_manual_execution_status(self, request_id: str) -> dict:
        self.manual_status_requests.append(request_id)
        return {
            "ok": True,
            "requestId": request_id,
            "taskKey": f"profit-manual:{request_id}",
            "marketHashName": MARKET_HASH_NAME,
            "name": "USP消音版 | 椰风花语（崭新出厂）",
            "requestedQuantity": 1,
            "status": "failed",
            "terminal": True,
            "summary": "一键执行失败：测试错误",
            "error": "测试错误",
            "counts": {"created": 1, "bought": 0, "listed": 0, "failed": 1},
            "trades": [],
        }

    def profit_selection_watch_now(self) -> dict:
        self.selection_refresh_calls += 1
        return {
            "ok": True,
            "taskKey": "profit_selection_watch",
            "queued": True,
            "alreadyRunning": False,
            "researchOnly": True,
            "canExecute": False,
        }


class FakeCaseMonitorController:
    def start(self) -> None:
        return None


class ProfitTradeObservabilityApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.temp_dir.name) / "assistant.db")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
        finally:
            db.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_profit_trade_sse_market_name_filter_matches_only_selected_item(self) -> None:
        filters = {"marketHashName": MARKET_HASH_NAME}
        self.assertTrue(
            web_api._profit_trade_log_event_matches(
                {"market_hash_name": MARKET_HASH_NAME, "message": "selected"},
                filters,
            )
        )
        self.assertFalse(
            web_api._profit_trade_log_event_matches(
                {
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "message": MARKET_HASH_NAME,
                },
                filters,
            )
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        runtime_controller: FakeRuntimeController | None = None,
    ) -> tuple[int, dict]:
        # Route tests must not start a real scheduler.  A real controller can
        # still be shutting down a worker after the response is sent, while
        # this one-request HTTP fixture only waits three seconds for its
        # server thread to exit.
        runtime_controller = runtime_controller or FakeRuntimeController()
        port = self._free_port()
        server_errors: list[BaseException] = []

        def run() -> None:
            try:
                web_api.run_profit_trade_api_server(
                    self.settings,
                    host="127.0.0.1",
                    port=port,
                    runtime_controller=runtime_controller,
                    case_monitor_controller=FakeCaseMonitorController(),
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                server_errors.append(exc)

        with (
            patch.object(web_api, "ThreadingHTTPServer", OneRequestServer),
            patch.object(web_api, "C5CaseSweeper", FakeSweeper),
            patch.object(web_api, "get_profit_trade_event_logger", return_value=FakeLogger()),
        ):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            raw_body = json.dumps(body or {}).encode("utf-8") if body is not None else None
            request = Request(
                f"http://127.0.0.1:{port}{path}",
                data=raw_body,
                method=method,
                headers={
                    **({"Content-Type": "application/json"} if raw_body is not None else {}),
                    "Connection": "close",
                },
            )
            response_body = b""
            status = 0
            deadline = time.monotonic() + 3.0
            while True:
                try:
                    with LOOPBACK_OPENER.open(request, timeout=2.0) as response:
                        status = int(response.status)
                        response_body = response.read()
                    break
                except HTTPError as exc:
                    status = int(exc.code)
                    response_body = exc.read()
                    break
                except URLError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            thread.join(timeout=3.0)
        if thread.is_alive():
            self.fail("one-request API server did not stop")
        if server_errors:
            raise server_errors[0]
        return status, json.loads(response_body.decode("utf-8"))

    def test_profit_trade_config_rejects_removed_reprice_only_field(self) -> None:
        removed_field = "allow" + "RepriceExecution"

        status, payload = self._request(
            "POST",
            "/api/profit-trade/config",
            body={removed_field: True},
        )

        self.assertEqual(400, status)
        self.assertIn("unsupported config field", payload["error"])

        status, payload = self._request(
            "POST",
            "/api/profit-trade/config",
            body={"allowRealExecution": False},
        )

        self.assertEqual(200, status)
        self.assertNotIn(removed_field, payload)
        self.assertNotIn(removed_field, payload["config"])

    def test_profit_trade_long_buy_config_round_trip_and_validation(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/profit-trade/config",
            body={
                "longBuyEnabled": True,
                "longBuyAllowRealExecution": False,
                "longBuyMaxActiveOrders": 25,
                "longBuyCreateFractionPerCycle": 0.2,
                "longBuyAggressiveRoiDelta": 0.005,
                "longBuyMinPriceAdvantage": 0.1,
                "longBuyMaxPriceAdvantage": 1.0,
            },
        )

        self.assertEqual(200, status)
        self.assertTrue(payload["longBuyEnabled"])
        self.assertFalse(payload["longBuyAllowRealExecution"])
        self.assertTrue(payload["config"]["longBuyEnabled"])
        self.assertFalse(payload["config"]["longBuyAllowRealExecution"])
        config = load_strategy_config(self.settings)
        self.assertEqual(25, config.profit_trade_long_buy_max_active_orders)
        self.assertEqual(
            0.2,
            config.profit_trade_long_buy_create_fraction_per_cycle,
        )
        self.assertEqual(0.005, config.profit_trade_long_buy_aggressive_roi_delta)
        self.assertEqual(0.1, config.profit_trade_long_buy_min_price_advantage)
        self.assertEqual(1.0, config.profit_trade_long_buy_max_price_advantage)

        invalid_status, invalid = self._request(
            "POST",
            "/api/profit-trade/config",
            body={"longBuyCreateFractionPerCycle": 0},
        )

        self.assertEqual(400, invalid_status)
        self.assertIn("longBuyCreateFractionPerCycle", invalid["error"])

    def test_roi_watch_and_history_endpoints_match_frontend_contract(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.record_profit_trade_roi_scan(
                [
                    {
                        "market_hash_name": MARKET_HASH_NAME,
                        "name_cn": "USP消音版 | 椰风花语（崭新出厂）",
                        "steam_buy_price": 100.0,
                        "c5_listing_price": 75.0,
                        "c5_expected_net_price": 74.25,
                        "balance_discount": 0.69,
                        "expected_profit": 5.25,
                        "expected_roi": 0.0525,
                        "buy_order_reference_roi": (74.25 / 101.0) - 0.69,
                        "buy_order_reference_profit": 74.25 - (101.0 * 0.69),
                        "buy_order_reference_status": "crossed_possible_stale",
                        "min_roi": 0.08,
                        "manual_review_roi": 0.20,
                        "inventory_count": 1,
                        "tradable_count": 1,
                        "risk_status": "passed",
                        "execution_status": "below_min_roi",
                        "execution_reason": "ROI below automatic threshold",
                        "raw": {
                            "c5PurchaseSellRatio": 0.6491,
                            "c5MinPurchaseSellRatio": 0.70,
                            "c5CurrentSellPrice": 6.47,
                            "c5PurchaseMaxPrice": 4.20,
                            "competitorBuyPrice": 99.0,
                            "competitorBuyRoi": 0.06,
                            "competitorBuyProfit": 6.0,
                            "competitorBuyStatus": "self_price_excluded",
                            "excludedOwnBuyPrices": [100.0],
                            "longBuyProposal": {
                                "eligible": False,
                                "targetPrice": 100.0,
                                "quantity": 1,
                                "decision": "standard_safe_price",
                            },
                            "steamOrderbook": {
                                "observedAt": "2026-07-13T01:02:02+00:00",
                                "currencyId": 23,
                                "sellerFloorPrice": 100.0,
                                "sellerFloorCount": 1,
                                "buyerMaxPrice": 101.0,
                                "buyerMaxCount": 2,
                                "spreadAmount": -1.0,
                                "spreadPct": -0.01,
                                "crossed": True,
                                "sellLevels": [{"price": 100.0, "count": 1}],
                                "buyLevels": [{"price": 101.0, "count": 2}],
                            }
                        },
                    }
                ],
                scan_id="PTSCAN-api-contract",
                observed_at="2026-07-13T01:02:03+00:00",
            )
            long_buy_id = db.create_profit_trade_long_buy_order(
                market_hash_name=MARKET_HASH_NAME,
                steam_account_id="account-a",
                steam_id="steam-a",
                create_request_id="PTLB-api-contract",
                bid_price_cents=10000,
                quantity=1,
                c5_price_batch=75.0,
                c5_expected_net_price=74.25,
                balance_discount=0.69,
                standard_roi=0.08,
                aggressive_roi=0.075,
                standard_safe_price_cents=9642,
                aggressive_safe_price_cents=9705,
                competitor_buy_price_cents=9900,
                competitor_buy_status="self_price_excluded",
                worst_case_roi=0.0525,
                source_scan_id="PTSCAN-api-contract",
                wallet_before=1000.0,
            )
            db.update_profit_trade_long_buy_order(
                long_buy_id,
                event_type="remote_created",
                state="active",
                buy_order_id="buy-api-contract",
            )
            trade_id = db.add_profit_trade(
                trade_no="PT-api-linked",
                market_hash_name=MARKET_HASH_NAME,
                status="steam_bought",
                step_key="steam_bought",
                step_index=3,
                steam_buy_price=100.0,
                note=json.dumps(
                    {
                        "source": "profit_trade_scan",
                        "originScanId": "PTSCAN-api-contract",
                        "steamBuySucceededAt": "2026-07-13T01:03:04+00:00",
                    },
                    ensure_ascii=False,
                ),
            )
        finally:
            db.close()

        status, payload = self._request(
            "GET",
            "/api/profit-trade/roi-watch?active=true&page=1&pageSize=12&sort=roi_desc",
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual({"items", "total", "page", "pageSize"}, {key for key in payload if key in {"items", "total", "page", "pageSize"}})
        self.assertEqual("observe_only", payload["items"][0]["executionStatus"])
        self.assertEqual("below_min_roi", payload["items"][0]["executionStatusCode"])
        self.assertEqual(trade_id, payload["items"][0]["latestTrade"]["tradeId"])
        self.assertEqual("steam_bought", payload["items"][0]["latestTrade"]["status"])
        self.assertEqual(101.0, payload["items"][0]["steamOrderbook"]["buyerMaxPrice"])
        self.assertTrue(payload["items"][0]["steamOrderbook"]["crossed"])
        self.assertEqual(0.69, payload["items"][0]["roiBasis"])
        self.assertEqual("crossed_possible_stale", payload["items"][0]["buyOrderReferenceStatus"])
        self.assertEqual(0.6491, payload["items"][0]["c5PurchaseSellRatio"])
        self.assertEqual(0.70, payload["items"][0]["c5MinPurchaseSellRatio"])
        self.assertEqual(6.47, payload["items"][0]["c5CurrentSellPrice"])
        self.assertEqual(4.20, payload["items"][0]["c5PurchaseMaxPrice"])
        self.assertEqual(99.0, payload["items"][0]["competitorBuyPrice"])
        self.assertEqual([100.0], payload["items"][0]["excludedOwnBuyPrices"])
        self.assertEqual(
            "buy-api-contract",
            payload["items"][0]["longBuyOrder"]["buyOrderId"],
        )
        self.assertEqual(
            "standard_safe_price",
            payload["items"][0]["longBuyProposal"]["decision"],
        )
        self.assertAlmostEqual((74.25 / 101.0) - 0.69, payload["items"][0]["buyOrderReferenceRoi"])
        self.assertAlmostEqual(5.25, payload["summary"]["currentExpectedProfitTotal"])
        self.assertEqual(0.0, payload["summary"]["buyOrderReferenceProfitTotal"])
        self.assertEqual(1, payload["summary"]["longBuyActiveOrders"])

        positive_status, positive_payload = self._request(
            "GET",
            "/api/profit-trade/roi-watch?active=true&roiSign=positive&page=1&pageSize=12",
        )
        self.assertEqual(200, positive_status)
        self.assertEqual(1, positive_payload["total"])

        negative_status, negative_payload = self._request(
            "GET",
            "/api/profit-trade/roi-watch?active=true&roiSign=negative&page=1&pageSize=12",
        )
        self.assertEqual(200, negative_status)
        self.assertEqual(0, negative_payload["total"])

        invalid_status, invalid_payload = self._request(
            "GET",
            "/api/profit-trade/roi-watch?active=true&roiSign=zero",
        )
        self.assertEqual(400, invalid_status)
        self.assertIn("roiSign", invalid_payload["error"])

        dashboard_status, dashboard = self._request(
            "GET",
            "/api/profit-trade/dashboard",
        )
        self.assertEqual(200, dashboard_status)
        self.assertEqual(1, dashboard["summary"]["longBuyActiveOrders"])

        query = urlencode(
            {
                "marketHashName": MARKET_HASH_NAME,
                "from": "2026-07-13T01:02:03.000Z",
                "to": "2026-07-13T01:02:03.000Z",
                "page": 1,
                "pageSize": 20,
            }
        )
        history_status, history = self._request(
            "GET",
            f"/api/profit-trade/roi-watch/history?{query}",
        )
        self.assertEqual(200, history_status)
        self.assertEqual(1, history["total"])
        self.assertEqual("PTSCAN-api-contract", history["items"][0]["scanId"])
        self.assertEqual(trade_id, history["items"][0]["relatedTrade"]["tradeId"])
        self.assertEqual("steam_bought", history["items"][0]["relatedTrade"]["status"])
        self.assertEqual(
            [{"price": 101.0, "count": 2}],
            history["items"][0]["steamOrderbook"]["buyLevels"],
        )
        self.assertAlmostEqual(0.0525, history["stats"]["highestRoi"])
        self.assertEqual(0.69, history["stats"]["roiBasis"])
        self.assertEqual(1, history["trend"]["totalValidPoints"])
        self.assertEqual("2026-07-13T01:02:03+00:00", history["trend"]["points"][0]["observedAt"])

    def test_manual_execution_status_endpoint_returns_batch_terminal_result(self) -> None:
        runtime = FakeRuntimeController()
        request_id = "PTMAN-api-status"

        status, payload = self._request(
            "GET",
            "/api/profit-trade/manual-execution/status?"
            + urlencode({"requestId": request_id}),
            runtime_controller=runtime,
        )

        self.assertEqual(200, status)
        self.assertEqual([request_id], runtime.manual_status_requests)
        self.assertEqual(request_id, payload["requestId"])
        self.assertEqual("failed", payload["status"])
        self.assertTrue(payload["terminal"])
        self.assertEqual(1, payload["counts"]["failed"])

    def test_selection_watch_api_is_research_only_and_matches_shared_history_contract(self) -> None:
        selection_name = "Glock-18 | Selection Research (Field-Tested)"
        fake_runtime = FakeRuntimeController()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.upsert_items(
                [
                    CatalogItem(
                        market_hash_name=selection_name,
                        name_cn="Glock-18 | 选品研究（久经沙场）",
                        raw_json={
                            "category": {"name": "手枪"},
                            "weapon": {"name": "Glock-18"},
                            "rarity": {"name": "保密级", "color": "#d32ce6"},
                            "wear": {"name": "久经沙场"},
                            "min_float": 0.10,
                            "max_float": 0.70,
                            "image": "https://example.invalid/selection.png",
                        },
                    )
                ]
            )
        finally:
            db.close()

        rejected_status, rejected = self._request(
            "POST",
            "/api/profit-trade/selection-watch",
            body={"action": "add", "marketHashName": "arbitrary-not-in-local-catalog"},
            runtime_controller=fake_runtime,
        )
        self.assertEqual(400, rejected_status)
        self.assertIn("local catalog", rejected["error"])
        self.assertEqual(0, fake_runtime.wake_calls)

        add_status, added = self._request(
            "POST",
            "/api/profit-trade/selection-watch",
            body={"action": "add", "marketHashName": selection_name},
            runtime_controller=fake_runtime,
        )
        self.assertEqual(200, add_status)
        self.assertTrue(added["researchOnly"])
        self.assertFalse(added["canExecute"])
        self.assertEqual("selection_only", added["item"]["executionStatus"])
        self.assertEqual(1, fake_runtime.wake_calls)

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.record_profit_trade_selection_watch_scan(
                [
                    {
                        "market_hash_name": selection_name,
                        "name_cn": "Glock-18 | 选品研究（久经沙场）",
                        "status": "observed",
                        "event_type": "observed",
                        "steam_buy_price": 10.0,
                        "steam_price_source": "steam_orderbook",
                        "c5_listing_price": 9.0,
                        "c5_price_source": "c5_batch",
                        "c5_expected_net_price": 8.91,
                        "balance_discount": 0.69,
                        "expected_profit": 2.01,
                        "expected_roi": 0.201,
                        "buy_order_reference_roi": 0.246,
                        "buy_order_reference_profit": 2.46,
                        "buy_order_reference_status": "valid",
                        "raw": {
                            "steamOrderbook": {
                                "observedAt": "2026-07-23T01:02:03+00:00",
                                "currencyId": 23,
                                "sellerFloorPrice": 10.0,
                                "sellerFloorCount": 1,
                                "buyerMaxPrice": 9.5,
                                "buyerMaxCount": 2,
                                "crossed": False,
                                "sellLevels": [{"price": 10.0, "count": 1}],
                                "buyLevels": [{"price": 9.5, "count": 2}],
                            }
                        },
                    }
                ],
                scan_id="PTSEL-api-contract",
                observed_at="2026-07-23T01:02:04+00:00",
            )
        finally:
            db.close()

        watch_status, watch = self._request(
            "GET",
            "/api/profit-trade/selection-watch?active=true&page=1&pageSize=12&sort=roi_desc",
            runtime_controller=fake_runtime,
        )
        self.assertEqual(200, watch_status)
        self.assertTrue(watch["researchOnly"])
        self.assertFalse(watch["canExecute"])
        self.assertEqual(selection_name, watch["items"][0]["marketHashName"])
        self.assertEqual("selection_only", watch["items"][0]["executionStatus"])
        self.assertEqual(0, watch["items"][0]["inventoryCount"])
        self.assertEqual(0, watch["items"][0]["tradableCount"])
        self.assertEqual(9.5, watch["items"][0]["steamOrderbook"]["buyerMaxPrice"])
        self.assertEqual("手枪", watch["items"][0]["itemType"])
        self.assertEqual("保密级", watch["items"][0]["rarityName"])
        self.assertEqual("久经沙场", watch["items"][0]["wearName"])
        self.assertEqual(0.10, watch["items"][0]["minFloat"])
        self.assertEqual("观望", watch["items"][0]["inventoryAdviceLabel"])
        self.assertEqual(1, watch["summary"]["positiveOpportunityCount"])

        refresh_status, refresh = self._request(
            "POST",
            "/api/profit-trade/selection-watch/refresh",
            body={},
            runtime_controller=fake_runtime,
        )
        self.assertEqual(202, refresh_status)
        self.assertTrue(refresh["researchOnly"])
        self.assertFalse(refresh["canExecute"])
        self.assertEqual(1, fake_runtime.selection_refresh_calls)

        history_status, history = self._request(
            "GET",
            "/api/profit-trade/selection-watch/history?"
            + urlencode({"marketHashName": selection_name, "page": 1, "pageSize": 20}),
            runtime_controller=fake_runtime,
        )
        self.assertEqual(200, history_status)
        contract_row = next(
            item
            for item in history["items"]
            if item["scanId"] == "PTSEL-api-contract"
        )
        self.assertEqual(9.5, contract_row["steamOrderbook"]["buyerMaxPrice"])
        self.assertAlmostEqual(0.201, history["stats"]["highestRoi"])
        self.assertAlmostEqual(0.201, history["stats"]["averageRoi"])
        self.assertEqual(0.69, history["stats"]["roiBasis"])
        # The add audit event is retained too, but it has no price/ROI; only
        # the actual market observation belongs to the ROI aggregate.
        self.assertEqual(1, history["stats"]["validObservationCount"])
        self.assertEqual(1, history["trend"]["totalValidPoints"])
        self.assertAlmostEqual(0.201, history["trend"]["points"][0]["expectedRoi"])
        self.assertAlmostEqual(0.246, history["trend"]["points"][0]["buyOrderReferenceRoi"])

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            self.assertEqual(0, db.conn.execute("SELECT COUNT(*) FROM profit_trades").fetchone()[0])
            self.assertEqual(0, db.conn.execute("SELECT COUNT(*) FROM asset_reservations").fetchone()[0])
        finally:
            db.close()

    def test_dashboard_roi_watch_and_interruptions_expose_persistent_listings_circuit(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.set_profit_trade_runtime_state(
                "steam_search_listings",
                {
                    "status": "open",
                    "reason": "Steam listings HTTP 429 cooldown",
                    "first429At": "2026-07-16T05:00:00+00:00",
                    "last429At": "2026-07-16T05:01:00+00:00",
                    "cooldownUntil": "2099-07-16T05:11:00+00:00",
                    "nextProbeAt": "2099-07-16T05:11:00+00:00",
                    "consecutive429Count": 3,
                    "triggerAccountName": "xiaodigu11",
                },
            )
        finally:
            db.close()

        dashboard_status, dashboard = self._request("GET", "/api/profit-trade/dashboard")
        self.assertEqual(200, dashboard_status)
        self.assertEqual("open", dashboard["listingsCircuit"]["status"])
        self.assertTrue(dashboard["listingsCircuit"]["isBlocking"])
        self.assertEqual("xiaodigu11", dashboard["listingsCircuit"]["triggerAccountName"])

        watch_status, watch = self._request(
            "GET",
            "/api/profit-trade/roi-watch?active=true&page=1&pageSize=12&sort=roi_desc",
        )
        self.assertEqual(200, watch_status)
        self.assertEqual("open", watch["listingsCircuit"]["status"])

        interruptions_status, interruptions = self._request(
            "GET",
            "/api/profit-trade/interruptions?acknowledged=exclude&page=1&pageSize=20",
        )
        self.assertEqual(200, interruptions_status)
        self.assertEqual("open", interruptions["listingsCircuit"]["status"])

    def test_completed_endpoint_is_not_truncated_and_filters_by_steam_purchase_time(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            for index, (bought_at, profit) in enumerate(
                (
                    ("2026-07-11T01:00:00+00:00", 3.25),
                    ("2026-07-27T01:00:00+00:00", 4.75),
                ),
                start=1,
            ):
                db.add_profit_trade(
                    trade_no=f"PT-completed-range-{index}",
                    market_hash_name=MARKET_HASH_NAME,
                    status="completed",
                    step_key="settled",
                    step_index=6,
                    steam_buy_price=100.0,
                    steam_balance_discount=0.69,
                    steam_real_cost=69.0,
                    c5_sold_net_price=69.0 + profit,
                    realized_profit=profit,
                    note=json.dumps({"steamBuySucceededAt": bought_at}),
                )
            # These attempts would push both completed rows outside the bounded
            # operational dashboard list. They must not affect accounting.
            for index in range(101):
                db.add_profit_trade(
                    trade_no=f"PT-cancelled-noise-{index}",
                    market_hash_name=MARKET_HASH_NAME,
                    status="cancelled",
                    step_key="asset_locked",
                    step_index=2,
                )
        finally:
            db.close()

        status, payload = self._request("GET", "/api/profit-trade/completed")
        self.assertEqual(200, status)
        self.assertEqual(2, payload["summary"]["count"])
        self.assertEqual(8.0, payload["summary"]["realizedProfit"])
        self.assertEqual(200.0, payload["summary"]["steamBuyTotal"])
        self.assertEqual(
            ["PT-completed-range-2", "PT-completed-range-1"],
            [item["tradeNo"] for item in payload["items"]],
        )

        query = urlencode(
            {
                "boughtFrom": "2026-07-26T16:00:00+00:00",
                "boughtTo": "2026-07-27T15:59:59.999+00:00",
            }
        )
        status, payload = self._request("GET", f"/api/profit-trade/completed?{query}")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["summary"]["count"])
        self.assertEqual(4.75, payload["summary"]["realizedProfit"])
        self.assertEqual("PT-completed-range-2", payload["items"][0]["tradeNo"])

    def test_interruption_endpoint_searches_chinese_name_and_rejects_completed(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-interruption",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="Steam search_listings HTTP 429",
                note=(
                    '{"name":"USP消音版 | 椰风花语（崭新出厂）",'
                    '"cancelSource":"profit_trade_search_listings",'
                    '"cancelReason":"Steam HTTP 429"}'
                ),
            )
        finally:
            db.close()

        query = urlencode(
            {
                "keyword": "椰风花语",
                "status": "cancelled",
                "acknowledged": "exclude",
                "page": 1,
                "pageSize": 20,
            }
        )
        status, payload = self._request("GET", f"/api/profit-trade/interruptions?{query}")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["total"])
        self.assertEqual(trade_id, payload["items"][0]["id"])
        self.assertEqual("profit_trade_search_listings", payload["items"][0]["cancelSource"])
        self.assertEqual(payload["total"], payload["summary"]["total"])

        invalid_status, invalid = self._request(
            "GET",
            "/api/profit-trade/interruptions?status=completed",
        )
        self.assertEqual(400, invalid_status)
        self.assertIn("invalid", invalid["error"])

    def test_timeline_endpoint_returns_only_truthful_historical_snapshot(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-history",
                market_hash_name=MARKET_HASH_NAME,
                status="cancelled",
                step_key="asset_locked",
                step_index=2,
                error="HTTP 429 before Steam purchase",
            )
            db.conn.execute("DELETE FROM profit_trade_state_events WHERE trade_id = ?", (trade_id,))
            db.conn.commit()
        finally:
            db.close()

        status, payload = self._request(
            "GET",
            f"/api/profit-trade/interruptions/timeline?tradeId={trade_id}",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["events"]))
        event = payload["events"][0]
        self.assertTrue(event["isSnapshot"])
        self.assertEqual("historical_snapshot", event["eventType"])
        self.assertIsNone(event["statusFrom"])
        self.assertEqual("cancelled", event["statusTo"])

    def test_acknowledge_endpoint_never_hides_uncertain_order_silently(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-unsafe-ack",
                market_hash_name="Dreams & Nightmares Case",
                status="manual_required",
                step_key="steam_bought",
                step_index=3,
                note='{"steamBuyMethod":"createbuyorder","steamBuyOrderId":"buy-order-live"}',
            )
        finally:
            db.close()

        with patch.object(
            web_api,
            "dismiss_profit_trade",
            return_value={
                "ok": False,
                "changed": True,
                "dismissed": False,
                "message": "Steam buy completed; trade restored",
            },
        ) as dismiss:
            status, payload = self._request(
                "POST",
                "/api/profit-trade/interruptions/acknowledge",
                body={"tradeId": trade_id, "action": "acknowledge", "reason": "reviewed"},
            )
        self.assertEqual(409, status)
        self.assertFalse(payload["ok"])
        dismiss.assert_called_once()

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            acknowledgement = db.conn.execute(
                "SELECT * FROM profit_trade_acknowledgements WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        finally:
            db.close()
        self.assertIsNone(acknowledgement)

    def test_public_profit_trade_payloads_never_expose_authentication_material(self) -> None:
        secret_values = {
            "SECRET_TOKEN_SENTINEL",
            "SECRET_STYLE_TOKEN_SENTINEL",
            "SECRET_COOKIE_SENTINEL",
            "SECRET_SESSION_SENTINEL",
            "SECRET_API_KEY_SENTINEL",
            "SECRET_APP_KEY_SENTINEL",
            "SECRET_PASSWORD_SENTINEL",
            "SECRET_STEAM_GUARD_SENTINEL",
            "SECRET_IDENTITY_SENTINEL",
            "SECRET_DEVICE_SENTINEL",
            "SECRET_SHARED_SENTINEL",
            "SECRET_DEVICE_ID_SENTINEL",
            "https://steamcommunity.com/tradeoffer/new/?partner=123&token=SECRET_TRADE_URL_SENTINEL",
        }
        note = {
            "name": "USP消音版 | 椰风花语（崭新出厂）",
            "token": "SECRET_TOKEN_SENTINEL",
            "styleToken": "SECRET_STYLE_TOKEN_SENTINEL",
            "cookies": "SECRET_COOKIE_SENTINEL",
            "sessionid": "SECRET_SESSION_SENTINEL",
            "apiKey": "SECRET_API_KEY_SENTINEL",
            "app-key": "SECRET_APP_KEY_SENTINEL",
            "password": "SECRET_PASSWORD_SENTINEL",
            "steamGuardSecret": "SECRET_STEAM_GUARD_SENTINEL",
            "identity_secret": "SECRET_IDENTITY_SENTINEL",
            "device_secret": "SECRET_DEVICE_SENTINEL",
            "sharedSecret": "SECRET_SHARED_SENTINEL",
            "deviceId": "SECRET_DEVICE_ID_SENTINEL",
            "tradeUrl": "https://steamcommunity.com/tradeoffer/new/?partner=123&token=SECRET_TRADE_URL_SENTINEL",
            "request": {
                "headers": {
                    "Cookie": "sessionid=SECRET_SESSION_SENTINEL; steamLoginSecure=SECRET_COOKIE_SENTINEL"
                }
            },
            "cancelSource": "profit_trade_search_listings",
            "cancelReason": "Steam HTTP 429",
            "purchaseRequestSent": False,
            "walletBalanceBefore": 321.45,
            "walletBalanceAfter": 321.45,
            "walletDelta": 0.0,
        }
        error = (
            "request failed sessionid=SECRET_SESSION_SENTINEL "
            "api_key=SECRET_API_KEY_SENTINEL "
            "https://steamcommunity.com/tradeoffer/new/?partner=123&token=SECRET_TRADE_URL_SENTINEL"
        )
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-sensitive-payload",
                market_hash_name=MARKET_HASH_NAME,
                status="manual_required",
                step_key="asset_locked",
                step_index=2,
                error=error,
                note=json.dumps(note, ensure_ascii=False),
            )
            db.record_profit_trade_roi_scan(
                [
                    {
                        "market_hash_name": MARKET_HASH_NAME,
                        "name_cn": note["name"],
                        "steam_buy_price": 100.0,
                        "c5_listing_price": 75.0,
                        "c5_expected_net_price": 74.25,
                        "balance_discount": 0.69,
                        "expected_profit": 5.25,
                        "expected_roi": 0.0525,
                        "min_roi": 0.08,
                        "manual_review_roi": 0.20,
                        "inventory_count": 1,
                        "tradable_count": 1,
                        "risk_status": "passed",
                        "execution_status": "below_min_roi",
                        "execution_reason": "sessionid=SECRET_SESSION_SENTINEL",
                        "raw": {
                            "token": "SECRET_TOKEN_SENTINEL",
                            "styleToken": "SECRET_STYLE_TOKEN_SENTINEL",
                        },
                    }
                ],
                scan_id="PTSCAN-sensitive-payload",
            )
        finally:
            db.close()

        responses: list[dict] = []
        for path in (
            "/api/profit-trade/dashboard",
            "/api/profit-trade/interruptions?status=manual_required&acknowledged=include",
            f"/api/profit-trade/interruptions/timeline?tradeId={trade_id}",
            "/api/profit-trade/roi-watch?active=true",
            f"/api/profit-trade/roi-watch/history?{urlencode({'marketHashName': MARKET_HASH_NAME})}",
        ):
            status, payload = self._request("GET", path)
            self.assertEqual(200, status, path)
            responses.append(payload)

        serialized = json.dumps(responses, ensure_ascii=False)
        for secret in secret_values:
            self.assertNotIn(secret, serialized)
        for sensitive_key in (
            '"token"',
            '"styleToken"',
            '"cookies"',
            '"sessionid"',
            '"apiKey"',
            '"app-key"',
            '"password"',
            '"steamGuardSecret"',
            '"identity_secret"',
            '"device_secret"',
            '"sharedSecret"',
            '"deviceId"',
            '"tradeUrl"',
        ):
            self.assertNotIn(sensitive_key, serialized)

        dashboard_trade = responses[0]["trades"][0]
        self.assertEqual("profit_trade_search_listings", dashboard_trade["cancelSource"])
        self.assertEqual("Steam HTTP 429", dashboard_trade["cancelReason"])
        self.assertFalse(dashboard_trade["purchaseRequestSent"])
        self.assertEqual(321.45, dashboard_trade["note"]["walletBalanceBefore"])
        self.assertEqual(321.45, dashboard_trade["note"]["walletBalanceAfter"])
        self.assertEqual(0.0, dashboard_trade["note"]["walletDelta"])

        db = Database(self.settings.db_path)
        try:
            db.initialize()
            stored = db.get_profit_trade(trade_id)
            stored_note = json.loads(str(stored["note"]))
        finally:
            db.close()
        self.assertEqual("SECRET_TOKEN_SENTINEL", stored_note["token"])
        self.assertEqual("SECRET_STYLE_TOKEN_SENTINEL", stored_note["styleToken"])

    def test_manual_record_create_update_and_safe_account_options(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.conn.execute(
                """
                INSERT INTO items (
                    market_hash_name, name_cn, raw_json, imported_at, updated_at
                ) VALUES (?, ?, '{}', ?, ?)
                """,
                (
                    "M4A4 | Temukau (Field-Tested)",
                    "M4A4 | 反冲精英（略有磨损）",
                    "2026-07-14T00:00:00+00:00",
                    "2026-07-14T00:00:00+00:00",
                ),
            )
            db.conn.commit()
        finally:
            db.close()
        search_status, search = self._request(
            "GET",
            f"/api/profit-trade/items/search?{urlencode({'query': '反冲', 'limit': 20})}",
        )
        self.assertEqual(200, search_status)
        self.assertEqual("M4A4 | Temukau (Field-Tested)", search["items"][0]["marketHashName"])
        self.assertEqual(1, search["pagination"]["total"])
        self.assertFalse(search["pagination"]["hasMore"])
        self.assertIsNone(search["pagination"]["nextOffset"])

        create_status, created = self._request(
            "POST",
            "/api/profit-trade/manual-record/create",
            body={
                "marketHashName": "Dreams & Nightmares Case",
                "name": "梦魇武器箱",
                "steamAccountId": None,
                "steamBuyPrice": 10.0,
                "balanceDiscount": 0.69,
                "c5SoldNetPrice": 8.0,
                "steamBoughtAt": "2026-07-14T10:00:00+08:00",
                "completedAt": "2026-07-14T11:00:00+08:00",
                "aAssetId": "asset-a",
                "bAssetId": "asset-b",
                "memo": "manual API test",
            },
        )
        self.assertEqual(200, create_status)
        self.assertTrue(created["ok"])
        self.assertEqual("manual_backfill", created["trade"]["recordOrigin"])
        trade_id = created["trade"]["id"]

        update_status, updated = self._request(
            "POST",
            "/api/profit-trade/manual-record/update",
            body={
                "tradeId": trade_id,
                "marketHashName": "Dreams & Nightmares Case",
                "name": "梦魇武器箱",
                "steamAccountId": None,
                "steamBuyPrice": 11.0,
                "balanceDiscount": 0.68,
                "c5SoldNetPrice": 9.0,
                "steamBoughtAt": "2026-07-13T10:00:00+08:00",
                "completedAt": "2026-07-13T11:00:00+08:00",
                "aAssetId": "asset-a",
                "bAssetId": "asset-b",
                "memo": "corrected API test",
            },
        )
        self.assertEqual(200, update_status)
        self.assertTrue(updated["trade"]["manuallyEdited"])
        self.assertAlmostEqual(7.48, updated["trade"]["steamRealCost"])
        self.assertEqual("2026-07-13T02:00:00+00:00", updated["trade"]["steamBoughtAt"])

        dashboard_status, dashboard = self._request("GET", "/api/profit-trade/dashboard")
        self.assertEqual(200, dashboard_status)
        self.assertEqual(trade_id, dashboard["trades"][0]["id"])
        for account in dashboard["manualEntryOptions"]["accounts"]:
            self.assertEqual({"accountId", "name", "steamId"}, set(account))

    def test_manual_record_update_rejects_non_completed_trade(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            trade_id = db.add_profit_trade(
                trade_no="PT-api-not-completed",
                market_hash_name="Dreams & Nightmares Case",
                status="candidate",
            )
        finally:
            db.close()
        status, payload = self._request(
            "POST",
            "/api/profit-trade/manual-record/update",
            body={
                "tradeId": trade_id,
                "marketHashName": "Dreams & Nightmares Case",
                "steamBuyPrice": 10.0,
                "balanceDiscount": 0.69,
                "c5SoldNetPrice": 8.0,
                "steamBoughtAt": "2026-07-14T10:00:00+08:00",
                "completedAt": "2026-07-14T11:00:00+08:00",
            },
        )
        self.assertEqual(409, status)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
