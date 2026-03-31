from __future__ import annotations

import json
from pathlib import Path

from cs2_assistant.models import CatalogItem


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
