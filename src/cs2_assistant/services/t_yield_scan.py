from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cs2_assistant.clients import C5GameClient, C5GameError, CSQAQClient, SteamDTClient
from cs2_assistant.config import Settings
from cs2_assistant.services.market import (
    DEFAULT_C5_SETTLEMENT_FACTOR,
    DEFAULT_STEAM_BALANCE_DISCOUNT,
    MarketService,
    calculate_ratio,
    calculate_t_yield_rate,
)
from cs2_assistant.utils import ensure_parent_dir, safe_float, utc_now_iso

INVENTORY_FILTER_ALL = "all"
INVENTORY_FILTER_ALL_COOLDOWN = "all_cooldown"
INVENTORY_FILTER_HAS_TRADABLE = "has_tradable"
INVENTORY_FILTER_TRADABLE_ONLY = "tradable_only"
INVENTORY_FILTER_COOLDOWN_ONLY = "cooldown_only"
INVENTORY_FILTER_MIXED_ONLY = "mixed_only"
DEFAULT_DAILY_REMINDER_INVENTORY_FILTER = INVENTORY_FILTER_MIXED_ONLY

VALID_INVENTORY_FILTERS = {
    INVENTORY_FILTER_ALL,
    INVENTORY_FILTER_ALL_COOLDOWN,
    INVENTORY_FILTER_HAS_TRADABLE,
    INVENTORY_FILTER_TRADABLE_ONLY,
    INVENTORY_FILTER_COOLDOWN_ONLY,
    INVENTORY_FILTER_MIXED_ONLY,
}

INVENTORY_FILTER_LABELS: dict[str, str] = {
    INVENTORY_FILTER_ALL: "全部库存",
    INVENTORY_FILTER_ALL_COOLDOWN: "全冷却",
    INVENTORY_FILTER_HAS_TRADABLE: "存在不冷却",
    INVENTORY_FILTER_TRADABLE_ONLY: "仅不冷却",
    INVENTORY_FILTER_COOLDOWN_ONLY: "全冷却",
    INVENTORY_FILTER_MIXED_ONLY: "同时有冷却和不冷却",
}


def inventory_filter_label(value: str) -> str:
    return INVENTORY_FILTER_LABELS.get(value, value)


def normalize_inventory_filter(
    value: str | None,
    *,
    default: str = INVENTORY_FILTER_ALL,
) -> str:
    raw = str(value or default).strip().lower()
    aliases = {
        "all": INVENTORY_FILTER_ALL,
        "all_cooldown": INVENTORY_FILTER_ALL_COOLDOWN,
        "has_tradable": INVENTORY_FILTER_HAS_TRADABLE,
        "exists_tradable": INVENTORY_FILTER_HAS_TRADABLE,
        "has_uncooled": INVENTORY_FILTER_HAS_TRADABLE,
        "exists_uncooldown": INVENTORY_FILTER_HAS_TRADABLE,
        "not_all_cooldown": INVENTORY_FILTER_HAS_TRADABLE,
        "tradable": INVENTORY_FILTER_TRADABLE_ONLY,
        "tradable_only": INVENTORY_FILTER_TRADABLE_ONLY,
        "no_cooldown": INVENTORY_FILTER_TRADABLE_ONLY,
        "non_cooldown": INVENTORY_FILTER_TRADABLE_ONLY,
        "uncooldown": INVENTORY_FILTER_TRADABLE_ONLY,
        "cooldown": INVENTORY_FILTER_COOLDOWN_ONLY,
        "cooldown_only": INVENTORY_FILTER_COOLDOWN_ONLY,
        "locked": INVENTORY_FILTER_COOLDOWN_ONLY,
        "mixed": INVENTORY_FILTER_MIXED_ONLY,
        "mixed_only": INVENTORY_FILTER_MIXED_ONLY,
        "both": INVENTORY_FILTER_MIXED_ONLY,
    }
    normalized = aliases.get(raw, raw)
    if normalized not in VALID_INVENTORY_FILTERS:
        supported = ", ".join(sorted(VALID_INVENTORY_FILTERS))
        raise ValueError(f"--inventory-filter 必须是以下值之一: {supported}")
    return normalized


def cooldown_count_from_counts(inventory_count: int, tradable_count: int) -> int:
    return max(0, int(inventory_count) - int(tradable_count))


