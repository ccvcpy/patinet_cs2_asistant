from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from cs2_assistant.config import Settings
from cs2_assistant.services.profit_trade import (
    build_profit_trade_dashboard_payload,
    dismiss_profit_trade,
    execute_profit_trade_buy,
    execute_profit_trade_list_c5,
    lock_profit_trade,
    manual_settle_profit_trade,
    refresh_profit_trade_sales,
    run_profit_trade_once,
    scan_profit_trade_opportunities,
    send_profit_trade_daily_report,
    set_profit_trade_config,
    update_profit_trade_protection,
)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def run_profit_trade_api_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CS2AssistantProfitTradeAPI/0.1"

        def _send_json(self, status: int, payload: Any) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send_json(HTTPStatus.NO_CONTENT, {})

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/profit-trade/dashboard":
                self._send_json(HTTPStatus.OK, build_profit_trade_dashboard_payload(settings))
                return
            if path == "/api/profit-trade/config":
                self._send_json(
                    HTTPStatus.OK,
                    build_profit_trade_dashboard_payload(settings, limit=0)["config"],
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/profit-trade/config":
                body = self._read_json_body()
                if (
                    "enabled" not in body
                    and "allowRealExecution" not in body
                    and "allowRepriceExecution" not in body
                    and "stickerSlabStatus" not in body
                    and "stickerStatus" not in body
                    and "dailySteamBudget" not in body
                ):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "enabled, allowRealExecution, allowRepriceExecution, stickerSlabStatus, stickerStatus or dailySteamBudget is required"},
                    )
                    return
                config = set_profit_trade_config(
                    settings,
                    enabled=bool(body["enabled"]) if "enabled" in body else None,
                    allow_real_execution=bool(body["allowRealExecution"])
                    if "allowRealExecution" in body
                    else None,
                    allow_reprice_execution=bool(body["allowRepriceExecution"])
                    if "allowRepriceExecution" in body
                    else None,
                    sticker_slab_status=str(body["stickerSlabStatus"])
                    if "stickerSlabStatus" in body
                    else None,
                    sticker_status=str(body["stickerStatus"])
                    if "stickerStatus" in body
                    else None,
                    daily_steam_budget=float(body["dailySteamBudget"])
                    if "dailySteamBudget" in body
                    else None,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "enabled": config.profit_trade_enabled,
                        "allowRealExecution": config.profit_trade_allow_real_execution,
                        "allowRepriceExecution": config.profit_trade_allow_reprice_execution,
                        "config": build_profit_trade_dashboard_payload(settings, limit=0)["config"],
                    },
                )
                return
            if path == "/api/profit-trade/serverchan/daily-report":
                try:
                    send_profit_trade_daily_report(settings)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/profit-trade/protection":
                body = self._read_json_body()
                try:
                    config = update_profit_trade_protection(
                        settings,
                        action=str(body.get("action") or ""),
                        kind=str(body.get("kind") or ""),
                        value=str(body.get("value") or ""),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "config": build_profit_trade_dashboard_payload(
                            settings,
                            config=config,
                            limit=0,
                        )["config"],
                    },
                )
                return
            if path == "/api/profit-trade/scan":
                body = self._read_json_body()
                try:
                    report = scan_profit_trade_opportunities(
                        settings,
                        limit=int(body.get("limit") or 20),
                        scan_max_items=int(body["scanMaxItems"]) if body.get("scanMaxItems") is not None else None,
                        record=bool(body.get("record")),
                        lock_asset=bool(body.get("lock")),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "report": report.to_dict()})
                return
            if path == "/api/profit-trade/run-once":
                body = self._read_json_body()
                try:
                    report = run_profit_trade_once(
                        settings,
                        scan_max_items=int(body["scanMaxItems"]) if body.get("scanMaxItems") is not None else None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "report": report.to_dict()})
                return
            if path == "/api/profit-trade/refresh-sales":
                try:
                    result = refresh_profit_trade_sales(settings)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/dismiss":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = dismiss_profit_trade(
                        settings,
                        int(trade_id),
                        reason=str(body.get("reason") or "user dismissed from dashboard"),
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/manual-settle":
                body = self._read_json_body()
                try:
                    result = manual_settle_profit_trade(
                        settings,
                        int(body.get("tradeId") or 0),
                        sold_net_price=float(body.get("soldNetPrice") or 0),
                        source=str(body.get("source") or "manual_other_platform"),
                        memo=str(body.get("memo") or "") or None,
                    )
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/lock":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = lock_profit_trade(settings, int(trade_id))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/buy":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = execute_profit_trade_buy(settings, int(trade_id))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/profit-trade/list-c5":
                body = self._read_json_body()
                trade_id = body.get("tradeId") or body.get("trade_id")
                if trade_id is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tradeId is required"})
                    return
                try:
                    result = execute_profit_trade_list_c5(settings, int(trade_id))
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"profitTrade API listening on http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
