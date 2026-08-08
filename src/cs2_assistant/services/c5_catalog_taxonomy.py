from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable


NONE_ID = "__none__"

_FILTER_KEYS = frozenset(
    {
        "categoryIds",
        "subtypeIds",
        "weaponIds",
        "rarityIds",
        "versions",
        "wearIds",
        "phases",
        "floatMin",
        "floatMax",
        "keyword",
    }
)
_LIST_FILTER_KEYS = (
    "categoryIds",
    "subtypeIds",
    "weaponIds",
    "rarityIds",
    "versions",
    "wearIds",
    "phases",
)

# These are domain boundaries, not catalog-size assumptions.  ByMykel supplies
# the skin's own min/max and the concrete wear variant; the interval below is
# intersected with both, never used to expand a grouped skin into new rows.
_WEAR_DEFINITIONS: tuple[tuple[str, str, float, float, bool], ...] = (
    ("SFUI_InvTooltip_Wear_Amount_0", "崭新出厂", 0.00, 0.07, False),
    ("SFUI_InvTooltip_Wear_Amount_1", "略有磨损", 0.07, 0.15, False),
    ("SFUI_InvTooltip_Wear_Amount_2", "久经沙场", 0.15, 0.38, False),
    ("SFUI_InvTooltip_Wear_Amount_3", "破损不堪", 0.38, 0.45, False),
    ("SFUI_InvTooltip_Wear_Amount_4", "战痕累累", 0.45, 1.00, True),
)
_WEAR_BY_ID = {
    wear_id: (name, lower, upper, upper_inclusive)
    for wear_id, name, lower, upper, upper_inclusive in _WEAR_DEFINITIONS
}

_VERSION_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("normal", "普通"),
    ("stattrak", "StatTrak™"),
    ("souvenir", "纪念品"),
)

_CATEGORY_LABELS = {
    "skins_not_grouped": "武器皮肤",
    "crates": "箱子与容器",
    "stickers": "印花",
    "graffiti": "涂鸦",
    "music_kits": "音乐盒",
    "patches": "布章",
    "keychains": "挂件",
    "collectibles": "收藏品 / 通行证 / 徽章",
    "agents": "探员",
    "keys": "钥匙",
}


@dataclass(frozen=True, slots=True)
class _FloatInterval:
    lower: float
    upper: float
    upper_inclusive: bool


@dataclass(slots=True)
class _CatalogRecord:
    market_hash_name: str
    name_cn: str
    image_url: str | None
    category_id: str
    category_ids: tuple[str, ...]
    category_names: dict[str, str]
    subtype_id: str | None
    subtype_name: str | None
    weapon_id: str | None
    weapon_name: str | None
    rarity_id: str
    rarity_name: str
    rarity_color: str | None
    version: str
    wear_id: str
    wear_name: str
    phase: str | None
    float_interval: _FloatInterval | None
    raw_catalog: dict[str, Any]
    search_text: str

    def public_row(self) -> dict[str, Any]:
        interval = self.float_interval
        return {
            "marketHashName": self.market_hash_name,
            "nameCn": self.name_cn,
            "imageUrl": self.image_url,
            "categoryId": self.category_id,
            "subtypeId": self.subtype_id,
            "weaponId": self.weapon_id,
            "rarityId": self.rarity_id,
            "rarityName": self.rarity_name,
            "rarityColor": self.rarity_color,
            "version": self.version,
            "wearId": self.wear_id,
            "wearName": self.wear_name,
            "phase": self.phase,
            "minFloat": interval.lower if interval is not None else None,
            "maxFloat": interval.upper if interval is not None else None,
            "rawCatalog": self.raw_catalog,
        }


