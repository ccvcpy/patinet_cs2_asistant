from __future__ import annotations

import json
from typing import Any

import requests


class SteamDTError(RuntimeError):
    pass


class SteamDTClient:
    def __init__(self, api_key: str, base_url: str = "https://open.steamdt.com", timeout: int = 30):
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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
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
            raise SteamDTError(f"SteamDT request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamDTError(f"SteamDT returned invalid JSON: {response.text}") from exc

        if payload.get("success") is not True:
            raise SteamDTError(json.dumps(payload, ensure_ascii=False))
        return payload.get("data")

    def base(self) -> Any:
        return self._request("GET", "/open/cs2/v1/base")

    def price_single(self, market_hash_name: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/open/cs2/v1/price/single",
            params={"marketHashName": market_hash_name},
        )
        return list(data or [])

    def price_batch(self, market_hash_names: list[str]) -> list[dict[str, Any]]:
        if not market_hash_names:
            return []
        data = self._request(
            "POST",
            "/open/cs2/v1/price/batch",
            json_body={"marketHashNames": market_hash_names},
        )
        return list(data or [])

    def price_avg(self, market_hash_name: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            "/open/cs2/v1/price/avg",
            params={"marketHashName": market_hash_name},
        )
        return dict(data or {})

