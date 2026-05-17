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

    def goods_search(
        self,
        *,
        app_id: int,
        market_hash_name: str,
        delivery: int = 1,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """搜索市场在售商品，返回 productId 和价格列表。delivery=1 只看自动发货。"""
        params: dict[str, Any] = {
            "appId": app_id,
            "marketHashName": market_hash_name,
            "delivery": delivery,
            "page": page,
            "limit": limit,
        }
        data = self._request("GET", "/merchant/goods/v1/search", params=params)
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