@dataclass(frozen=True, slots=True)
class _NormalizedFilters:
    category_ids: frozenset[str]
    subtype_ids: frozenset[str]
    weapon_ids: frozenset[str]
    rarity_ids: frozenset[str]
    versions: frozenset[str]
    wear_ids: frozenset[str]
    phases: frozenset[str]
    float_min: float | None
    float_max: float | None
    keyword: str

    def public(self) -> dict[str, Any]:
        return {
            "categoryIds": sorted(self.category_ids),
            "subtypeIds": sorted(self.subtype_ids),
            "weaponIds": sorted(self.weapon_ids),
            "rarityIds": sorted(self.rarity_ids),
            "versions": sorted(self.versions),
            "wearIds": sorted(self.wear_ids),
            "phases": sorted(self.phases),
            "floatMin": self.float_min,
            "floatMax": self.float_max,
            "keyword": self.keyword,
        }


def build_c5_catalog_taxonomy(settings: Any) -> dict[str, Any]:
    """Build all CSGO-API facets from the current read-only items snapshot."""

    records = _load_catalog_records(settings)
    category_facets: dict[str, dict[str, Any]] = {}
    subtype_facets: dict[str, dict[str, Any]] = {}
    weapon_facets: dict[str, dict[str, Any]] = {}
    rarity_facets: dict[str, dict[str, Any]] = {}
    version_counts = {version_id: 0 for version_id, _ in _VERSION_DEFINITIONS}
    wear_facets: dict[str, dict[str, Any]] = {}
    phase_counts: dict[str, int] = {}
    float_intervals: list[_FloatInterval] = []

    for record in records:
        _accumulate_category_tree(category_facets, record)
        _accumulate_flat_subtype(subtype_facets, record)
        _accumulate_flat_weapon(weapon_facets, record)

        rarity = rarity_facets.setdefault(
            record.rarity_id,
            {
                "id": record.rarity_id,
                "name": record.rarity_name,
                "color": record.rarity_color,
                "count": 0,
            },
        )
        rarity["count"] += 1
        if not rarity.get("color") and record.rarity_color:
            rarity["color"] = record.rarity_color

        version_counts[record.version] = version_counts.get(record.version, 0) + 1
        wear = wear_facets.setdefault(
            record.wear_id,
            {"id": record.wear_id, "name": record.wear_name, "count": 0},
        )
        wear["count"] += 1
        if record.phase is not None:
            phase_counts[record.phase] = phase_counts.get(record.phase, 0) + 1
        if record.float_interval is not None:
            float_intervals.append(record.float_interval)

    categories = [_finalize_category(value) for value in category_facets.values()]
    categories.sort(key=_option_sort_key)
    subtypes = [_finalize_flat_subtype(value) for value in subtype_facets.values()]
    subtypes.sort(key=_option_sort_key)
    weapons = [_finalize_flat_weapon(value) for value in weapon_facets.values()]
    weapons.sort(key=_option_sort_key)

    rarities = list(rarity_facets.values())
    rarities.sort(key=lambda option: (option["id"] == NONE_ID, *_option_sort_key(option)))
    versions = [
        {"id": version_id, "name": name, "count": version_counts.get(version_id, 0)}
        for version_id, name in _VERSION_DEFINITIONS
    ]
    wears = _ordered_wear_options(wear_facets)
    phases = [
        {"id": phase, "name": phase, "count": count}
        for phase, count in sorted(phase_counts.items(), key=lambda item: item[0].casefold())
    ]
    float_range = {
        "min": min((interval.lower for interval in float_intervals), default=None),
        "max": max((interval.upper for interval in float_intervals), default=None),
    }
    return {
        "totalCount": len(records),
        "categories": categories,
        "subtypes": subtypes,
        "weapons": weapons,
        "rarities": rarities,
        "versions": versions,
        "wears": wears,
        "phases": phases,
        "floatRange": float_range,
    }


