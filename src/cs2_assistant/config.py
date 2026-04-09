from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "assistant.db"
DEFAULT_STEAMDT_BASE_PATH = PROJECT_ROOT / "饰品数据" / "steamdt_cs2_base.json"
DEFAULT_SERVERCHAN_BASE_URL = "https://sctapi.ftqq.com"
DEFAULT_C5_BASE_URL = "https://openapi.c5game.com"
DEFAULT_STEAMDT_BASE_URL = "https://open.steamdt.com"
DEFAULT_CSQAQ_BASE_URL = "https://api.csqaq.com"
DEFAULT_STEAM_MARKET_BASE_URL = "https://steamcommunity.com"
DEFAULT_CSGO_APP_ID = 730


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
    steam_id64: str | None = None
    steam_identity_secret: str | None = None
    steam_device_id: str | None = None


def load_settings() -> Settings:
    db_path = Path(_first_env("CS2_ASSISTANT_DB_PATH") or DEFAULT_DB_PATH)
    steamdt_base_path = Path(
        _first_env("CS2_ASSISTANT_STEAMDT_BASE_PATH") or DEFAULT_STEAMDT_BASE_PATH
    )
    return Settings(
        db_path=db_path,
        steamdt_base_path=steamdt_base_path,
        steamdt_api_key=_first_env("STEAMDT_API_KEY"),
        c5_api_key=_first_env("C5GAME_API_KEY", "C5_API_KEY"),
        csqaq_api_token=_first_env("CSQAQ_API_TOKEN", "CSQAQ_API_KEY"),
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
        steam_id64=_first_env("STEAM_ID64"),
        steam_identity_secret=_first_env("STEAM_IDENTITY_SECRET"),
        steam_device_id=_first_env("STEAM_DEVICE_ID"),
    )
