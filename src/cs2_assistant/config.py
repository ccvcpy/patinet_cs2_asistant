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
DEFAULT_CSGO_APP_ID = 730


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
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
    app_id: int = DEFAULT_CSGO_APP_ID


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
    )