def filter_c5_catalog_items(settings: Any, filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Return concrete catalog rows matching every selected facet."""

    normalized = _normalize_filters(filters)
    records = _matching_records(_load_catalog_records(settings), normalized)
    records.sort(key=lambda record: (record.name_cn.casefold(), record.market_hash_name.casefold()))
    return [record.public_row() for record in records]


def estimate_c5_catalog_filter(settings: Any, filters: dict[str, Any]) -> dict[str, Any]:
    """Count a local catalog selection without creating jobs or remote requests."""

    normalized = _normalize_filters(filters)
    records = _load_catalog_records(settings)
    matched_count = sum(1 for record in records if _record_matches(record, normalized))
    warnings: list[str] = []
    if matched_count == 0:
        warnings.append("当前筛选未命中本地 CSGO-API catalog")
    return {
        "totalCatalogCount": len(records),
        "catalogMatchedCount": matched_count,
        "requiresC5PriceCount": matched_count,
        "filters": normalized.public(),
        "warnings": warnings,
    }


def _load_catalog_records(settings: Any) -> list[_CatalogRecord]:
    db_path_value = getattr(settings, "db_path", None)
    if db_path_value is None:
        raise ValueError("settings.db_path is required")
    db_path = Path(db_path_value).expanduser().resolve()
    if not db_path.is_file():
        return []

    uri = f"{db_path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        try:
            rows = connection.execute(
                """
                SELECT market_hash_name, name_cn, raw_json
                FROM items
                ORDER BY market_hash_name COLLATE NOCASE, market_hash_name
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
    finally:
        connection.close()

    records: list[_CatalogRecord] = []
    for row in rows:
        raw_catalog = _decode_raw_catalog(row["raw_json"])
        if raw_catalog is None:
            continue
        record = _record_from_raw(
            raw_catalog,
            market_hash_name=_clean_text(row["market_hash_name"]),
            name_cn=_clean_text(row["name_cn"]),
        )
        if record is not None:
            records.append(record)
    return records


def _decode_raw_catalog(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _record_from_raw(
    raw: dict[str, Any],
    *,
    market_hash_name: str,
    name_cn: str,
) -> _CatalogRecord | None:
    meta = raw.get("csgoApi")
    if not isinstance(meta, dict):
        return None
    category_ids = _source_category_ids(meta)
    if not category_ids:
        return None
    primary_category = _clean_text(meta.get("category")) or category_ids[0]
    if primary_category not in category_ids:
        category_ids = (primary_category, *category_ids)

    market_hash_name = market_hash_name or _clean_text(raw.get("market_hash_name"))
    name_cn = name_cn or _clean_text(raw.get("name")) or market_hash_name
    if not market_hash_name:
        return None

    category_names = {
        category_id: _source_category_name(meta, category_id, primary_category)
        for category_id in category_ids
    }
    subtype_id, subtype_name = _subtype(raw)
    weapon_id, weapon_name = _object_id_name(raw.get("weapon"))

    rarity = raw.get("rarity")
    rarity_id, rarity_name = _object_id_name(rarity)
    if not rarity_id:
        rarity_id = NONE_ID
        rarity_name = "无品质"
    elif not rarity_name:
        rarity_name = rarity_id
    rarity_color = _clean_text(rarity.get("color")) if isinstance(rarity, dict) else None

    wear = raw.get("wear")
    wear_id, raw_wear_name = _object_id_name(wear)
    if not wear_id:
        wear_id = NONE_ID
        wear_name = "无磨损"
    else:
        wear_name = _WEAR_BY_ID.get(wear_id, (raw_wear_name or wear_id, 0.0, 0.0, False))[0]

    version = _catalog_version(raw, market_hash_name, primary_category)
    phase = _scalar_or_object_id(raw.get("phase"))
    float_interval = _effective_float_interval(
        raw.get("min_float"),
        raw.get("max_float"),
        wear_id,
    )
    image_url = (
        _clean_text(raw.get("image"))
        or _clean_text(raw.get("image_url"))
        or _clean_text(raw.get("imageUrl"))
    )
    search_text = "\n".join(
        value.casefold()
        for value in (
            market_hash_name,
            name_cn,
            _clean_text(raw.get("name")),
            primary_category,
            category_names.get(primary_category),
            subtype_id,
            subtype_name,
            weapon_id,
            weapon_name,
            rarity_id,
            rarity_name,
            wear_id,
            wear_name,
            version,
            phase,
        )
        if value
    )
    return _CatalogRecord(
        market_hash_name=market_hash_name,
        name_cn=name_cn,
        image_url=image_url,
        category_id=primary_category,
        category_ids=category_ids,
        category_names=category_names,
        subtype_id=subtype_id,
        subtype_name=subtype_name,
        weapon_id=weapon_id,
        weapon_name=weapon_name,
        rarity_id=rarity_id,
        rarity_name=rarity_name,
        rarity_color=rarity_color,
        version=version,
        wear_id=wear_id,
        wear_name=wear_name,
        phase=phase,
        float_interval=float_interval,
        raw_catalog=raw,
        search_text=search_text,
    )


def _source_category_ids(meta: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    raw_categories = meta.get("categories")
    if isinstance(raw_categories, (list, tuple, set, frozenset)):
        values.extend(_clean_text(value) for value in raw_categories)
    category = _clean_text(meta.get("category"))
    if category:
        values.insert(0, category)
    return tuple(_deduplicate(value for value in values if value))


def _source_category_name(
    meta: dict[str, Any],
    category_id: str,
    primary_category: str,
) -> str:
    names = meta.get("categoryNames")
    if isinstance(names, dict):
        value = _clean_text(names.get(category_id))
        if value:
            return value
    if category_id == primary_category:
        value = _clean_text(meta.get("categoryName") or meta.get("category_name"))
        if value:
            return value
    return _CATEGORY_LABELS.get(category_id, category_id)


def _subtype(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    category_id, category_name = _object_id_name(raw.get("category"))
    if category_id:
        return category_id, category_name or category_id
    raw_type = raw.get("type")
    if isinstance(raw_type, dict):
        subtype_id, subtype_name = _object_id_name(raw_type)
        return subtype_id, subtype_name or subtype_id
    subtype_id = _clean_text(raw_type)
    return subtype_id, subtype_id


def _object_id_name(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    object_id = _clean_text(value.get("id"))
    if not object_id and value is not None and "weapon_id" in value:
        object_id = _clean_text(value.get("weapon_id"))
    return object_id, _clean_text(value.get("name"))


def _scalar_or_object_id(value: Any) -> str | None:
    if isinstance(value, dict):
        object_id, object_name = _object_id_name(value)
        return object_id or object_name
    return _clean_text(value)


def _catalog_version(raw: dict[str, Any], market_hash_name: str, category_id: str) -> str:
    if _truthy(raw.get("souvenir")):
        return "souvenir"
    if _truthy(raw.get("stattrak")) or market_hash_name.casefold().startswith("stattrak™"):
        return "stattrak"
    if category_id == "skins_not_grouped" and market_hash_name.casefold().startswith("souvenir "):
        return "souvenir"
    return "normal"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return False


def _effective_float_interval(
    raw_minimum: Any,
    raw_maximum: Any,
    wear_id: str,
) -> _FloatInterval | None:
    # A grouped skin may contain `wears[]` and base min/max but has no current
    # variant.  Treating it as a 0..1 row would fabricate a tradable exterior.
    if wear_id == NONE_ID:
        return None
    base_minimum = _finite_float(raw_minimum)
    base_maximum = _finite_float(raw_maximum)
    if base_minimum is None or base_maximum is None or base_minimum > base_maximum:
        return None

    definition = _WEAR_BY_ID.get(wear_id)
    if definition is None:
        return _FloatInterval(base_minimum, base_maximum, True)
    _, wear_minimum, wear_maximum, wear_upper_inclusive = definition
    lower = max(base_minimum, wear_minimum)
    upper = min(base_maximum, wear_maximum)
    upper_inclusive = base_maximum < wear_maximum or (
        math.isclose(base_maximum, wear_maximum) and wear_upper_inclusive
    )
    if lower > upper or (math.isclose(lower, upper) and not upper_inclusive):
        return None
    return _FloatInterval(lower, upper, upper_inclusive)


def _normalize_filters(filters: dict[str, Any] | None) -> _NormalizedFilters:
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise TypeError("filters must be a dict")
    unsupported = sorted(set(filters) - _FILTER_KEYS)
    if unsupported:
        raise ValueError(f"unsupported catalog filter keys: {', '.join(unsupported)}")

    normalized_lists = {key: _normalize_id_values(filters.get(key)) for key in _LIST_FILTER_KEYS}
    normalized_lists["versions"] = frozenset(
        value.casefold() for value in normalized_lists["versions"]
    )
    float_min = _filter_float(filters.get("floatMin"), "floatMin")
    float_max = _filter_float(filters.get("floatMax"), "floatMax")
    if float_min is not None and float_max is not None and float_min > float_max:
        raise ValueError("floatMin cannot be greater than floatMax")
    return _NormalizedFilters(
        category_ids=normalized_lists["categoryIds"],
        subtype_ids=normalized_lists["subtypeIds"],
        weapon_ids=normalized_lists["weaponIds"],
        rarity_ids=normalized_lists["rarityIds"],
        versions=normalized_lists["versions"],
        wear_ids=normalized_lists["wearIds"],
        phases=normalized_lists["phases"],
        float_min=float_min,
        float_max=float_max,
        keyword=_clean_text(filters.get("keyword")).casefold(),
    )


def _normalize_id_values(value: Any) -> frozenset[str]:
    if value is None or value == "":
        return frozenset()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        raise TypeError("catalog facet filters must be strings or arrays")
    return frozenset(text for item in values if (text := _clean_text(item)))


def _filter_float(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    parsed = _finite_float(value)
    if parsed is None:
        raise ValueError(f"{field} must be a finite number")
    return parsed


def _matching_records(
    records: Iterable[_CatalogRecord],
    filters: _NormalizedFilters,
) -> list[_CatalogRecord]:
    return [record for record in records if _record_matches(record, filters)]


def _record_matches(record: _CatalogRecord, filters: _NormalizedFilters) -> bool:
    if filters.category_ids and filters.category_ids.isdisjoint(record.category_ids):
        return False
    if filters.subtype_ids and record.subtype_id not in filters.subtype_ids:
        return False
    if filters.weapon_ids and record.weapon_id not in filters.weapon_ids:
        return False
    if filters.rarity_ids and record.rarity_id not in filters.rarity_ids:
        return False
    if filters.versions and record.version not in filters.versions:
        return False
    if filters.wear_ids and record.wear_id not in filters.wear_ids:
        return False
    if filters.phases and record.phase not in filters.phases:
        return False
    if filters.keyword and filters.keyword not in record.search_text:
        return False
    if filters.float_min is not None or filters.float_max is not None:
        if not _interval_overlaps_filter(
            record.float_interval,
            filters.float_min,
            filters.float_max,
        ):
            return False
    return True


def _interval_overlaps_filter(
    interval: _FloatInterval | None,
    filter_minimum: float | None,
    filter_maximum: float | None,
) -> bool:
    if interval is None:
        return False
    if filter_minimum is not None:
        if interval.upper < filter_minimum:
            return False
        if math.isclose(interval.upper, filter_minimum) and not interval.upper_inclusive:
            return False
    if filter_maximum is not None and interval.lower > filter_maximum:
        return False
    return True


def _accumulate_category_tree(
    facets: dict[str, dict[str, Any]],
    record: _CatalogRecord,
) -> None:
    for category_id in record.category_ids:
        category = facets.setdefault(
            category_id,
            {
                "id": category_id,
                "name": record.category_names.get(category_id) or category_id,
                "count": 0,
                "_subtypes": {},
                "_weapons": {},
            },
        )
        category["count"] += 1
        if record.weapon_id:
            _increment_named_option(
                category["_weapons"],
                record.weapon_id,
                record.weapon_name or record.weapon_id,
            )
        if not record.subtype_id:
            continue
        subtype = category["_subtypes"].setdefault(
            record.subtype_id,
            {
                "id": record.subtype_id,
                "name": record.subtype_name or record.subtype_id,
                "count": 0,
                "_weapons": {},
            },
        )
        subtype["count"] += 1
        if record.weapon_id:
            _increment_named_option(
                subtype["_weapons"],
                record.weapon_id,
                record.weapon_name or record.weapon_id,
            )


def _accumulate_flat_subtype(
    facets: dict[str, dict[str, Any]],
    record: _CatalogRecord,
) -> None:
    if not record.subtype_id:
        return
    subtype = facets.setdefault(
        record.subtype_id,
        {
            "id": record.subtype_id,
            "name": record.subtype_name or record.subtype_id,
            "count": 0,
            "_category_ids": set(),
            "_weapons": {},
        },
    )
    subtype["count"] += 1
    subtype["_category_ids"].update(record.category_ids)
    if record.weapon_id:
        _increment_named_option(
            subtype["_weapons"],
            record.weapon_id,
            record.weapon_name or record.weapon_id,
        )


def _accumulate_flat_weapon(
    facets: dict[str, dict[str, Any]],
    record: _CatalogRecord,
) -> None:
    if not record.weapon_id:
        return
    weapon = facets.setdefault(
        record.weapon_id,
        {
            "id": record.weapon_id,
            "name": record.weapon_name or record.weapon_id,
            "count": 0,
            "_category_ids": set(),
            "_subtype_ids": set(),
        },
    )
    weapon["count"] += 1
    weapon["_category_ids"].update(record.category_ids)
    if record.subtype_id:
        weapon["_subtype_ids"].add(record.subtype_id)


def _increment_named_option(
    options: dict[str, dict[str, Any]],
    option_id: str,
    name: str,
) -> None:
    option = options.setdefault(option_id, {"id": option_id, "name": name, "count": 0})
    option["count"] += 1


def _finalize_category(value: dict[str, Any]) -> dict[str, Any]:
    subtypes = [_finalize_nested_subtype(option) for option in value["_subtypes"].values()]
    subtypes.sort(key=_option_sort_key)
    weapons = list(value["_weapons"].values())
    weapons.sort(key=_option_sort_key)
    return {
        "id": value["id"],
        "name": value["name"],
        "count": value["count"],
        "subtypes": subtypes,
        "weapons": weapons,
    }


def _finalize_nested_subtype(value: dict[str, Any]) -> dict[str, Any]:
    weapons = list(value["_weapons"].values())
    weapons.sort(key=_option_sort_key)
    return {
        "id": value["id"],
        "name": value["name"],
        "count": value["count"],
        "weapons": weapons,
    }


def _finalize_flat_subtype(value: dict[str, Any]) -> dict[str, Any]:
    weapons = list(value["_weapons"].values())
    weapons.sort(key=_option_sort_key)
    return {
        "id": value["id"],
        "name": value["name"],
        "count": value["count"],
        "categoryIds": sorted(value["_category_ids"]),
        "weapons": weapons,
    }


def _finalize_flat_weapon(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value["id"],
        "name": value["name"],
        "count": value["count"],
        "categoryIds": sorted(value["_category_ids"]),
        "subtypeIds": sorted(value["_subtype_ids"]),
    }


def _ordered_wear_options(observed: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    canonical_ids = {wear_id for wear_id, *_ in _WEAR_DEFINITIONS}
    for wear_id, name, _lower, _upper, _upper_inclusive in _WEAR_DEFINITIONS:
        options.append(
            {"id": wear_id, "name": name, "count": observed.get(wear_id, {}).get("count", 0)}
        )
    extras = [
        option
        for wear_id, option in observed.items()
        if wear_id not in canonical_ids and wear_id != NONE_ID
    ]
    extras.sort(key=_option_sort_key)
    options.extend(extras)
    options.append(
        {
            "id": NONE_ID,
            "name": "无磨损",
            "count": observed.get(NONE_ID, {}).get("count", 0),
        }
    )
    return options


def _option_sort_key(option: dict[str, Any]) -> tuple[str, str]:
    return (str(option.get("name") or "").casefold(), str(option.get("id") or "").casefold())


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return ""
    return str(value).strip()


def _deduplicate(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = [
    "build_c5_catalog_taxonomy",
    "filter_c5_catalog_items",
    "estimate_c5_catalog_filter",
]
