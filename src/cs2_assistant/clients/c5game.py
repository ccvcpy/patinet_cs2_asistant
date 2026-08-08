from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable, Mapping

import requests


class C5GameError(RuntimeError):
    pass


C5TelemetryCallback = Callable[[dict[str, Any]], None]
C5RequestGuard = Callable[[], bool]

_C5_TELEMETRY_CONTEXT_FIELDS = {
    "source",
    "run_id",
    "trade_id",
    "trade_no",
    "market_hash_name",
    "asset_id",
    "account_id",
    "steam_id64",
}
_C5_SENSITIVE_ERROR_RE = re.compile(
    r"(?i)(app[-_ ]?key|api[-_ ]?key|authorization|cookie|sessionid|password|"
    r"identity[-_ ]?secret|device[-_ ]?secret|shared[-_ ]?secret|steam[-_ ]?guard|"
    r"style[-_ ]?token|access[-_ ]?token|refresh[-_ ]?token|token|trade[-_ ]?url)"
    r"(\s*[=:]\s*)([^&;\s,}\]]+|\"[^\"]*\"|'[^']*')"
)
_C5_TRADE_URL_RE = re.compile(
    r"https?://steamcommunity\.com/tradeoffer/new/\?[^\s\"']+",
    re.IGNORECASE,
)
_C5_IPV4_RE = re.compile(
    r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\."
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?![\d.])"
)


def _redact_app_key(text: str) -> str:
    return re.sub(r"(app-key=)[^&\s]+", r"\1<redacted>", text)


def _safe_c5_telemetry_error(exc: BaseException) -> str:
    text = _C5_TRADE_URL_RE.sub("<redacted:trade_url>", str(exc))
    text = _C5_SENSITIVE_ERROR_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    return text if len(text) <= 1000 else f"{text[:1000]}...<truncated>"


