from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

import requests

from cs2_assistant.models import CatalogItem


CSGO_API_BASE_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api"
CSGO_API_DEFAULT_LANGUAGE = "zh-CN"
CSGO_API_DEFAULT_CATEGORIES: tuple[str, ...] = (
    "crates",
    "skins_not_grouped",
    "keychains",
    "stickers",
    "agents",
    "graffiti",
    "music_kits",
    "patches",
    "collectibles",
    "keys",
)


@dataclass(slots=True)
class CsgoApiCatalogResult:
    items: list[CatalogItem]
    language: str
    category_counts: dict[str, int]
    weapon_case_count: int


def load_steamdt_catalog(path: Path) -> list[CatalogItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get("data") or []
    items: list[CatalogItem] = []
    for raw_item in data:
        platform_map = {
            str((platform.get("name") or "")).upper(): str(platform.get("itemId") or "")
            for platform in raw_item.get("platformList") or []
        }
        item = CatalogItem(
            market_hash_name=str(raw_item.get("marketHashName") or "").strip(),
            name_cn=str(raw_item.get("name") or "").strip(),
            c5_item_id=platform_map.get("C5") or None,
            steam_item_id=platform_map.get("STEAM") or None,
            raw_json=raw_item,
        )
        if item.market_hash_name and item.name_cn:
            items.append(item)
    return items


def load_csgo_api_catalog(
    *,
    language: str = CSGO_API_DEFAULT_LANGUAGE,
    categories: Iterable[str] | None = None,
    base_url: str = CSGO_API_BASE_URL,
    timeout: float = 30.0,
) -> CsgoApiCatalogResult:
    requested_categories = _normalize_categories(categories)
    by_market_hash_name: dict[str, CatalogItem] = {}
    category_counts: dict[str, int] = {}

    for category in requested_categories:
        payload = _fetch_csgo_api_category(
            language=language,
            category=category,
            base_url=base_url,
            timeout=timeout,
        )
        raw_items = _extract_csgo_api_items(payload, category=category)
        category_counts[category] = len(raw_items)
        for raw_item in raw_items:
            item = _catalog_item_from_csgo_api(raw_item, language=language, category=category)
            if item is None:
                continue
            existing = by_market_hash_name.get(item.market_hash_name)
            if existing is None:
                by_market_hash_name[item.market_hash_name] = item
                continue
            _merge_csgo_api_category(existing.raw_json, category)
            if not existing.name_cn and item.name_cn:
                existing.name_cn = item.name_cn

    items = list(by_market_hash_name.values())
    return CsgoApiCatalogResult(
        items=items,
        language=language,
        category_counts=category_counts,
        weapon_case_count=sum(1 for item in items if is_csgo_api_weapon_case(item.raw_json)),
    )


def is_csgo_api_weapon_case(raw_json: dict[str, Any]) -> bool:
    if not isinstance(raw_json, dict):
        return False
    categories = _csgo_api_categories(raw_json)
    return "crates" in categories


def _normalize_categories(categories: Iterable[str] | None) -> tuple[str, ...]:
    source = categories or CSGO_API_DEFAULT_CATEGORIES
    normalized: list[str] = []
    seen: set[str] = set()
    for category in source:
        value = str(category or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def _fetch_csgo_api_category(
    *,
    language: str,
    category: str,
    base_url: str,
    timeout: float,
) -> Any:
    url = f"{base_url.rstrip('/')}/{language}/{category}.json"
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "cs2-assistant/0.1"},
    )
    response.raise_for_status()
    return response.json()


def _extract_csgo_api_items(payload: Any, *, category: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("data") or payload.get("items") or []
    else:
        raise ValueError(f"CSGO-API {category} 返回格式不是 list/dict")
    if not isinstance(raw_items, list):
        raise ValueError(f"CSGO-API {category} data/items 不是 list")
    return [item for item in raw_items if isinstance(item, dict)]


def _catalog_item_from_csgo_api(
    raw_item: dict[str, Any],
    *,
    language: str,
    category: str,
) -> CatalogItem | None:
    market_hash_name = str(raw_item.get("market_hash_name") or "").strip()
    if not market_hash_name:
        return None
    name_cn = str(raw_item.get("name") or market_hash_name).strip()
    raw_json = dict(raw_item)
    raw_json["csgoApi"] = {
        "source": "ByMykel/CSGO-API",
        "language": language,
        "category": category,
        "categories": [category],
    }
    return CatalogItem(
        market_hash_name=market_hash_name,
        name_cn=name_cn,
        raw_json=raw_json,
    )


def _merge_csgo_api_category(raw_json: dict[str, Any], category: str) -> None:
    meta = raw_json.setdefault("csgoApi", {})
    if not isinstance(meta, dict):
        raw_json["csgoApi"] = {"categories": [category], "category": category}
        return
    categories = _csgo_api_categories(raw_json)
    categories.add(category)
    meta["categories"] = sorted(categories)
    if not meta.get("category"):
        meta["category"] = category


def _csgo_api_categories(raw_json: dict[str, Any]) -> set[str]:
    meta = raw_json.get("csgoApi") or {}
    if not isinstance(meta, dict):
        return set()
    categories: set[str] = set()
    raw_categories = meta.get("categories")
    if isinstance(raw_categories, list):
        categories.update(str(category).strip() for category in raw_categories if str(category).strip())
    category = str(meta.get("category") or "").strip()
    if category:
        categories.add(category)
    return categories
