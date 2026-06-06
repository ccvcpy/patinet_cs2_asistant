from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NETWORK_CONFIG_PATH = PROJECT_ROOT / "config" / "network.json"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "assistant.db"
DEFAULT_STEAMDT_BASE_PATH = PROJECT_ROOT / "饰品数据" / "steamdt_cs2_base.json"
DEFAULT_SERVERCHAN_BASE_URL = "https://sctapi.ftqq.com"
DEFAULT_C5_BASE_URL = "https://openapi.c5game.com"
DEFAULT_STEAMDT_BASE_URL = "https://open.steamdt.com"
DEFAULT_CSQAQ_BASE_URL = "https://api.csqaq.com"
DEFAULT_STEAM_MARKET_BASE_URL = "https://steamcommunity.com"
DEFAULT_CSGO_APP_ID = 730
_TRUSTSTORE_INJECTED = False
_NO_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_PROXY_MODE_ALIASES = {
    "system": "system",
    "env": "system",
    "proxy": "system",
    "on": "system",
    "true": "system",
    "1": "system",
    "none": "none",
    "direct": "none",
    "off": "none",
    "false": "none",
    "0": "none",
    "no": "none",
}


def _read_windows_registry_env(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None

    locations = [
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]
    for hive, subkey in locations:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if value:
            return str(value)
    return None


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if not value:
            value = _read_windows_registry_env(name)
        if value:
            return value
    return None


def normalize_proxy_mode(value: str | None) -> str:
    raw = (value or "system").strip().lower()
    mode = _PROXY_MODE_ALIASES.get(raw)
    if mode is None:
        raise ValueError("无效代理模式，请使用 system 或 none")
    return mode


def load_network_proxy_mode() -> str:
    env_value = _first_env("CS2_ASSISTANT_PROXY_MODE", "CS2_PROXY_MODE")
    if env_value:
        return normalize_proxy_mode(env_value)

    if not NETWORK_CONFIG_PATH.exists():
        return "system"
    try:
        payload = json.loads(NETWORK_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "system"
    if not isinstance(payload, dict):
        return "system"
    return normalize_proxy_mode(str(payload.get("proxyMode") or "system"))


def save_network_proxy_mode(mode: str) -> Path:
    normalized = normalize_proxy_mode(mode)
    NETWORK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    NETWORK_CONFIG_PATH.write_text(
        json.dumps({"proxyMode": normalized}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return NETWORK_CONFIG_PATH


def apply_proxy_mode(mode: str) -> str:
    normalized = normalize_proxy_mode(mode)
    if normalized == "none":
        for key in _NO_PROXY_KEYS:
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
    return normalized


def ensure_system_cert_store() -> None:
    """Prefer the OS certificate store for HTTPS requests when available."""
    global _TRUSTSTORE_INJECTED
    if _TRUSTSTORE_INJECTED:
        return
    try:
        import truststore
    except ImportError:
        return
    truststore.inject_into_ssl()
    _TRUSTSTORE_INJECTED = True


@dataclass(slots=True)
class Settings:
    db_path: Path = DEFAULT_DB_PATH
    steamdt_base_path: Path = DEFAULT_STEAMDT_BASE_PATH
    steamdt_api_key: str | None = None
    c5_api_key: str | None = None
    csqaq_api_token: str | None = None
    serverchan_sendkey: str | None = None
    serverchan_base_url: str = DEFAULT_SERVERCHAN_BASE_URL
    c5_base_url: str = DEFAULT_C5_BASE_URL
    steamdt_base_url: str = DEFAULT_STEAMDT_BASE_URL
    csqaq_base_url: str = DEFAULT_CSQAQ_BASE_URL
    steam_market_base_url: str = DEFAULT_STEAM_MARKET_BASE_URL
    app_id: int = DEFAULT_CSGO_APP_ID
    steam_cookies: str | None = None
    steam_identity_secret: str | None = None
    steam_device_id: str | None = None
    network_proxy_mode: str = "system"


def load_settings() -> Settings:
    ensure_system_cert_store()
    network_proxy_mode = apply_proxy_mode(load_network_proxy_mode())
    db_path = Path(_first_env("CS2_ASSISTANT_DB_PATH") or DEFAULT_DB_PATH)
    steamdt_base_path = Path(
        _first_env("CS2_ASSISTANT_STEAMDT_BASE_PATH") or DEFAULT_STEAMDT_BASE_PATH
    )
    return Settings(
        db_path=db_path,
        steamdt_base_path=steamdt_base_path,
        steamdt_api_key=_first_env("STEAMDT_API_KEY"),
        c5_api_key=_first_env("C5GAME_API_KEY", "C5_API_KEY"),
        csqaq_api_token=_first_env("CSQAQ_API_KEY", "CSQAQ_API_TOKEN"),
        serverchan_sendkey=_first_env(
            "SERVERCHAN_SENDKEY",
            "SCTKEY",
            "SERVER_CHAN_TURBO_SENDKEY",
        ),
        serverchan_base_url=_first_env("SERVERCHAN_BASE_URL") or DEFAULT_SERVERCHAN_BASE_URL,
        c5_base_url=_first_env("C5GAME_BASE_URL") or DEFAULT_C5_BASE_URL,
        steamdt_base_url=_first_env("STEAMDT_BASE_URL") or DEFAULT_STEAMDT_BASE_URL,
        csqaq_base_url=_first_env("CSQAQ_BASE_URL") or DEFAULT_CSQAQ_BASE_URL,
        steam_market_base_url=_first_env("STEAM_MARKET_BASE_URL") or DEFAULT_STEAM_MARKET_BASE_URL,
        steam_cookies=_first_env("STEAM_COOKIES"),
        steam_identity_secret=_first_env("STEAM_IDENTITY_SECRET"),
        steam_device_id=_first_env("STEAM_DEVICE_ID"),
        network_proxy_mode=network_proxy_mode,
    )
