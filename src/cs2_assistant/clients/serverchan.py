from __future__ import annotations

import requests


class ServerChanError(RuntimeError):
    pass


class ServerChanClient:
    def __init__(self, sendkey: str, base_url: str = "https://sctapi.ftqq.com", timeout: int = 30):
        self.sendkey = sendkey
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def send(self, title: str, desp: str | None = None) -> dict:
        url = f"{self.base_url}/{self.sendkey}.send"
        try:
            response = requests.post(
                url,
                data={"title": title, "desp": desp or ""},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ServerChanError(f"ServerChan request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ServerChanError(f"ServerChan returned invalid JSON: {response.text}") from exc

        code = payload.get("code")
        if code not in (0, None):
            raise ServerChanError(str(payload))
        return payload
