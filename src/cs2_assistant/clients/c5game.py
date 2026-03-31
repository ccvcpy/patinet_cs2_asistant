from __future__ import annotations

import json
from typing import Any

import requests


class C5GameError(RuntimeError):
    pass


class C5GameClient:
    def __init__(self, api_key: str, base_url: str = "https://openapi.c5game.com", timeout: int = 30):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        merged_params = dict(params or {})
        merged_params["app-key"] = self.api_key
        headers = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            response = requests.request(
                method=method,
                url=f"{self.base_url}{path}",
                params=merged_params,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise C5GameError(f"C5 request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise C5GameError(f"C5 returned invalid JSON: {response.text}") from exc

        if payload.get("success") is not True:
            raise C5GameError(json.dumps(payload, ensure_ascii=False))
        return payload.get("data")

    def steam_info(self) -> dict[str, Any]:
        data = self._request("GET", "/merchant/account/v1/steamInfo")
        return dict(data or {})

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

    def sale_cancel(self, *, app_id: int, product_ids: list[int]) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/merchant/sale/v1/cancel",
            json_body={"appId": app_id, "productIds": product_ids},
        )
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