def inventory_status_from_counts(inventory_count: int, tradable_count: int) -> str:
    tradable = max(0, int(tradable_count))
    cooldown = cooldown_count_from_counts(inventory_count, tradable_count)
    if tradable > 0 and cooldown > 0:
        return INVENTORY_FILTER_MIXED_ONLY
    if tradable > 0:
        return INVENTORY_FILTER_TRADABLE_ONLY
    return INVENTORY_FILTER_COOLDOWN_ONLY


def inventory_status_summary(inventory_count: int, tradable_count: int) -> str:
    cooldown = cooldown_count_from_counts(inventory_count, tradable_count)
    status = inventory_status_from_counts(inventory_count, tradable_count)
    return f"{inventory_filter_label(status)} ({cooldown} 冷却 / {tradable_count} 不冷却)"


def inventory_matches_filter(inventory_status: str, inventory_filter: str) -> bool:
    normalized_filter = normalize_inventory_filter(inventory_filter)
    if normalized_filter == INVENTORY_FILTER_ALL:
        return True
    if normalized_filter == INVENTORY_FILTER_ALL_COOLDOWN:
        return inventory_status == INVENTORY_FILTER_COOLDOWN_ONLY
    if normalized_filter == INVENTORY_FILTER_HAS_TRADABLE:
        return inventory_status in {INVENTORY_FILTER_TRADABLE_ONLY, INVENTORY_FILTER_MIXED_ONLY}
    return inventory_status == normalized_filter


def filter_inventory_summaries(
    inventory_summaries: list[dict[str, Any]],
    inventory_filter: str,
) -> list[dict[str, Any]]:
    normalized_filter = normalize_inventory_filter(inventory_filter)
    if normalized_filter == INVENTORY_FILTER_ALL:
        return list(inventory_summaries)
    return [
        summary
        for summary in inventory_summaries
        if inventory_matches_filter(str(summary.get("inventory_status") or ""), normalized_filter)
    ]


@dataclass(slots=True)
class TYieldAccountRef:
    steam_id: str
    nickname: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "steamId": self.steam_id,
            "nickname": self.nickname,
        }


@dataclass(slots=True)
class TYieldCandidate:
    name: str
    market_hash_name: str
    inventory_count: int
    tradable_count: int
    inventory_status: str
    steam_accounts: list[TYieldAccountRef]
    c5_lowest_sell_price: float
    c5_price_source: str
    steam_lowest_sell_price: float
    steam_price_source: str
    ratio: float
    t_yield_rate: float

    @property
    def cooldown_count(self) -> int:
        return cooldown_count_from_counts(self.inventory_count, self.tradable_count)

    @property
    def inventory_status_label(self) -> str:
        return inventory_filter_label(self.inventory_status)

    @property
    def inventory_status_summary(self) -> str:
        return inventory_status_summary(self.inventory_count, self.tradable_count)

    @property
    def t_yield_pct(self) -> float:
        return self.t_yield_rate * 100

    def to_dict(self, *, rank: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "marketHashName": self.market_hash_name,
            "inventoryCount": self.inventory_count,
            "tradableCount": self.tradable_count,
            "cooldownCount": self.cooldown_count,
            "inventoryStatus": self.inventory_status,
            "inventoryStatusLabel": self.inventory_status_label,
            "inventoryStatusSummary": self.inventory_status_summary,
            "steamAccounts": [account.to_dict() for account in self.steam_accounts],
            "c5LowestSellPrice": self.c5_lowest_sell_price,
            "c5PriceSource": self.c5_price_source,
            "steamLowestSellPrice": self.steam_lowest_sell_price,
            "steamPriceSource": self.steam_price_source,
            "ratio": self.ratio,
            "tYieldRate": self.t_yield_rate,
            "tYieldPct": self.t_yield_pct,
        }
        if rank is not None:
            payload["rank"] = rank
        return payload


@dataclass(slots=True)
class MissingSteamPriceIssue:
    name: str
    market_hash_name: str
    inventory_count: int
    tradable_count: int
    inventory_status: str
    c5_sell_price: float
    c5_price_source: str | None
    steam_price_source: str | None
    steam_sources_attempted: list[str]
    steam_accounts: list[TYieldAccountRef]

    @property
    def cooldown_count(self) -> int:
        return cooldown_count_from_counts(self.inventory_count, self.tradable_count)

    @property
    def inventory_status_label(self) -> str:
        return inventory_filter_label(self.inventory_status)

    @property
    def inventory_status_summary(self) -> str:
        return inventory_status_summary(self.inventory_count, self.tradable_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "marketHashName": self.market_hash_name,
            "inventoryCount": self.inventory_count,
            "tradableCount": self.tradable_count,
            "cooldownCount": self.cooldown_count,
            "inventoryStatus": self.inventory_status,
            "inventoryStatusLabel": self.inventory_status_label,
            "inventoryStatusSummary": self.inventory_status_summary,
            "c5SellPrice": self.c5_sell_price,
            "c5PriceSource": self.c5_price_source,
            "steamPriceSource": self.steam_price_source,
            "steamSourcesAttempted": self.steam_sources_attempted,
            "steamAccounts": [account.to_dict() for account in self.steam_accounts],
        }


