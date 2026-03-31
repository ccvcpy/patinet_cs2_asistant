from __future__ import annotations

import json
from typing import Any

import requests


class CSQAQError(RuntimeError):
    pass


class CSQAQClient:
    def __init__(self, api_token: str, base_url: str = "https://api.csqaq.com", timeout: int = 30):
        self.api_token = api_token
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
        headers = {
            "ApiToken": self.api_token,
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            response = requests.request(
                method=method,
                url=f"{self.base_url}{path}",
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CSQAQError(f"CSQAQ request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise CSQAQError(f"CSQAQ returned invalid JSON: {response.text}") from exc

        if not isinstance(payload, dict):
            return payload

        code = payload.get("code")
        if code not in (200, None):
            raise CSQAQError(json.dumps(payload, ensure_ascii=False))
        return payload.get("data")

    def bind_local_ip(self) -> str:
        data = self._request("POST", "/api/v1/sys/bind_local_ip")
        return str(data or "")

    def price_by_market_hash_names(self, market_hash_names: list[str]) -> dict[str, dict[str, Any]]:
        if not market_hash_names:
            return {}
        data = self._request(
            "POST",
            "/api/v1/goods/getPriceByMarketHashName",
            json_body={"marketHashNameList": market_hash_names},
        )
        if not isinstance(data, dict):
            return {}
        success_rows = data.get("success") or {}
        return dict(success_rows if isinstance(success_rows, dict) else {})