def _c5_business_error_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return only non-secret fields needed for shared runtime protection."""

    error_code = payload.get("errorCode")
    error_message = str(payload.get("errorMsg") or payload.get("message") or "")
    ip_match = _C5_IPV4_RE.search(error_message)
    return {
        "error_code": error_code,
        "request_ip": ip_match.group(1) if ip_match else None,
    }


class C5GameClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openapi.c5game.com",
        timeout: int = 30,
        *,
        telemetry_callback: C5TelemetryCallback | None = None,
        telemetry_context: Mapping[str, Any] | None = None,
        request_guard: C5RequestGuard | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._telemetry_callback = telemetry_callback
        self._request_guard = request_guard
        self._telemetry_context = {
            key: value
            for key, value in dict(telemetry_context or {}).items()
            if key in _C5_TELEMETRY_CONTEXT_FIELDS
        }
        self._telemetry_client_instance_id = f"c5_{uuid.uuid4().hex}"

    @staticmethod
    def _telemetry_operation(path: str) -> str:
        normalized = str(path or "").lower()
        mappings = (
            ("/account/v1/steaminfo", "steam_info"),
            ("/inventory/", "inventory"),
            ("/product/price/batch", "price_batch"),
            ("/item/stat/hash/name", "price_statistics_batch"),
            ("/purchase/v1/max-price", "purchase_max_price"),
            ("/sale/v1/search", "sale_search"),
            ("/sale/v1/modify", "sale_modify"),
            ("/sale/v2/create", "sale_create"),
            ("/sale/v1/cancel", "sale_cancel"),
            ("/trade/v2/normal-buy", "normal_buy"),
            ("/trade/v2/quick-buy", "quick_buy"),
            ("/market/v2/products/search", "market_products_search"),
            ("/market/v2/products/list", "market_products_list"),
            ("/trade/v1/batch/buy", "batch_buy"),
            ("/order/v2/buyer/status", "buyer_order_status"),
            ("/order/v2/buy/detail", "buyer_order_detail"),
            ("/order/v1/list", "seller_order_list"),
            ("/order/v1/detail", "seller_order_detail"),
        )
        for marker, operation in mappings:
            if marker in normalized:
                return operation
        return "c5_http_request"

    def _emit_telemetry(
        self,
        *,
        level: str,
        operation: str,
        message: str,
        **fields: Any,
    ) -> None:
        callback = self._telemetry_callback
        if callback is None:
            return
        event = dict(self._telemetry_context)
        event.update(
            {
                "level": level,
                "provider": "c5",
                "component": "c5game",
                "operation": operation,
                "message": message,
                "client_instance_id": self._telemetry_client_instance_id,
            }
        )
        event.update({key: value for key, value in fields.items() if value is not None})
        try:
            callback(event)
        except Exception:
            # Telemetry is diagnostic only and must never change a C5 action.
            return

    def _request_with_telemetry(
        self,
        *,
        method: str,
        path: str,
        request: Callable[[], requests.Response],
    ) -> requests.Response:
        request_id = f"c5_req_{uuid.uuid4().hex}"
        operation = self._telemetry_operation(path)
        started = time.perf_counter()
        self._emit_telemetry(
            level="DEBUG",
            operation=operation,
            message="C5 request started",
            request_id=request_id,
            attempt=1,
            method=str(method).upper(),
            endpoint=path,
            safe_context={"phase": "start"},
        )
        try:
            response = request()
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            self._emit_telemetry(
                level="ERROR",
                operation=operation,
                message="C5 request failed before receiving a response",
                request_id=request_id,
                attempt=1,
                method=str(method).upper(),
                endpoint=path,
                elapsed_ms=elapsed_ms,
                exception_type=type(exc).__name__,
                safe_context={
                    "phase": "failure",
                    "error": _safe_c5_telemetry_error(exc),
                },
            )
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        status_code = int(getattr(response, "status_code", 0) or 0)
        response_headers = getattr(response, "headers", {}) or {}
        retry_after = response_headers.get("Retry-After") if hasattr(response_headers, "get") else None
        failed = status_code >= 400
        self._emit_telemetry(
            level="ERROR" if failed else "INFO",
            operation=operation,
            message="C5 request returned an HTTP error" if failed else "C5 request succeeded",
            request_id=request_id,
            attempt=1,
            method=str(method).upper(),
            endpoint=path,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            retry_after=retry_after,
            safe_context={"phase": "failure" if failed else "success"},
        )
        try:
            setattr(response, "_profit_trade_telemetry_request_id", request_id)
        except Exception:
            pass
        return response

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if self._request_guard is not None and not self._request_guard():
            raise C5GameError("C5 request blocked: shared IP whitelist circuit is open")
        merged_params = dict(params or {})
        merged_params["app-key"] = self.api_key
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            response = self._request_with_telemetry(
                method=method,
                path=path,
                request=lambda: requests.request(
                    method=method,
                    url=f"{self.base_url}{path}",
                    params=merged_params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                ),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise C5GameError(f"C5 request failed: {_redact_app_key(str(exc))}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            self._emit_telemetry(
                level="ERROR",
                operation=self._telemetry_operation(path),
                message="C5 response was not valid JSON",
                request_id=getattr(response, "_profit_trade_telemetry_request_id", None),
                attempt=1,
                method=str(method).upper(),
                endpoint=path,
                status_code=int(getattr(response, "status_code", 0) or 0),
                exception_type=type(exc).__name__,
                safe_context={"phase": "response_parse", "error": _safe_c5_telemetry_error(exc)},
            )
            raise C5GameError(f"C5 returned invalid JSON: {response.text}") from exc

        if payload.get("success") is not True:
            business_metadata = _c5_business_error_metadata(payload)
            safe_message = _safe_c5_telemetry_error(
                RuntimeError(
                    str(
                        payload.get("errorMsg")
                        or payload.get("message")
                        or payload.get("error")
                        or "C5 business request failed"
                    )
                )
            )
            self._emit_telemetry(
                level="ERROR",
                operation=self._telemetry_operation(path),
                message="C5 response reported a business failure",
                request_id=getattr(response, "_profit_trade_telemetry_request_id", None),
                attempt=1,
                method=str(method).upper(),
                endpoint=path,
                status_code=int(getattr(response, "status_code", 0) or 0),
                error_code=business_metadata["error_code"],
                request_ip=business_metadata["request_ip"],
                safe_context={
                    "phase": "business_failure",
                    "error": safe_message,
                    **business_metadata,
                },
            )
            raise C5GameError(json.dumps(payload, ensure_ascii=False))
        return payload.get("data")

    def steam_info(self) -> dict[str, Any]:
        data = self._request("GET", "/merchant/account/v1/steamInfo")
        return dict(data or {})

    def account_balance(self) -> dict[str, Any]:
        info = self.steam_info()
        return {
            "balance": info.get("balance"),
            "uid": info.get("uid"),
            "nickname": info.get("nickname"),
        }

    def inventory(self, steam_id: str, app_id: int = 730) -> dict[str, Any]:
        data = self._request("GET", f"/merchant/inventory/v2/{steam_id}/{app_id}")
        return dict(data or {})

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, Any]:
        if not market_hash_names:
            return {}
        data = self._request(
            "POST",
            "/merchant/product/price/batch",
            json_body={"appId": str(app_id), "marketHashNames": market_hash_names},
        )
        return dict(data or {})

    def price_statistics_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, Any]:
        """Batch query C5 item statistics used for liquidity/risk checks."""
        if not market_hash_names:
            return {}
        data = self._request(
            "POST",
            "/merchant/market/v2/item/stat/hash/name",
            json_body={"appId": str(app_id), "marketHashNames": market_hash_names},
        )
        return dict(data or {})

    def purchase_max_price(self, market_hash_name: str, app_id: int = 730) -> dict[str, Any]:
        data = self._request(
            "GET",
            "/merchant/purchase/v1/max-price",
            params={"appId": app_id, "marketHashName": market_hash_name},
        )
        return dict(data or {})

    def sale_search(
        self,
        *,
        app_id: int,
        steam_id: str | None = None,
        delivery: int | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"appId": app_id, "page": page, "limit": limit}
        if steam_id:
            params["steamId"] = steam_id
        if delivery is not None:
            params["delivery"] = delivery
        data = self._request("GET", "/merchant/sale/v1/search", params=params)
        return dict(data or {})

    def sale_modify(self, *, app_id: int, data_list: list[dict[str, Any]]) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/merchant/sale/v1/modify",
            json_body={"appId": app_id, "dataList": data_list},
        )
        return dict(data or {})

    def sale_create(self, *, app_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            raise C5GameError("items is required")

        data_list: list[dict[str, Any]] = []
        for item in items:
            asset_id = str(item.get("assetId") or "").strip()
            market_hash_name = str(item.get("marketHashName") or "").strip()
            token = str(item.get("token") or "").strip()
            style_token = str(item.get("styleToken") or item.get("style_token") or "").strip()
            price = item.get("price")
            if not asset_id:
                raise C5GameError("sale_create requires assetId for each item")
            if not market_hash_name:
                raise C5GameError("sale_create requires marketHashName for each item")
            if not token:
                raise C5GameError("sale_create requires token for each item")
            if not style_token:
                raise C5GameError("sale_create requires styleToken for each item")
            if price is None:
                raise C5GameError("sale_create requires price for each item")
            data_list.append(
                {
                    "price": float(price),
                    "token": token,
                    "styleToken": style_token,
                }
            )

        data = self._request(
            "POST",
            "/merchant/sale/v2/create",
            json_body={"appId": app_id, "dataList": data_list},
        )
        return dict(data or {})

    def sale_cancel(self, *, app_id: int, product_ids: list[int]) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/merchant/sale/v1/cancel",
            json_body={"appId": app_id, "productIds": product_ids},
        )
        return dict(data or {})

    def normal_buy(
        self,
        *,
        app_id: int,
        product_id: int,
        buy_price: float,
        trade_url: str,
        out_trade_no: str | None = None,
    ) -> dict[str, Any]:
        """普通购买：按指定 productId 和价格购买。"""
        import uuid
        body: dict[str, Any] = {
            "appId": app_id,
            "productId": product_id,
            "buyPrice": buy_price,
            "tradeUrl": trade_url,
            "outTradeNo": out_trade_no or uuid.uuid4().hex,
        }
        data = self._request("POST", "/merchant/trade/v2/normal-buy", json_body=body)
        return dict(data or {})

    def quick_buy(
        self,
        *,
        app_id: int,
        market_hash_name: str | None = None,
        item_id: str | None = None,
        max_price: float | None = None,
        delivery: int | None = None,
        low_price: float | None = None,
        out_trade_no: str | None = None,
        device: int = 0,
        trade_url: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"appId": app_id, "device": device}
        if out_trade_no is not None:
            body["outTradeNo"] = out_trade_no
        if trade_url is not None:
            body["tradeUrl"] = trade_url
        if item_id is not None:
            body["itemId"] = item_id
        if market_hash_name is not None:
            body["marketHashName"] = market_hash_name
        if max_price is not None:
            body["maxPrice"] = max_price
        if delivery is not None:
            body["delivery"] = delivery
        if low_price is not None:
            body["lowPrice"] = low_price
        data = self._request("POST", "/merchant/trade/v2/quick-buy", json_body=body)
        return dict(data or {})

    def market_products_search(
        self,
        *,
        item_id: str,
        page_size: int,
    ) -> dict[str, Any]:
        """Read one bounded concrete-listing snapshot for a C5 item.

        This internal-preview endpoint can diverge from ``price_batch`` and
        must not be treated as a complete-market price authority.  Keep the
        request minimal: adding delivery/bargain or name/price filters can
        narrow the visible listing set further.
        """
        data = self._request(
            "POST",
            "/merchant/market/v2/products/search",
            json_body={
                "itemId": item_id,
                "pageSize": page_size,
            },
        )
        return dict(data or {})

    def market_products_list(
        self,
        *,
        item_id: str,
        delivery: int,
        page_num: int,
        page_size: int,
    ) -> dict[str, Any]:
        """Read one concrete page of C5 listings for a catalog item."""
        data = self._request(
            "POST",
            "/merchant/market/v2/products/list",
            json_body={
                "itemId": item_id,
                "delivery": delivery,
                "pageNum": page_num,
                "pageSize": page_size,
            },
        )
        return dict(data or {})

    def batch_buy(
        self,
        *,
        product_list: list[dict[str, Any]],
        trade_url: str,
    ) -> dict[str, Any]:
        """Submit multiple concrete C5 listings in one purchase request."""
        data = self._request(
            "POST",
            "/merchant/trade/v1/batch/buy",
            json_body={"productList": product_list, "tradeUrl": trade_url},
        )
        return dict(data or {})

    def buyer_order_status(
        self,
        *,
        page_num: int = 1,
        page_size: int = 100,
        status: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"pageNum": page_num, "pageSize": page_size}
        if status is not None:
            body["status"] = status
        data = self._request("POST", "/merchant/order/v2/buyer/status", json_body=body)
        return dict(data or {})

    def buyer_order_detail(self, order_id: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            "/merchant/order/v2/buy/detail",
            params={"orderId": order_id},
        )
        return dict(data or {})

    def seller_order_list(
        self,
        *,
        app_id: int,
        steam_id: str | None = None,
        status: int | None = None,
        page: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"appId": app_id, "page": page, "limit": limit}
        if steam_id:
            params["steamId"] = steam_id
        if status is not None:
            params["status"] = status
        data = self._request("GET", "/merchant/order/v1/list", params=params)
        return dict(data or {})

    def seller_order_detail(self, order_id: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            "/merchant/order/v1/detail",
            params={"orderId": order_id},
        )
        return dict(data or {})