@dataclass(slots=True)
class TYieldScanReport:
    generated_at: str
    inventory_source: str
    inventory_cached_at: str | None
    inventory_filter: str
    accounts: list[TYieldAccountRef]
    inventory_type_total_count: int
    inventory_type_count: int
    candidates: list[TYieldCandidate]
    missing_steam_prices: list[MissingSteamPriceIssue]
    missing_steam_price_path: str

    @property
    def inventory_filter_label(self) -> str:
        return inventory_filter_label(self.inventory_filter)

    def top_rows(self, top_n: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, candidate in enumerate(self.candidates[:top_n], start=1):
            rows.append(candidate.to_dict(rank=index))
        return rows

    def bottom_rows(self, bottom_n: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        ranked_candidates = sorted(self.candidates, key=lambda candidate: candidate.t_yield_rate)
        for index, candidate in enumerate(ranked_candidates[:bottom_n], start=1):
            rows.append(candidate.to_dict(rank=index))
        return rows


def filter_candidates_by_inventory_filter(
    candidates: list[TYieldCandidate],
    inventory_filter: str,
) -> list[TYieldCandidate]:
    normalized_filter = normalize_inventory_filter(inventory_filter)
    if normalized_filter == INVENTORY_FILTER_ALL:
        return list(candidates)
    return [
        candidate
        for candidate in candidates
        if inventory_matches_filter(candidate.inventory_status, normalized_filter)
    ]


def _inventory_cache_path(settings: Settings) -> Path:
    return settings.db_path.parent / "c5_inventory_all_cache.json"


def _missing_steam_report_path(settings: Settings) -> Path:
    return settings.db_path.parent / "c5_t_yield_missing_steam_prices.json"


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_inventory_cache(
    settings: Settings,
    *,
    max_age_minutes: int | None = None,
) -> dict[str, Any] | None:
    cache_path = _inventory_cache_path(settings)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    cached_at = _parse_iso_datetime(str(payload.get("cachedAt") or ""))
    if max_age_minutes is not None and cached_at is not None:
        max_age = timedelta(minutes=max_age_minutes)
        if datetime.now(timezone.utc) - cached_at > max_age:
            return None

    payload["source"] = "cache"
    return payload


def _save_inventory_cache(settings: Settings, payload: dict[str, Any]) -> None:
    cache_path = _inventory_cache_path(settings)
    ensure_parent_dir(cache_path)
    payload_to_write = dict(payload)
    payload_to_write["cachedAt"] = utc_now_iso()
    cache_path.write_text(
        json.dumps(payload_to_write, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_missing_steam_report(settings: Settings, items: list[MissingSteamPriceIssue]) -> Path:
    path = _missing_steam_report_path(settings)
    ensure_parent_dir(path)
    path.write_text(
        json.dumps(
            {
                "updatedAt": utc_now_iso(),
                "itemCount": len(items),
                "items": [item.to_dict() for item in items],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_missing_steam_report(settings: Settings) -> dict[str, Any]:
    path = _missing_steam_report_path(settings)
    if not path.exists():
        return {"updatedAt": None, "itemCount": 0, "items": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"updatedAt": None, "itemCount": 0, "items": []}
    if not isinstance(payload, dict):
        return {"updatedAt": None, "itemCount": 0, "items": []}
    return {
        "updatedAt": payload.get("updatedAt"),
        "itemCount": int(payload.get("itemCount") or 0),
        "items": list(payload.get("items") or []),
    }


def list_c5_steam_accounts(client: C5GameClient) -> list[dict[str, Any]]:
    info = client.steam_info()
    accounts: list[dict[str, Any]] = []

    steam_list = info.get("steamList") or []
    if isinstance(steam_list, list):
        for account in steam_list:
            if not isinstance(account, dict):
                continue
            steam_id = str(account.get("steamId") or "").strip()
            if not steam_id:
                continue
            accounts.append(
                {
                    "steamId": steam_id,
                    "nickname": account.get("nickname"),
                    "username": account.get("username"),
                    "relationId": account.get("relationId"),
                    "autoType": account.get("autoType"),
                    "tradableTime": account.get("tradableTime"),
                    "accountStatus": account.get("accountStatus"),
                }
            )

    if not accounts:
        direct_steam_id = str(info.get("steamId") or "").strip()
        if direct_steam_id:
            accounts.append(
                {
                    "steamId": direct_steam_id,
                    "nickname": info.get("nickname"),
                    "username": info.get("username"),
                    "relationId": info.get("relationId"),
                    "autoType": info.get("autoType"),
                    "tradableTime": info.get("tradableTime"),
                    "accountStatus": info.get("accountStatus"),
                }
            )

    seen: set[str] = set()
    unique_accounts: list[dict[str, Any]] = []
    for account in accounts:
        steam_id = str(account["steamId"])
        if steam_id in seen:
            continue
        seen.add(steam_id)
        unique_accounts.append(account)

    unique_accounts.sort(key=lambda account: (0 if account.get("autoType") == 2 else 1, account["steamId"]))
    return unique_accounts


def _fetch_single_inventory(
    client: C5GameClient,
    settings: Settings,
    account: dict[str, Any],
) -> dict[str, Any]:
    steam_id = str(account["steamId"])
    try:
        inventory = client.inventory(steam_id, app_id=settings.app_id)
    except C5GameError as exc:
        if _is_empty_inventory_error(exc):
            inventory = {"list": [], "total": 0}
        else:
            raise
    items = inventory.get("list") or []
    if not isinstance(items, list):
        items = []
    inventory_total = inventory.get("total")
    return {
        "steamId": steam_id,
        "nickname": account.get("nickname"),
        "username": account.get("username"),
        "autoType": account.get("autoType"),
        "total": inventory_total if inventory_total is not None else len(items),
        "list": items,
    }


def _is_empty_inventory_error(exc: Exception) -> bool:
    message = str(exc)
    return ("库存为空" in message) or ("206003" in message)


def _empty_inventory(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "steamId": str(account.get("steamId") or ""),
        "nickname": account.get("nickname"),
        "username": account.get("username"),
        "autoType": account.get("autoType"),
        "total": 0,
        "list": [],
    }


def fetch_all_c5_inventories(
    client: C5GameClient,
    settings: Settings,
    *,
    allow_cached_fallback: bool,
    cache_max_age_minutes: int | None,
) -> dict[str, Any]:
    cached_payload = _load_inventory_cache(settings, max_age_minutes=cache_max_age_minutes)

    try:
        accounts = list_c5_steam_accounts(client)
    except Exception:
        if allow_cached_fallback and cached_payload is not None:
            return cached_payload
        raise

    if not accounts:
        if allow_cached_fallback and cached_payload is not None:
            return cached_payload
        raise RuntimeError("未找到绑定的 Steam 账号。")

    inventories: list[dict[str, Any]] = []
    merged_items: list[dict[str, Any]] = []
    total = 0

    try:
        max_workers = min(4, len(accounts))
        if max_workers <= 1:
            fetched_inventories = []
            for account in accounts:
                try:
                    fetched_inventories.append(_fetch_single_inventory(client, settings, account))
                except C5GameError as exc:
                    if _is_empty_inventory_error(exc):
                        fetched_inventories.append(_empty_inventory(account))
                    else:
                        raise
        else:
            inventory_by_steam_id: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(_fetch_single_inventory, client, settings, account): str(account["steamId"])
                    for account in accounts
                }
                for future in as_completed(future_map):
                    steam_id = future_map[future]
                    try:
                        inventory_by_steam_id[steam_id] = future.result()
                    except C5GameError as exc:
                        if _is_empty_inventory_error(exc):
                            account = next(
                                (row for row in accounts if str(row.get("steamId")) == steam_id),
                                None,
                            )
                            inventory_by_steam_id[steam_id] = _empty_inventory(account or {"steamId": steam_id})
                        else:
                            raise
            fetched_inventories = [
                inventory_by_steam_id[str(account["steamId"])]
                for account in accounts
            ]

        for inventory in fetched_inventories:
            items = inventory.get("list") or []
            total += inventory.get("total") if isinstance(inventory.get("total"), int) else len(items)
            inventories.append(inventory)
            for item in items:
                if isinstance(item, dict):
                    enriched_item = dict(item)
                    enriched_item.setdefault("steamId", inventory["steamId"])
                    enriched_item.setdefault("steamNickname", inventory.get("nickname"))
                    merged_items.append(enriched_item)
    except Exception:
        if allow_cached_fallback and cached_payload is not None:
            return cached_payload
        raise

    payload = {
        "source": "live",
        "cachedAt": utc_now_iso(),
        "accountCount": len(accounts),
        "total": total,
        "accounts": accounts,
        "inventories": inventories,
        "list": merged_items,
    }
    _save_inventory_cache(settings, payload)
    return payload


def summarize_inventory_types(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        market_hash_name = str(item.get("marketHashName") or "").strip()
        if not market_hash_name:
            continue

        summary = grouped.setdefault(
            market_hash_name,
            {
                "market_hash_name": market_hash_name,
                "name_cn": item.get("name") or item.get("shortName") or market_hash_name,
                "inventory_count": 0,
                "tradable_count": 0,
                "steam_ids": set(),
                "c5_item_id": item.get("itemId"),
                "reference_price": safe_float(item.get("price")),
            },
        )
        summary["inventory_count"] += 1
        if item.get("ifTradable") is True:
            summary["tradable_count"] += 1
        steam_id = str(item.get("steamId") or "").strip()
        if steam_id:
            summary["steam_ids"].add(steam_id)
        if not summary.get("c5_item_id") and item.get("itemId"):
            summary["c5_item_id"] = item.get("itemId")
        if summary.get("reference_price") is None:
            summary["reference_price"] = safe_float(item.get("price"))

    summaries: list[dict[str, Any]] = []
    for summary in grouped.values():
        inventory_count = int(summary["inventory_count"])
        tradable_count = int(summary["tradable_count"])
        inventory_status = inventory_status_from_counts(inventory_count, tradable_count)
        summaries.append(
            {
                "market_hash_name": summary["market_hash_name"],
                "name_cn": summary["name_cn"],
                "inventory_count": inventory_count,
                "tradable_count": tradable_count,
                "cooldown_count": cooldown_count_from_counts(inventory_count, tradable_count),
                "inventory_status": inventory_status,
                "inventory_status_label": inventory_filter_label(inventory_status),
                "inventory_status_summary": inventory_status_summary(inventory_count, tradable_count),
                "steam_ids": sorted(summary["steam_ids"]),
                "c5_item_id": summary["c5_item_id"],
                "reference_price": summary["reference_price"],
            }
        )
    summaries.sort(key=lambda row: row["market_hash_name"])
    return summaries


def configured_steam_sources(settings: Settings) -> list[str]:
    sources: list[str] = []
    if settings.steamdt_api_key:
        sources.append("steamdt")
    if settings.csqaq_api_token:
        sources.append("csqaq")
    return sources


def build_market_service(
    settings: Settings,
    *,
    include_c5_purchase_prices: bool,
) -> MarketService:
    return MarketService(
        steamdt_client=SteamDTClient(settings.steamdt_api_key, settings.steamdt_base_url)
        if settings.steamdt_api_key
        else None,
        csqaq_client=CSQAQClient(settings.csqaq_api_token, settings.csqaq_base_url)
        if settings.csqaq_api_token
        else None,
        c5_client=C5GameClient(settings.c5_api_key, settings.c5_base_url)
        if settings.c5_api_key
        else None,
        app_id=settings.app_id,
        include_c5_purchase_prices=include_c5_purchase_prices,
    )


def scan_t_yield(
    settings: Settings,
    *,
    min_price: float,
    steam_discount: float = DEFAULT_STEAM_BALANCE_DISCOUNT,
    allow_cached_fallback: bool = True,
    cache_max_age_minutes: int | None = 180,
    inventory_filter: str = INVENTORY_FILTER_ALL,
) -> TYieldScanReport:
    normalized_inventory_filter = normalize_inventory_filter(inventory_filter)

    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    if not settings.steamdt_api_key and not settings.csqaq_api_token:
        raise RuntimeError("缺少 STEAMDT_API_KEY 或 CSQAQ_API_KEY / CSQAQ_API_TOKEN 环境变量。")
    if min_price < 0:
        raise ValueError("--min-price 不能小于 0。")

    c5_client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    inventory_payload = fetch_all_c5_inventories(
        c5_client,
        settings,
        allow_cached_fallback=allow_cached_fallback,
        cache_max_age_minutes=cache_max_age_minutes,
    )
    all_inventory_types = summarize_inventory_types(list(inventory_payload.get("list") or []))
    inventory_types = filter_inventory_summaries(all_inventory_types, normalized_inventory_filter)
    accounts = [
        TYieldAccountRef(
            steam_id=str(account.get("steamId") or "").strip(),
            nickname=account.get("nickname"),
        )
        for account in list(inventory_payload.get("accounts") or [])
        if str(account.get("steamId") or "").strip()
    ]
    account_lookup = {account.steam_id: account.nickname for account in accounts}

    if not inventory_types:
        missing_path = _save_missing_steam_report(settings, [])
        return TYieldScanReport(
            generated_at=utc_now_iso(),
            inventory_source=str(inventory_payload.get("source") or "live"),
            inventory_cached_at=str(inventory_payload.get("cachedAt") or "") or None,
            inventory_filter=normalized_inventory_filter,
            accounts=accounts,
            inventory_type_total_count=len(all_inventory_types),
            inventory_type_count=0,
            candidates=[],
            missing_steam_prices=[],
            missing_steam_price_path=str(missing_path),
        )

    states = build_market_service(settings, include_c5_purchase_prices=False).refresh_items(inventory_types)
    state_map = {state.market_hash_name: state for state in states}
    attempted_sources = configured_steam_sources(settings)

    candidates: list[TYieldCandidate] = []
    missing_steam_prices: list[MissingSteamPriceIssue] = []

    for item_type in inventory_types:
        state = state_map.get(item_type["market_hash_name"])
        if state is None:
            continue

        c5_sell_price = item_type["reference_price"]
        c5_price_source = "inventory_price" if c5_sell_price is not None else None
        if c5_sell_price is None:
            c5_sell_price = state.c5_sell_price
            c5_price_source = state.c5_price_source

        if c5_sell_price is None:
            continue

        steam_accounts = [
            TYieldAccountRef(steam_id=steam_id, nickname=account_lookup.get(steam_id))
            for steam_id in item_type["steam_ids"]
        ]

        if state.steam_sell_price is None:
            missing_steam_prices.append(
                MissingSteamPriceIssue(
                    name=state.name_cn or item_type["name_cn"],
                    market_hash_name=item_type["market_hash_name"],
                    inventory_count=int(item_type["inventory_count"]),
                    tradable_count=int(item_type["tradable_count"]),
                    inventory_status=str(item_type["inventory_status"]),
                    c5_sell_price=float(c5_sell_price),
                    c5_price_source=c5_price_source,
                    steam_price_source=state.steam_price_source,
                    steam_sources_attempted=list(attempted_sources),
                    steam_accounts=steam_accounts,
                )
            )
            continue

        ratio = calculate_ratio(
            c5_sell_price,
            state.steam_sell_price,
            c5_settlement_factor=DEFAULT_C5_SETTLEMENT_FACTOR,
        )
        t_yield_rate = calculate_t_yield_rate(
            ratio,
            steam_balance_discount=steam_discount,
            c5_settlement_factor=DEFAULT_C5_SETTLEMENT_FACTOR,
        )
        if t_yield_rate is None or c5_sell_price < min_price:
            continue

        candidates.append(
            TYieldCandidate(
                name=state.name_cn or item_type["name_cn"],
                market_hash_name=item_type["market_hash_name"],
                inventory_count=int(item_type["inventory_count"]),
                tradable_count=int(item_type["tradable_count"]),
                inventory_status=str(item_type["inventory_status"]),
                steam_accounts=steam_accounts,
                c5_lowest_sell_price=float(c5_sell_price),
                c5_price_source=c5_price_source or "unknown",
                steam_lowest_sell_price=float(state.steam_sell_price),
                steam_price_source=state.steam_price_source or "unknown",
                ratio=float(ratio),
                t_yield_rate=float(t_yield_rate),
            )
        )

    candidates.sort(key=lambda item: item.t_yield_rate, reverse=True)
    missing_steam_prices.sort(key=lambda item: item.market_hash_name)
    missing_path = _save_missing_steam_report(settings, missing_steam_prices)

    return TYieldScanReport(
        generated_at=utc_now_iso(),
        inventory_source=str(inventory_payload.get("source") or "live"),
        inventory_cached_at=str(inventory_payload.get("cachedAt") or "") or None,
        inventory_filter=normalized_inventory_filter,
        accounts=accounts,
        inventory_type_total_count=len(all_inventory_types),
        inventory_type_count=len(inventory_types),
        candidates=candidates,
        missing_steam_prices=missing_steam_prices,
        missing_steam_price_path=str(missing_path),
    )
