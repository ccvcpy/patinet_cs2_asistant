from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from cs2_assistant.accounts.browser import relogin_with_browser
from cs2_assistant.accounts import AccountStore
from cs2_assistant.accounts.steam_auth import (
    _verify_steam_cookies_valid,
    try_steam_auto_relogin,
)
from cs2_assistant.catalog import (
    CSGO_API_BASE_URL,
    CSGO_API_DEFAULT_CATEGORIES,
    CSGO_API_DEFAULT_LANGUAGE,
    is_csgo_api_weapon_case,
    load_csgo_api_catalog,
    load_steamdt_catalog,
)
from cs2_assistant.clients import (
    C5GameClient,
    CSQAQClient,
    ServerChanClient,
    SteamDTClient,
    SteamMarketClient,
)
from cs2_assistant.clients.steam_market import SteamMarketError
from cs2_assistant.config import PROJECT_ROOT, Settings, load_settings
from cs2_assistant.db import Database
from cs2_assistant.reminders.t_yield import main as t_yield_reminder_main
from cs2_assistant.services import AlertService, MarketService, NotificationService
from cs2_assistant.models import (
    STRATEGY_GUADAO,
    STRATEGY_HOLD,
    STRATEGY_LABELS,
    STRATEGY_TRANSFER,
    StrategyConfig,
    guadao_scope_allows_item,
    looks_like_weapon_case_name,
    normalize_guadao_item_scope,
)
from cs2_assistant.services.market import (
    DEFAULT_C5_SETTLEMENT_FACTOR,
    DEFAULT_STEAM_BALANCE_DISCOUNT,
    calculate_ratio,
    calculate_t_yield_rate,
)
from cs2_assistant.services.strategy import (
    load_strategy_config,
    save_strategy_config,
    scan_strategies,
)
from cs2_assistant.services.executor_engine import ExecutionEngine
from cs2_assistant.services.pricing import (
    clear_pricing_cache,
    fetch_listing_price,
    get_pricing_cache_snapshot,
)
from cs2_assistant.services.t_yield_alerts import build_t_yield_notification
from cs2_assistant.services.t_yield_scan import (
    fetch_all_c5_inventories,
    INVENTORY_FILTER_ALL,
    INVENTORY_FILTER_ALL_COOLDOWN,
    INVENTORY_FILTER_COOLDOWN_ONLY,
    INVENTORY_FILTER_HAS_TRADABLE,
    INVENTORY_FILTER_MIXED_ONLY,
    INVENTORY_FILTER_TRADABLE_ONLY,
    inventory_filter_label,
    load_missing_steam_report,
    normalize_inventory_filter,
    scan_t_yield,
)
from cs2_assistant.utils import ensure_parent_dir, safe_float, safe_int, utc_now_iso


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    if getattr(args, "db_path", None):
        settings.db_path = Path(args.db_path)
    return settings


def _open_db(settings: Settings) -> Database:
    db = Database(settings.db_path)
    db.initialize()
    return db


def _account_store(_: Settings | None = None) -> AccountStore:
    return AccountStore(PROJECT_ROOT / "config")


def _build_steam_client(settings: Settings) -> SteamMarketClient:
    store = _account_store(settings)
    current = store.get_current()
    cookies = (current.cookies if current else None) or settings.steam_cookies
    identity_secret = (current.identity_secret if current else None) or settings.steam_identity_secret
    device_id = (current.device_id if current else None) or settings.steam_device_id
    steam_id64 = (current.steam_id64 if current else None) or None
    if not cookies:
        raise RuntimeError("missing STEAM_COOKIES")
    return SteamMarketClient(
        cookies=cookies,
        steam_id64=steam_id64,
        identity_secret=identity_secret,
        device_id=device_id,
        account_id=current.id if current else None,
        base_url=settings.steam_market_base_url,
    )


def _print_json(payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        print(serialized)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(serialized.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


def _resolve_c5_steam_id(client: C5GameClient, provided_steam_id: str | None) -> str:
    if provided_steam_id:
        return provided_steam_id

    info = client.steam_info()
    direct_steam_id = str(info.get("steamId") or "").strip()
    if direct_steam_id:
        return direct_steam_id

    steam_list = info.get("steamList") or []
    if isinstance(steam_list, list):
        preferred_accounts = sorted(
            (account for account in steam_list if isinstance(account, dict)),
            key=lambda account: 0 if account.get("autoType") == 2 else 1,
        )
        for account in preferred_accounts:
            steam_id = str(account.get("steamId") or "").strip()
            if steam_id:
                return steam_id

    raise RuntimeError("未能从 C5 账号信息里解析到 Steam ID，请手动传入 --steam-id")


def _list_c5_steam_accounts(client: C5GameClient) -> list[dict[str, Any]]:
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


def _inventory_cache_path(settings: Settings) -> Path:
    return settings.db_path.parent / "c5_inventory_all_cache.json"


def _load_inventory_cache(settings: Settings) -> dict[str, Any] | None:
    cache_path = _inventory_cache_path(settings)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
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


def _fetch_all_c5_inventories(
    client: C5GameClient,
    settings: Settings,
    *,
    allow_cached_fallback: bool = False,
) -> dict[str, Any]:
    try:
        accounts = _list_c5_steam_accounts(client)
    except Exception:
        if allow_cached_fallback:
            cached_payload = _load_inventory_cache(settings)
            if cached_payload is not None:
                return cached_payload
        raise
    if not accounts:
        if allow_cached_fallback:
            cached_payload = _load_inventory_cache(settings)
            if cached_payload is not None:
                return cached_payload
        raise RuntimeError("未找到绑定的 Steam 账号。")

    inventories: list[dict[str, Any]] = []
    merged_items: list[dict[str, Any]] = []
    total = 0

    try:
        for account in accounts:
            steam_id = str(account["steamId"])
            inventory = client.inventory(steam_id, app_id=settings.app_id)
            items = inventory.get("list") or []
            if not isinstance(items, list):
                items = []
            inventory_total = inventory.get("total")
            total += inventory_total if isinstance(inventory_total, int) else len(items)
            inventories.append(
                {
                    "steamId": steam_id,
                    "nickname": account.get("nickname"),
                    "username": account.get("username"),
                    "autoType": account.get("autoType"),
                    "total": inventory_total if inventory_total is not None else len(items),
                    "list": items,
                }
            )
            for item in items:
                if isinstance(item, dict):
                    enriched_item = dict(item)
                    enriched_item.setdefault("steamId", steam_id)
                    enriched_item.setdefault("steamNickname", account.get("nickname"))
                    merged_items.append(enriched_item)
    except Exception:
        if allow_cached_fallback:
            cached_payload = _load_inventory_cache(settings)
            if cached_payload is not None:
                return cached_payload
        raise

    payload = {
        "source": "live",
        "accountCount": len(accounts),
        "total": total,
        "accounts": accounts,
        "inventories": inventories,
        "list": merged_items,
    }
    _save_inventory_cache(settings, payload)
    return payload


def _summarize_inventory_types(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        summaries.append(
            {
                "market_hash_name": summary["market_hash_name"],
                "name_cn": summary["name_cn"],
                "inventory_count": summary["inventory_count"],
                "tradable_count": summary["tradable_count"],
                "steam_ids": sorted(summary["steam_ids"]),
                "c5_item_id": summary["c5_item_id"],
                "reference_price": summary["reference_price"],
            }
        )
    summaries.sort(key=lambda row: row["market_hash_name"])
    return summaries


def _build_market_service(
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


def _refresh_items_for_t_yield(
    settings: Settings,
    items: list[dict[str, Any]],
) -> list[Any]:
    market_service = _build_market_service(settings, include_c5_purchase_prices=False)
    return market_service.refresh_items(items)


def _format_t_yield_top_rows(
    rankings: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    top_n: int,
) -> list[dict[str, Any]]:
    account_lookup = {
        str(account.get("steamId") or "").strip(): account.get("nickname")
        for account in accounts
    }
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(rankings[:top_n], start=1):
        rows.append(
            {
                "rank": index,
                "name": row["name"],
                "marketHashName": row["marketHashName"],
                "steamAccounts": [
                    {
                        "steamId": steam_id,
                        "nickname": account_lookup.get(steam_id),
                    }
                    for steam_id in row["steamIds"]
                ],
                "tYieldPct": f"{row['tYieldPct']:.2f}%",
                "ratio": f"{row['ratio']:.4f}",
                "listingRatio": f"{row['listingRatio']:.4f}" if row.get("listingRatio") is not None else "-",
                "c5LowestSellPrice": row["c5SellPrice"],
                "steamLowestSellPrice": row["steamSellPrice"],
                "c5PriceSource": row["c5PriceSource"],
                "steamPriceSource": row["steamPriceSource"],
            }
        )
    return rows


def _t_yield_missing_steam_path(settings: Settings) -> Path:
    return settings.db_path.parent / "c5_t_yield_missing_steam_prices.json"


def _load_t_yield_missing_steam_report(settings: Settings) -> dict[str, Any]:
    path = _t_yield_missing_steam_path(settings)
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


def _save_t_yield_missing_steam_report(settings: Settings, items: list[dict[str, Any]]) -> Path:
    path = _t_yield_missing_steam_path(settings)
    ensure_parent_dir(path)
    path.write_text(
        json.dumps(
            {
                "updatedAt": utc_now_iso(),
                "itemCount": len(items),
                "items": items,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _configured_steam_sources(settings: Settings) -> list[str]:
    sources: list[str] = []
    if settings.steamdt_api_key:
        sources.append("steamdt")
    if settings.csqaq_api_token:
        sources.append("csqaq")
    return sources


def _build_market_price_gap_rows(
    states: list[Any],
    *,
    attempted_sources: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for state in states:
        if state.c5_sell_price is None or state.steam_sell_price is not None:
            continue
        rows.append(
            {
                "marketHashName": state.market_hash_name,
                "name": state.name_cn or state.market_hash_name,
                "c5SellPrice": state.c5_sell_price,
                "c5PriceSource": state.c5_price_source,
                "steamPriceSource": state.steam_price_source,
                "steamSourcesAttempted": attempted_sources,
            }
        )
    rows.sort(key=lambda row: row["marketHashName"])
    return rows


def _build_t_yield_report(
    settings: Settings,
    *,
    top_n: int,
    min_price: float,
    steam_discount: float,
) -> dict[str, Any]:
    c5_client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    inventory_payload = _fetch_all_c5_inventories(c5_client, settings, allow_cached_fallback=True)
    inventory_types = _summarize_inventory_types(inventory_payload["list"])
    if not inventory_types:
        missing_path = _save_t_yield_missing_steam_report(settings, [])
        return {
            "accounts": inventory_payload["accounts"],
            "inventoryTypeCount": 0,
            "rankings": [],
            "formattedRows": [],
            "missingSteamPrices": [],
            "missingSteamPricePath": str(missing_path),
        }

    states = _refresh_items_for_t_yield(settings, inventory_types)
    state_map = {state.market_hash_name: state for state in states}
    account_lookup = {
        str(account.get("steamId") or "").strip(): account.get("nickname")
        for account in inventory_payload["accounts"]
    }
    attempted_sources = _configured_steam_sources(settings)

    rankings: list[dict[str, Any]] = []
    missing_steam_prices: list[dict[str, Any]] = []

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

        if state.steam_sell_price is None:
            missing_steam_prices.append(
                {
                    "name": state.name_cn or item_type["name_cn"],
                    "marketHashName": item_type["market_hash_name"],
                    "inventoryCount": item_type["inventory_count"],
                    "tradableCount": item_type["tradable_count"],
                    "c5SellPrice": c5_sell_price,
                    "c5PriceSource": c5_price_source,
                    "steamPriceSource": state.steam_price_source,
                    "steamSourcesAttempted": attempted_sources,
                    "steamAccounts": [
                        {
                            "steamId": steam_id,
                            "nickname": account_lookup.get(steam_id),
                        }
                        for steam_id in item_type["steam_ids"]
                    ],
                }
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

        rankings.append(
            {
                "marketHashName": item_type["market_hash_name"],
                "name": state.name_cn or item_type["name_cn"],
                "inventoryCount": item_type["inventory_count"],
                "tradableCount": item_type["tradable_count"],
                "steamAccountCount": len(item_type["steam_ids"]),
                "steamIds": item_type["steam_ids"],
                "c5ItemId": state.c5_item_id or item_type["c5_item_id"],
                "c5SellPrice": c5_sell_price,
                "c5PriceSource": c5_price_source or "unknown",
                "steamSellPrice": state.steam_sell_price,
                "steamPriceSource": state.steam_price_source or "unknown",
                "ratio": ratio,
                "tYieldRate": t_yield_rate,
                "tYieldPct": t_yield_rate * 100,
            }
        )

    rankings.sort(key=lambda row: row["tYieldRate"], reverse=True)
    missing_steam_prices.sort(key=lambda row: row["marketHashName"])
    missing_path = _save_t_yield_missing_steam_report(settings, missing_steam_prices)
    return {
        "accounts": inventory_payload["accounts"],
        "inventoryTypeCount": len(inventory_types),
        "rankings": rankings,
        "formattedRows": _format_t_yield_top_rows(rankings, inventory_payload["accounts"], top_n),
        "missingSteamPrices": missing_steam_prices,
        "missingSteamPricePath": str(missing_path),
    }


def _warn_missing_t_yield_steam_prices(report: dict[str, Any]) -> None:
    missing_items = report["missingSteamPrices"]
    if not missing_items:
        return
    print(
        (
            f"提示: 有 {len(missing_items)} 个库存饰品存在 C5 价格但缺少 Steam 价格；"
            f"已写入 {report['missingSteamPricePath']}；"
            "可运行 `python .\\main.py c5-t-yield-missing-steam` 查看。"
        ),
        file=sys.stderr,
    )


def _require_item(db: Database, market_hash_name: str) -> None:
    if db.get_item(market_hash_name) is None:
        raise ValueError(f"Item not found in catalog: {market_hash_name}. Please run import-catalog first.")


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings):
        print(f"数据库已初始化: {settings.db_path}")
    return 0


def cmd_import_catalog(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    source_path = Path(args.file) if args.file else settings.steamdt_base_path
    items = load_steamdt_catalog(source_path)
    with _open_db(settings) as db:
        count = db.upsert_items(items)
    print(f"已导入 {count} 条饰品基础数据: {source_path}")
    return 0


def cmd_catalog_sync_csgo_api(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    result = load_csgo_api_catalog(
        language=args.language,
        categories=args.categories,
        base_url=args.base_url,
        timeout=args.timeout,
    )
    with _open_db(settings) as db:
        count = db.upsert_items(result.items, preserve_existing_ids=True)

    categories_text = ", ".join(
        f"{category}={item_count}" for category, item_count in result.category_counts.items()
    )
    print(f"已从 CSGO-API 同步 {count} 条饰品资料，语言={result.language}")
    print(f"分类明细: {categories_text}")
    print(f"箱子识别资料: CSGO-API crates 分类共 {result.weapon_case_count} 个")
    print("已保留本地已有 c5_item_id / steam_item_id，不会覆盖成空。")
    return 0


def cmd_search_item(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        rows = db.search_items(args.keyword, limit=args.limit)
    if not rows:
        print("没有找到匹配的饰品。")
        return 0
    for row in rows:
        c5_item_id = row["c5_item_id"] or "-"
        print(f"{row['name_cn']} | {row['market_hash_name']} | C5 itemId={c5_item_id}")
    return 0


def cmd_watch_add(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        _require_item(db, args.market_hash_name)
        db.add_watch_item(
            args.market_hash_name,
            display_name=args.display_name,
            note=args.note,
        )
    print(f"已加入监控: {args.market_hash_name}")
    return 0


def cmd_watch_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        rows = db.list_watch_items(enabled_only=not args.all)
    if not rows:
        print("当前没有监控项。")
        return 0
    for row in rows:
        status = "enabled" if int(row["enabled"]) == 1 else "disabled"
        print(f"{row['display_name']} | {row['market_hash_name']} | {status}")
    return 0


def cmd_basket_add(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        db.add_basket(args.name, note=args.note)
    print(f"已创建篮子: {args.name}")
    return 0


def cmd_basket_add_item(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        _require_item(db, args.market_hash_name)
        db.add_basket_item(args.basket_name, args.market_hash_name, quantity=args.quantity)
    print(f"已加入篮子: {args.basket_name} -> {args.market_hash_name} x {args.quantity}")
    return 0


def cmd_basket_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        baskets = db.list_baskets()
        items = db.list_basket_items(args.basket_name)
    if not baskets:
        print("当前没有篮子。")
        return 0
    grouped: dict[str, list[str]] = {}
    for item in items:
        grouped.setdefault(item["basket_name"], []).append(
            f"{item['name_cn']} ({item['market_hash_name']}) x {item['quantity']}"
        )
    for basket in baskets:
        if args.basket_name and basket["name"] != args.basket_name:
            continue
        print(f"[{basket['name']}]")
        for line in grouped.get(basket["name"], []):
            print(f"  - {line}")
    return 0


def cmd_position_add(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        _require_item(db, args.market_hash_name)
        position_id = db.add_position(
            args.market_hash_name,
            status=args.status,
            quantity=args.quantity,
            manual_cost=args.manual_cost,
            target_buy_price=args.target_buy_price,
            target_sell_price=args.target_sell_price,
            note=args.note,
        )
    print(f"已新增仓位记录: id={position_id}")
    return 0


def cmd_position_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        rows = db.list_positions()
    if not rows:
        print("当前没有仓位记录。")
        return 0
    for row in rows:
        print(
            f"#{row['id']} | {row['name_cn']} | status={row['status']} | qty={row['quantity']} | "
            f"cost={row['manual_cost']} | buy={row['target_buy_price']} | sell={row['target_sell_price']}"
        )
    return 0


def cmd_rule_add(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        if args.target_type == "item":
            _require_item(db, args.target_key)
        rule_id = db.add_alert_rule(
            target_type=args.target_type,
            target_key=args.target_key,
            metric=args.metric,
            operator=args.operator,
            threshold=args.threshold,
            anchor_value=args.anchor_value,
            cooldown_minutes=args.cooldown_minutes,
            note=args.note,
        )
    print(f"已新增提醒规则: id={rule_id}")
    return 0


def cmd_rule_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with _open_db(settings) as db:
        rows = db.list_alert_rules(enabled_only=not args.all)
    if not rows:
        print("当前没有提醒规则。")
        return 0
    for row in rows:
        print(
            f"#{row['id']} | {row['target_type']}:{row['target_key']} | "
            f"{row['metric']} {row['operator']} {row['threshold']} | "
            f"anchor={row['anchor_value']} | cooldown={row['cooldown_minutes']}m"
        )
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.serverchan_sendkey:
        raise RuntimeError("缺少 SERVERCHAN_SENDKEY / SCTKEY 环境变量。")
    notifier = ServerChanClient(
        settings.serverchan_sendkey,
        base_url=settings.serverchan_base_url,
    )
    payload = notifier.send(args.title, args.message)
    _print_json(payload)
    return 0


def cmd_check_market(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.steamdt_api_key and not settings.c5_api_key and not settings.csqaq_api_token:
        raise RuntimeError("至少需要配置 STEAMDT_API_KEY、C5GAME_API_KEY、CSQAQ_API_KEY 中的一项。")

    with _open_db(settings) as db:
        watch_rows = db.list_watch_items(enabled_only=True)
        required_names = db.list_required_market_hash_names()
        if not required_names:
            print("当前没有监控项或篮子成分，请先添加 watch-item 或 basket-item。")
            return 0
        item_rows = [dict(db.get_item(name)) for name in required_names if db.get_item(name) is not None]

        market_service = _build_market_service(settings, include_c5_purchase_prices=True)
        item_states = market_service.refresh_items(item_rows)
        alert_service = AlertService(db)
        basket_states = alert_service.build_baskets(item_states)
        alerts = alert_service.evaluate(item_states, basket_states)

    missing_steam_prices = _build_market_price_gap_rows(
        item_states,
        attempted_sources=_configured_steam_sources(settings),
    )
    print(
        f"已检查 {len(item_states)} 个饰品，{len(basket_states)} 个篮子，"
        f"触发 {len(alerts)} 条提醒。"
    )
    if missing_steam_prices:
        print(
            f"提示: {len(missing_steam_prices)} 个监控标的有 C5 价格但缺少 Steam 价格，比值相关规则可能被跳过。",
            file=sys.stderr,
        )
    for alert in alerts:
        print(f"- {alert.message}")

    if args.notify and not settings.serverchan_sendkey:
        raise RuntimeError("缺少 SERVERCHAN_SENDKEY / SCTKEY 环境变量。")

    if alerts and args.notify:
        notification_service = NotificationService(
            ServerChanClient(
                settings.serverchan_sendkey,
                base_url=settings.serverchan_base_url,
            )
        )
        notification_service.send(NotificationService.build_rule_alert_message(alerts))
        print("已发送 ServerChan 提醒。")
    elif alerts:
        print("当前为仅生成提醒模式；如需推送，请追加 --notify。")

    if args.dump_json:
        payload = {
            "watch_items": [dict(row) for row in watch_rows],
            "states": [
                {
                    "marketHashName": state.market_hash_name,
                    "name": state.name_cn,
                    "c5SellPrice": state.c5_sell_price,
                    "c5BidPrice": state.c5_bid_price,
                    "steamSellPrice": state.steam_sell_price,
                    "ratio": state.ratio,
                    "tYieldRate": calculate_t_yield_rate(state.ratio),
                    "tYieldPct": calculate_t_yield_rate(state.ratio) * 100
                    if calculate_t_yield_rate(state.ratio) is not None
                    else None,
                }
                for state in item_states
            ],
            "baskets": [
                {
                    "name": basket.name,
                    "totalValue": basket.total_value,
                    "components": basket.components,
                }
                for basket in basket_states
            ],
            "alerts": [
                {
                    "ruleId": alert.rule_id,
                    "message": alert.message,
                }
                for alert in alerts
            ],
            "missingSteamPrices": missing_steam_prices,
        }
        _print_json(payload)
    return 0


def cmd_c5_quick_buy(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    client = C5GameClient(settings.c5_api_key, settings.c5_base_url)

    payload = {
        "market_hash_name": args.market_hash_name,
        "item_id": args.item_id,
        "max_price": args.max_price,
        "delivery": args.delivery,
        "low_price": args.low_price,
        "out_trade_no": args.out_trade_no or uuid.uuid4().hex[:24],
    }
    print("即将调用 C5 快速购买：")
    _print_json(payload)

    if not args.yes:
        confirm = input("输入 YES 确认下单，其它任意键取消: ")
        if confirm != "YES":
            print("已取消。")
            return 0

    result = client.quick_buy(
        app_id=settings.app_id,
        market_hash_name=args.market_hash_name,
        item_id=args.item_id,
        max_price=args.max_price,
        delivery=args.delivery,
        low_price=args.low_price,
        out_trade_no=payload["out_trade_no"],
    )
    _print_json(result)
    return 0


def cmd_c5_sales(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    steam_id = _resolve_c5_steam_id(client, args.steam_id)
    result = client.sale_search(
        app_id=settings.app_id,
        steam_id=steam_id,
        delivery=args.delivery,
        page=args.page,
        limit=args.limit,
    )
    _print_json(result)
    return 0


def cmd_c5_steam_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if args.min_price < 0:
        raise ValueError("--min-price 不能小于 0")
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    payload = {"accounts": _list_c5_steam_accounts(client)}
    _print_json(payload)
    return 0


def cmd_c5_inventory(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    steam_id = _resolve_c5_steam_id(client, args.steam_id)
    result = client.inventory(steam_id, app_id=settings.app_id)
    _print_json(result)
    return 0


def cmd_c5_inventory_all(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    payload = _fetch_all_c5_inventories(client, settings, allow_cached_fallback=True)
    _print_json(payload)
    return 0


def cmd_t_yield_scan(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    report = scan_t_yield(
        settings,
        min_price=args.min_price,
        steam_discount=args.steam_discount,
        allow_cached_fallback=not args.no_cache_fallback,
        cache_max_age_minutes=args.cache_max_age_minutes,
        inventory_filter=args.inventory_filter,
    )
    output_mode = "bottom" if args.bottom is not None else "top"
    output_count = args.bottom if args.bottom is not None else args.top

    if report.inventory_filter == INVENTORY_FILTER_ALL:
        inventory_summary = f"已扫描 {report.inventory_type_count} 个库存饰品类型，"
    else:
        inventory_summary = (
            f"已扫描 {report.inventory_type_total_count} 个库存饰品类型，"
            f"筛选后 {report.inventory_type_count} 个（{report.inventory_filter_label}），"
        )
    print(
        inventory_summary
        + f"命中 {len(report.candidates)} 个做T候选，"
        + f"缺少 Steam 价格 {len(report.missing_steam_prices)} 个。"
    )

    top_candidates = report.candidates[: args.top]
    if not top_candidates:
        print("当前没有符合条件的做T候选。")
    else:
        for index, candidate in enumerate(top_candidates, start=1):
            accounts = ", ".join(
                account.nickname or account.steam_id
                for account in candidate.steam_accounts
            ) or "-"
            marker = "★" if candidate.t_yield_pct >= args.star_threshold else "-"
            print(
                f"{marker} {index}. {candidate.name} | 收益率 {candidate.t_yield_pct:.2f}% | "
                f"{candidate.inventory_status_summary} | 挂刀比例 {candidate.ratio:.4f} | "
                f"C5 {candidate.c5_lowest_sell_price:.2f} | "
                f"Steam {candidate.steam_lowest_sell_price:.2f} | 账号 {accounts}"
            )

    if report.missing_steam_prices:
        print(f"缺少 Steam 价格的饰品: {len(report.missing_steam_prices)} 个")
        for issue in report.missing_steam_prices[:10]:
            print(
                f"- {issue.name} | {issue.inventory_status_summary} | C5 {issue.c5_sell_price:.2f} | "
                f"HashName={issue.market_hash_name}"
            )
        print(f"详情文件: {report.missing_steam_price_path}")

    if args.dump_json:
        _print_json(
            {
                "generatedAt": report.generated_at,
                "inventorySource": report.inventory_source,
                "inventoryCachedAt": report.inventory_cached_at,
                "inventoryFilter": report.inventory_filter,
                "inventoryFilterLabel": report.inventory_filter_label,
                "inventoryTypeTotalCount": report.inventory_type_total_count,
                "inventoryTypeCount": report.inventory_type_count,
                "rows": report.top_rows(args.top),
                "missingSteamPrices": [issue.to_dict() for issue in report.missing_steam_prices],
                "missingSteamPricePath": report.missing_steam_price_path,
            }
        )
    return 0


def cmd_t_yield_missing_steam_v2(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    _print_json(load_missing_steam_report(settings))
    return 0


def cmd_notify_t_yield(args: argparse.Namespace) -> int:
    reminder_argv: list[str] = []
    if args.configure:
        reminder_argv.append("--configure")
    if args.once:
        reminder_argv.append("--once")
    if args.show_config:
        reminder_argv.append("--show-config")
    if args.show_missing_steam:
        reminder_argv.append("--show-missing-steam")
    return int(t_yield_reminder_main(reminder_argv))


def cmd_c5_t_yield_top(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if args.top <= 0:
        raise ValueError("--top 必须是正整数")
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    if not settings.steamdt_api_key:
        raise RuntimeError("缺少 STEAMDT_API_KEY 环境变量。")

    c5_client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    inventory_payload = _fetch_all_c5_inventories(c5_client, settings, allow_cached_fallback=True)
    inventory_types = _summarize_inventory_types(inventory_payload["list"])
    if not inventory_types:
        _print_json([])
        return 0

    states = _refresh_items_for_t_yield(settings, inventory_types)
    state_map = {state.market_hash_name: state for state in states}

    rankings: list[dict[str, Any]] = []
    for item_type in inventory_types:
        state = state_map.get(item_type["market_hash_name"])
        if state is None:
            continue
        c5_sell_price = state.c5_sell_price or item_type["reference_price"]
        ratio = calculate_ratio(
            c5_sell_price,
            state.steam_sell_price,
            c5_settlement_factor=DEFAULT_C5_SETTLEMENT_FACTOR,
        )
        t_yield_rate = calculate_t_yield_rate(
            ratio,
            steam_balance_discount=args.steam_discount,
            c5_settlement_factor=DEFAULT_C5_SETTLEMENT_FACTOR,
        )
        if t_yield_rate is None:
            continue
        rankings.append(
            {
                "marketHashName": item_type["market_hash_name"],
                "name": state.name_cn or item_type["name_cn"],
                "inventoryCount": item_type["inventory_count"],
                "tradableCount": item_type["tradable_count"],
                "steamAccountCount": len(item_type["steam_ids"]),
                "steamIds": item_type["steam_ids"],
                "c5ItemId": state.c5_item_id or item_type["c5_item_id"],
                "c5SellPrice": c5_sell_price,
                "c5PriceSource": "inventory_price",
                "steamSellPrice": state.steam_sell_price,
                "ratio": ratio,
                "tYieldRate": t_yield_rate,
                "tYieldPct": t_yield_rate * 100,
            }
        )

    rankings.sort(key=lambda row: row["tYieldRate"], reverse=True)
    _print_json(_format_t_yield_top_rows(rankings, inventory_payload["accounts"], args.top))
    return 0


def cmd_c5_steam_list_safe(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    payload = {"accounts": _list_c5_steam_accounts(client)}
    _print_json(payload)
    return 0


def cmd_c5_t_yield_top_v2(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if args.top <= 0:
        raise ValueError("--top 必须是正整数")
    if args.min_price < 0:
        raise ValueError("--min-price 不能小于 0")
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    if not settings.steamdt_api_key and not settings.csqaq_api_token:
        raise RuntimeError("缺少 STEAMDT_API_KEY 或 CSQAQ_API_KEY / CSQAQ_API_TOKEN 环境变量。")

    report = _build_t_yield_report(
        settings,
        top_n=args.top,
        min_price=args.min_price,
        steam_discount=args.steam_discount,
    )
    _warn_missing_t_yield_steam_prices(report)
    _print_json(report["formattedRows"])
    return 0


def cmd_c5_t_yield_missing_steam(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    _print_json(_load_t_yield_missing_steam_report(settings))
    return 0


def cmd_c5_t_yield_alert(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if args.top <= 0:
        raise ValueError("--top 必须是正整数")
    if args.min_price < 0:
        raise ValueError("--min-price 不能小于 0")
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    if not settings.steamdt_api_key and not settings.csqaq_api_token:
        raise RuntimeError("缺少 STEAMDT_API_KEY 或 CSQAQ_API_KEY / CSQAQ_API_TOKEN 环境变量。")
    if args.notify and not settings.serverchan_sendkey:
        raise RuntimeError("缺少 SERVERCHAN_SENDKEY / SCTKEY 环境变量。")

    report = _build_t_yield_report(
        settings,
        top_n=args.top,
        min_price=args.min_price,
        steam_discount=args.steam_discount,
    )
    _warn_missing_t_yield_steam_prices(report)
    notification = build_t_yield_notification(
        report["formattedRows"],
        top_n=args.top,
        min_price=args.min_price,
        missing_steam_prices=report["missingSteamPrices"],
    )

    print(
        f"已扫描 {report['inventoryTypeCount']} 个库存饰品类型，"
        f"命中 {len(report['formattedRows'])} 个做T候选，"
        f"缺少 Steam 价格 {len(report['missingSteamPrices'])} 个。"
    )

    if args.notify:
        notification_service = NotificationService(
            ServerChanClient(
                settings.serverchan_sendkey,
                base_url=settings.serverchan_base_url,
            )
        )
        notification_service.send(notification)
        print("已发送 ServerChan 做T提醒。")
    else:
        print("当前为仅生成提醒模式；如需推送，请追加 --notify。")

    if args.dump_json:
        _print_json(
            {
                "rows": report["formattedRows"],
                "missingSteamPrices": report["missingSteamPrices"],
                "missingSteamPricePath": report["missingSteamPricePath"],
                "notification": {
                    "title": notification.title,
                    "body": notification.body,
                },
            }
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CS2 理财助手 CLI",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--db-path", help="自定义 SQLite 数据库路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="初始化数据库")
    init_db.set_defaults(handler=cmd_init_db)

    import_catalog = subparsers.add_parser("import-catalog", help="导入本地 SteamDT 基础数据")
    import_catalog.add_argument("--file", help="SteamDT 基础数据 JSON 文件路径")
    import_catalog.set_defaults(handler=cmd_import_catalog)

    catalog = subparsers.add_parser("catalog", help="饰品资料库")
    catalog_subparsers = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_sync_csgo_api = catalog_subparsers.add_parser(
        "sync-csgo-api",
        help="从 ByMykel/CSGO-API 同步饰品资料",
        description=(
            "从 ByMykel/CSGO-API 同步饰品资料到本地 items 表。\n"
            "默认同步常用可交易品类，并保留本地已有 c5_item_id / steam_item_id。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    catalog_sync_csgo_api.add_argument("--language", default=CSGO_API_DEFAULT_LANGUAGE, help="CSGO-API 语言目录")
    catalog_sync_csgo_api.add_argument("--base-url", default=CSGO_API_BASE_URL, help=argparse.SUPPRESS)
    catalog_sync_csgo_api.add_argument("--timeout", type=float, default=30.0, help="单个分类请求超时秒数")
    catalog_sync_csgo_api.add_argument(
        "--category",
        dest="categories",
        action="append",
        help="只同步指定分类；可重复传入。默认同步常用品类。",
    )
    catalog_sync_csgo_api.set_defaults(handler=cmd_catalog_sync_csgo_api)

    search_item = subparsers.add_parser("search-item", help="按关键词搜索饰品")
    search_item.add_argument("--keyword", required=True, help="中文名或 HashName 关键词")
    search_item.add_argument("--limit", type=int, default=20, help="返回条数")
    search_item.set_defaults(handler=cmd_search_item)

    watch_add = subparsers.add_parser("watch-add", help="加入单品监控")
    watch_add.add_argument("--market-hash-name", required=True)
    watch_add.add_argument("--display-name")
    watch_add.add_argument("--note")
    watch_add.set_defaults(handler=cmd_watch_add)

    watch_list = subparsers.add_parser("watch-list", help="查看监控列表")
    watch_list.add_argument("--all", action="store_true", help="包含禁用项")
    watch_list.set_defaults(handler=cmd_watch_list)

    basket_add = subparsers.add_parser("basket-add", help="创建篮子")
    basket_add.add_argument("--name", required=True)
    basket_add.add_argument("--note")
    basket_add.set_defaults(handler=cmd_basket_add)

    basket_add_item = subparsers.add_parser("basket-add-item", help="向篮子加入饰品")
    basket_add_item.add_argument("--basket-name", required=True)
    basket_add_item.add_argument("--market-hash-name", required=True)
    basket_add_item.add_argument("--quantity", type=float, default=1.0)
    basket_add_item.set_defaults(handler=cmd_basket_add_item)

    basket_list = subparsers.add_parser("basket-list", help="查看篮子")
    basket_list.add_argument("--basket-name")
    basket_list.set_defaults(handler=cmd_basket_list)

    position_add = subparsers.add_parser("position-add", help="新增人工仓位记录")
    position_add.add_argument("--market-hash-name", required=True)
    position_add.add_argument("--status", required=True)
    position_add.add_argument("--quantity", type=float, default=0)
    position_add.add_argument("--manual-cost", type=float)
    position_add.add_argument("--target-buy-price", type=float)
    position_add.add_argument("--target-sell-price", type=float)
    position_add.add_argument("--note")
    position_add.set_defaults(handler=cmd_position_add)

    position_list = subparsers.add_parser("position-list", help="查看仓位记录")
    position_list.set_defaults(handler=cmd_position_list)

    rule_add = subparsers.add_parser("rule-add", help="新增提醒规则")
    rule_add.add_argument("--target-type", choices=["item", "basket"], required=True)
    rule_add.add_argument("--target-key", required=True, help="item 用 HashName，basket 用篮子名")
    rule_add.add_argument(
        "--metric",
        choices=[
            "c5_price",
            "steam_price",
            "c5_bid_price",
            "ratio",
            "basket_total",
            "c5_change_pct",
            "steam_change_pct",
            "basket_change_pct",
        ],
        required=True,
    )
    rule_add.add_argument("--operator", choices=["lte", "gte"], required=True)
    rule_add.add_argument("--threshold", type=float, required=True)
    rule_add.add_argument("--anchor-value", type=float)
    rule_add.add_argument("--cooldown-minutes", type=int, default=60)
    rule_add.add_argument("--note")
    rule_add.set_defaults(handler=cmd_rule_add)

    rule_list = subparsers.add_parser("rule-list", help="查看提醒规则")
    rule_list.add_argument("--all", action="store_true")
    rule_list.set_defaults(handler=cmd_rule_list)

    notify_test = subparsers.add_parser("notify-test", help="发送一条 ServerChan 测试消息")
    notify_test.add_argument("--title", default="CS2 理财助手测试提醒")
    notify_test.add_argument("--message", default="如果你看到这条消息，说明 ServerChan 已经打通。")
    notify_test.set_defaults(handler=cmd_notify_test)

    check_market = subparsers.add_parser("check-market", help="采集价格并触发规则判断")
    check_market.add_argument("--notify", action="store_true", help="命中规则后通过 ServerChan 推送")
    check_market.add_argument("--dump-json", action="store_true", help="额外输出 JSON 结果")
    check_market.set_defaults(handler=cmd_check_market)

    c5_quick_buy = subparsers.add_parser("c5-quick-buy", help="C5 快速购买，需要用户确认")
    group = c5_quick_buy.add_mutually_exclusive_group(required=True)
    group.add_argument("--market-hash-name")
    group.add_argument("--item-id")
    c5_quick_buy.add_argument("--max-price", type=float)
    c5_quick_buy.add_argument("--delivery", type=int)
    c5_quick_buy.add_argument("--low-price", type=float)
    c5_quick_buy.add_argument("--out-trade-no")
    c5_quick_buy.add_argument("--yes", action="store_true", help="跳过二次确认")
    c5_quick_buy.set_defaults(handler=cmd_c5_quick_buy)

    c5_sales = subparsers.add_parser("c5-sales", help="查询当前 C5 在售列表")
    c5_sales.add_argument("--steam-id")
    c5_sales.add_argument("--delivery", type=int)
    c5_sales.add_argument("--page", type=int, default=1)
    c5_sales.add_argument("--limit", type=int, default=20)
    c5_sales.set_defaults(handler=cmd_c5_sales)

    c5_steam_list = subparsers.add_parser("c5-steam-list", help="列出 C5 绑定的 Steam 账号")
    c5_steam_list.set_defaults(handler=cmd_c5_steam_list_safe)

    c5_inventory = subparsers.add_parser("c5-inventory", help="查询单个 Steam 账号的 C5 库存")
    c5_inventory.add_argument("--steam-id")
    c5_inventory.set_defaults(handler=cmd_c5_inventory)

    c5_inventory_all = subparsers.add_parser("c5-inventory-all", help="汇总所有绑定 Steam 账号的 C5 库存")
    c5_inventory_all.set_defaults(handler=cmd_c5_inventory_all)

    t_yield = subparsers.add_parser(
        "t-yield",
        help="做T扫描与结果输出",
        description=(
            "做T扫描相关命令。\n\n"
            "常用：\n"
            "  python .\\main.py t-yield scan -h\n"
            "  python .\\main.py t-yield scan --top 10 --min-price 10 --inventory-filter all\n"
            "  python .\\main.py t-yield scan --inventory-filter tradable_only\n"
            "  python .\\main.py t-yield scan --inventory-filter cooldown_only\n"
            "  python .\\main.py t-yield scan --inventory-filter mixed_only\n"
            "  python .\\main.py t-yield missing-steam"
        ),
        epilog="提示：要看 scan 的完整参数，请执行 `python .\\main.py t-yield scan -h`。",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    t_yield_subparsers = t_yield.add_subparsers(dest="t_yield_command", required=True)

    t_yield_scan = t_yield_subparsers.add_parser(
        "scan",
        help="扫描全部库存并输出做T结果",
        description=(
            "扫描全部绑定 Steam 账号的 C5 库存，计算做T候选，并支持按库存状态筛选。\n\n"
            "inventory-filter 说明：\n"
            "  all: 全部库存状态\n"
            "  tradable_only: 仅不冷却\n"
            "  cooldown_only: 仅冷却\n"
            "  mixed_only: 同一个饰品类型里同时存在冷却和不冷却"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    t_yield_scan.add_argument("--top", type=int, default=10, help="输出前 N 个候选")
    t_yield_scan.add_argument("--min-price", type=float, default=10.0, help="只保留 C5 最低售价不低于该值的饰品")
    t_yield_scan.add_argument("--steam-discount", type=float, default=DEFAULT_STEAM_BALANCE_DISCOUNT)
    t_yield_scan.add_argument(
        "--inventory-filter",
        choices=[
            INVENTORY_FILTER_ALL,
            INVENTORY_FILTER_TRADABLE_ONLY,
            INVENTORY_FILTER_COOLDOWN_ONLY,
            INVENTORY_FILTER_MIXED_ONLY,
        ],
        default=INVENTORY_FILTER_ALL,
        help=(
            "库存筛选: "
            f"{INVENTORY_FILTER_ALL}={inventory_filter_label(INVENTORY_FILTER_ALL)}, "
            f"{INVENTORY_FILTER_TRADABLE_ONLY}={inventory_filter_label(INVENTORY_FILTER_TRADABLE_ONLY)}, "
            f"{INVENTORY_FILTER_COOLDOWN_ONLY}={inventory_filter_label(INVENTORY_FILTER_COOLDOWN_ONLY)}, "
            f"{INVENTORY_FILTER_MIXED_ONLY}={inventory_filter_label(INVENTORY_FILTER_MIXED_ONLY)}"
        ),
    )
    t_yield_scan.add_argument("--star-threshold", type=float, default=10.0, help="达到该收益率时在本地输出中标星")
    t_yield_scan.add_argument("--cache-max-age-minutes", type=int, default=180, help="允许使用的库存缓存最大时长")
    t_yield_scan.add_argument("--no-cache-fallback", action="store_true", help="库存拉取失败时不回退到缓存")
    t_yield_scan.add_argument("--dump-json", action="store_true", help="额外输出 JSON 结果")
    t_yield_scan.set_defaults(handler=cmd_t_yield_scan)

    t_yield_missing = t_yield_subparsers.add_parser("missing-steam", help="查看最近一次缺失 Steam 价格的明细")
    t_yield_missing.set_defaults(handler=cmd_t_yield_missing_steam_v2)

    notify = subparsers.add_parser(
        "notify",
        help="提醒模块入口",
        description=(
            "提醒模块入口。\n\n"
            "常用：\n"
            "  python .\\main.py notify t-yield -h\n"
            "  python .\\main.py notify t-yield --configure\n"
            "  python .\\main.py notify t-yield --show-config\n"
            "  python .\\main.py notify t-yield --once"
        ),
        epilog="提示：要看做T提醒的完整参数，请执行 `python .\\main.py notify t-yield -h`。",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    notify_subparsers = notify.add_subparsers(dest="notify_command", required=True)

    notify_t_yield = notify_subparsers.add_parser(
        "t-yield",
        help="做T提醒",
        description=(
            "做T提醒命令。\n\n"
            "说明：\n"
            "  --configure      重新配置提醒参数\n"
            "  --show-config    查看当前提醒配置\n"
            "  --show-missing-steam  查看最近一次 Steam 缺价明细\n"
            "  --once           仅执行一次扫描和提醒判断"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    notify_t_yield.add_argument("--configure", action="store_true", help="重新配置做T提醒参数")
    notify_t_yield.add_argument("--once", action="store_true", help="只执行一次提醒判断")
    notify_t_yield.add_argument("--show-config", action="store_true", help="输出当前提醒配置")
    notify_t_yield.add_argument("--show-missing-steam", action="store_true", help="输出最近一次缺失 Steam 价格的明细")
    notify_t_yield.set_defaults(handler=cmd_notify_t_yield)

    return parser


def cmd_t_profit_scan(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    report = scan_t_yield(
        settings,
        min_price=args.min_price,
        steam_discount=args.steam_discount,
        allow_cached_fallback=not args.no_cache_fallback,
        cache_max_age_minutes=args.cache_max_age_minutes,
        inventory_filter=args.inventory_filter,
    )
    output_mode = "bottom" if args.bottom is not None else "top"
    output_count = args.bottom if args.bottom is not None else args.top
    selected_candidates = (
        sorted(report.candidates, key=lambda candidate: candidate.t_yield_rate)[:output_count]
        if output_mode == "bottom"
        else report.candidates[:output_count]
    )

    if report.inventory_filter == INVENTORY_FILTER_ALL:
        inventory_summary = f"已扫描 {report.inventory_type_count} 个库存饰品类型，"
    else:
        inventory_summary = (
            f"已扫描 {report.inventory_type_total_count} 个库存饰品类型，"
            f"筛选后 {report.inventory_type_count} 个（{report.inventory_filter_label}），"
        )
    print(
        inventory_summary
        + f"命中 {len(report.candidates)} 个做T候选，"
        + f"缺少 Steam 价格 {len(report.missing_steam_prices)} 个。"
    )
    print(f"价格源: C5官方API / Steam官方orderbook | 库存账号 {len(report.accounts)} 个")

    if not selected_candidates:
        print("当前没有符合条件的做T候选。")
    else:
        print(f"输出模式: {'低收益优先' if output_mode == 'bottom' else '高收益优先'}，数量 {output_count}")
        for index, candidate in enumerate(selected_candidates, start=1):
            accounts = ", ".join(
                account.nickname or account.steam_id
                for account in candidate.steam_accounts
            ) or "-"
            marker = "★" if candidate.t_yield_pct >= args.star_threshold else "-"
            print(f"\n{marker} {index}. {candidate.name}")
            print(
                f"   利润 {candidate.t_yield_pct:.2f}% | "
                f"折算比 {candidate.ratio:.4f} | 挂刀比 {candidate.listing_ratio:.4f}"
            )
            print(f"   C5 {candidate.c5_lowest_sell_price:.2f} | Steam {candidate.steam_lowest_sell_price:.2f}")
            print(f"   库存 {candidate.inventory_status_summary}")
            print(f"   账号 {accounts}")

    if report.missing_steam_prices:
        print(f"\n缺少 Steam 价格: {len(report.missing_steam_prices)} 个")
        for issue in report.missing_steam_prices[:10]:
            print(f"- {issue.name}")
            print(f"  C5 {issue.c5_sell_price:.2f} | {issue.inventory_status_summary}")
            print(f"  HashName={issue.market_hash_name}")
        print(f"详情文件: {report.missing_steam_price_path}")

    if args.dump_json:
        _print_json(
            {
                "generatedAt": report.generated_at,
                "inventorySource": report.inventory_source,
                "inventoryCachedAt": report.inventory_cached_at,
                "inventoryFilter": report.inventory_filter,
                "inventoryFilterLabel": report.inventory_filter_label,
                "inventoryTypeTotalCount": report.inventory_type_total_count,
                "inventoryTypeCount": report.inventory_type_count,
                "sortMode": output_mode,
                "rows": report.bottom_rows(output_count) if output_mode == "bottom" else report.top_rows(output_count),
                "missingSteamPrices": [issue.to_dict() for issue in report.missing_steam_prices],
                "missingSteamPricePath": report.missing_steam_price_path,
            }
        )
    return 0


def cmd_notify_t_profit(args: argparse.Namespace) -> int:
    return cmd_notify_t_yield(args)


# ---------------------------------------------------------------------------
# Pool / strategy commands
# ---------------------------------------------------------------------------


POOL_REPORT_TZ = timezone(timedelta(hours=8))


def _parse_c5_tradable_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw_number = float(value)
        if raw_number <= 0:
            return None
        if raw_number > 10_000_000_000:
            raw_number /= 1000
        return datetime.fromtimestamp(raw_number, tz=timezone.utc)

    raw = str(value).strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        raw_number = float(raw)
        if raw_number <= 0:
            return None
        if raw_number > 10_000_000_000:
            raw_number /= 1000
        return datetime.fromtimestamp(raw_number, tz=timezone.utc)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_broad_weapon_case_name(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    if looks_like_weapon_case_name(value):
        return True
    return (
        normalized.endswith(" capsule")
        or normalized.endswith(" package")
        or normalized.endswith(" container")
        or "胶囊" in value
        or "收藏包" in value
    )


def _is_pool_scope_weapon_case(db: Database, item: dict[str, Any]) -> bool:
    market_hash_name = str(item.get("marketHashName") or "").strip()
    name = str(item.get("name") or item.get("shortName") or "").strip()
    catalog_item = db.get_item(market_hash_name) if market_hash_name else None
    if catalog_item is not None:
        raw_json = _read_note_dict(catalog_item["raw_json"])
        if isinstance(raw_json.get("csgoApi"), dict) and is_csgo_api_weapon_case(raw_json):
            return True
        if _is_broad_weapon_case_name(str(catalog_item["market_hash_name"] or "")):
            return True
        if _is_broad_weapon_case_name(str(catalog_item["name_cn"] or "")):
            return True
        if _is_broad_weapon_case_name(str(raw_json.get("marketHashName") or "")):
            return True
        if _is_broad_weapon_case_name(str(raw_json.get("name") or "")):
            return True
        type_name = str(raw_json.get("typeName") or raw_json.get("type") or "")
        if _is_broad_weapon_case_name(type_name) or "weaponcase" in type_name.lower():
            return True
    return _is_broad_weapon_case_name(market_hash_name) or _is_broad_weapon_case_name(name)


def _format_account_counts(counts: dict[str, int], account_lookup: dict[str, str]) -> str:
    if not counts:
        return "-"
    parts: list[str] = []
    for steam_id, count in sorted(counts.items(), key=lambda row: (-row[1], account_lookup.get(row[0], row[0]))):
        label = account_lookup.get(steam_id) or steam_id or "未知账号"
        parts.append(f"{label} {count}")
    return "、".join(parts)


def _format_status_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return "、".join(
        f"status={status} {count}"
        for status, count in sorted(counts.items(), key=lambda row: (str(row[0]), -int(row[1])))
    )


def _build_pool_inventory_report(
    inventory_payload: dict[str, Any],
    *,
    db: Database,
    scope: str,
    days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_scope = normalize_guadao_item_scope(scope)
    now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    today = now_utc.astimezone(POOL_REPORT_TZ).date()
    day_keys = [today + timedelta(days=offset) for offset in range(max(1, days))]
    day_set = set(day_keys)

    account_lookup = {
        str(account.get("steamId") or "").strip(): str(
            account.get("nickname") or account.get("username") or account.get("steamId") or ""
        ).strip()
        for account in list(inventory_payload.get("accounts") or [])
        if str(account.get("steamId") or "").strip()
    }

    grouped: dict[str, dict[str, Any]] = {}
    for item in list(inventory_payload.get("list") or []):
        if not isinstance(item, dict):
            continue
        if not guadao_scope_allows_item(
            normalized_scope,
            is_weapon_case=_is_pool_scope_weapon_case(db, item),
        ):
            continue
        market_hash_name = str(item.get("marketHashName") or "").strip()
        if not market_hash_name:
            continue
        row = grouped.setdefault(
            market_hash_name,
            {
                "marketHashName": market_hash_name,
                "name": item.get("name") or item.get("shortName") or market_hash_name,
                "totalCount": 0,
                "tradableCount": 0,
                "cooldownCount": 0,
                "tradableAccounts": {},
                "cooldownAccounts": {},
                "cooldownStatuses": {},
                "cooldownByDate": {day.isoformat(): {} for day in day_keys},
                "futureCooldownCount": 0,
                "unknownCooldownCount": 0,
                "outOfRangeCooldownCount": 0,
            },
        )
        row["totalCount"] += 1
        steam_id = str(item.get("steamId") or "").strip()
        if item.get("ifTradable") is True:
            row["tradableCount"] += 1
            row["tradableAccounts"][steam_id] = int(row["tradableAccounts"].get(steam_id, 0)) + 1
            continue

        row["cooldownCount"] += 1
        row["cooldownAccounts"][steam_id] = int(row["cooldownAccounts"].get(steam_id, 0)) + 1
        status_key = str(item.get("status") if item.get("status") is not None else "unknown")
        row["cooldownStatuses"][status_key] = int(row["cooldownStatuses"].get(status_key, 0)) + 1
        tradable_time = _parse_c5_tradable_time(item.get("tradableTime"))
        if tradable_time is None:
            row["unknownCooldownCount"] += 1
            continue
        local_day = tradable_time.astimezone(POOL_REPORT_TZ).date()
        if local_day in day_set:
            day_bucket = row["cooldownByDate"][local_day.isoformat()]
            day_bucket[steam_id] = int(day_bucket.get(steam_id, 0)) + 1
            row["futureCooldownCount"] += 1
        else:
            row["outOfRangeCooldownCount"] += 1

    rows = list(grouped.values())
    for row in rows:
        row["tradableAccountSummary"] = _format_account_counts(row["tradableAccounts"], account_lookup)
        row["cooldownAccountSummary"] = _format_account_counts(row["cooldownAccounts"], account_lookup)
        row["cooldownStatusSummary"] = _format_status_counts(row["cooldownStatuses"])
    rows.sort(key=lambda row: (-int(row["tradableCount"]), -int(row["totalCount"]), str(row["marketHashName"])))

    daily_rows: list[dict[str, Any]] = []
    for day in day_keys:
        item_rows: list[dict[str, Any]] = []
        day_key = day.isoformat()
        for row in rows:
            counts = dict(row["cooldownByDate"].get(day_key) or {})
            count = sum(int(value) for value in counts.values())
            if count <= 0:
                continue
            item_rows.append(
                {
                    "marketHashName": row["marketHashName"],
                    "name": row["name"],
                    "count": count,
                    "accountSummary": _format_account_counts(counts, account_lookup),
                }
            )
        item_rows.sort(key=lambda item: (-int(item["count"]), str(item["marketHashName"])))
        daily_rows.append({"date": day_key, "items": item_rows, "count": sum(int(item["count"]) for item in item_rows)})

    return {
        "generatedAt": utc_now_iso(),
        "source": inventory_payload.get("source") or "live",
        "cachedAt": inventory_payload.get("cachedAt"),
        "accountCount": int(inventory_payload.get("accountCount") or len(account_lookup)),
        "scope": normalized_scope,
        "days": len(day_keys),
        "totalTypes": len(rows),
        "totalCount": sum(int(row["totalCount"]) for row in rows),
        "tradableCount": sum(int(row["tradableCount"]) for row in rows),
        "cooldownCount": sum(int(row["cooldownCount"]) for row in rows),
        "futureCooldownCount": sum(int(row["futureCooldownCount"]) for row in rows),
        "unknownCooldownCount": sum(int(row["unknownCooldownCount"]) for row in rows),
        "outOfRangeCooldownCount": sum(int(row["outOfRangeCooldownCount"]) for row in rows),
        "rows": rows,
        "daily": daily_rows,
    }


def _print_pool_inventory_report(report: dict[str, Any]) -> None:
    scope_label = "广义武器箱" if report["scope"] == "case_only" else "非广义武器箱"
    print("C5 库存冷却查询")
    print(
        f"库存源: {report['source']} | C5账号 {report['accountCount']} 个 | "
        f"itemScope={report['scope']} ({scope_label})"
    )
    print(
        f"汇总: 类型 {report['totalTypes']} 个 | 总件 {report['totalCount']} | "
        f"当前可交易 {report['tradableCount']} | 冷却中 {report['cooldownCount']}"
    )
    print(
        f"未来 {report['days']} 天解冷却 {report['futureCooldownCount']} 件 | "
        f"C5未返回到期时间 {report['unknownCooldownCount']} 件 | "
        f"超过范围 {report['outOfRangeCooldownCount']} 件"
    )

    print("\n当前已经可以交易:")
    tradable_rows = [row for row in report["rows"] if int(row["tradableCount"]) > 0]
    if not tradable_rows:
        print("- 无")
    for index, row in enumerate(tradable_rows, start=1):
        print(
            f"{index}. {row['marketHashName']} | "
            f"可交易 {row['tradableCount']}/{row['totalCount']} | "
            f"账号 {row['tradableAccountSummary']}"
        )

    print(f"\n未来 {report['days']} 天每天解冷却:")
    if int(report["futureCooldownCount"]) <= 0:
        if int(report["unknownCooldownCount"]) > 0:
            print("C5 inventory/v2 本次没有返回可解析的到期时间，无法按日拆分。")
        else:
            print("- 无")
    else:
        for day in report["daily"]:
            print(f"{day['date']} | 合计 {day['count']} 件")
            if not day["items"]:
                print("  - 无")
                continue
            for item in day["items"]:
                print(
                    f"  - {item['marketHashName']} | +{item['count']} | "
                    f"账号 {item['accountSummary']}"
                )

    undated_rows = [row for row in report["rows"] if int(row["unknownCooldownCount"]) > 0]
    if undated_rows:
        undated_rows.sort(key=lambda row: (-int(row["unknownCooldownCount"]), str(row["marketHashName"])))
        print("\nC5未返回到期时间的不可交易库存:")
        for index, row in enumerate(undated_rows, start=1):
            print(
                f"{index}. {row['marketHashName']} | "
                f"未给时间 {row['unknownCooldownCount']}/{row['cooldownCount']} | "
                f"{row['cooldownStatusSummary']} | 账号 {row['cooldownAccountSummary']}"
            )


def cmd_pool_inventory(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.c5_api_key:
        raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。")
    config = load_strategy_config(settings)
    scope = normalize_guadao_item_scope(getattr(args, "scope", None) or config.guadao_item_scope)
    c5_client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    inventory_payload = fetch_all_c5_inventories(
        c5_client,
        settings,
        allow_cached_fallback=bool(getattr(args, "cache_fallback", False)),
        cache_max_age_minutes=getattr(args, "cache_max_age_minutes", 180),
    )
    db = _open_db(settings)
    try:
        report = _build_pool_inventory_report(
            inventory_payload,
            db=db,
            scope=scope,
            days=int(getattr(args, "days", 7) or 7),
        )
    finally:
        db.close()

    _print_pool_inventory_report(report)
    if getattr(args, "dump_json", False):
        _print_json(report)
    return 0


def cmd_pool_scan(args: argparse.Namespace) -> int:
    """Scan inventory pool and evaluate strategies for each item type."""
    settings = _settings_from_args(args)
    db = _open_db(settings)
    pool_names = db.get_pool_market_hash_names()
    db.close()
    if not pool_names:
        print("底仓为空。运行 `python .\\main.py executor start` 会自动从 C5 库存初始化。")
        return 0
    config = load_strategy_config(settings)

    # Override config from CLI args
    if args.min_price is not None:
        config.min_price = args.min_price
    if args.guadao_max_ratio is not None:
        config.guadao_max_listing_ratio = args.guadao_max_ratio
    if args.transfer_min_ratio is not None:
        config.transfer_min_real_ratio = args.transfer_min_ratio
    if args.steam_net_factor is not None:
        config.steam_net_factor = args.steam_net_factor
    if args.top_n is not None:
        config.top_n = args.top_n

    report = scan_strategies(
        settings,
        config,
        allow_cached_fallback=not args.no_cache_fallback,
        cache_max_age_minutes=args.cache_max_age_minutes,
        pool_market_hash_names=pool_names,
    )

    print(
        f"扫描完成: {report.total_pool_types} 个饰品类型 | "
        f"已评估 {len(report.all_evaluated)} 个 | "
        f"缺价 {report.missing_price_count} 个"
    )
    print(
        f"策略分布: 挂刀做T {report.guadao_count} 个 | "
        f"导余额做T {report.transfer_count} 个 | "
        f"持有 {report.hold_count} 个"
    )
    print(f"配置: listing_ratio ≤ {config.guadao_max_listing_ratio} → 挂刀 | "
          f"transfer_real_ratio ≥ {config.transfer_min_real_ratio} → 导余额")
    print()

    # Show guadao candidates
    top_n = config.top_n
    if report.guadao_candidates:
        lines = [f"=== 挂刀做T 候选 (listing_ratio 低优先, Top {top_n}) ==="]
        for i, c in enumerate(report.guadao_candidates[:top_n], 1):
            strategies_str = "+".join(STRATEGY_LABELS.get(s, s) for s in c.recommended_strategies)
            account_summary = "、".join(c.steam_accounts) if c.steam_accounts else "未知"
            star = "★" if c.listing_ratio <= args.star_threshold else " "
            lines.append(
                f"{star}{i:>3}. {c.name} | "
                f"listing_ratio {c.listing_ratio:.4f} | "
                f"transfer_ratio {c.transfer_real_ratio_pct:+.2f}% | "
                f"补仓 ¥{c.rebuy_price:.2f} | "
                f"Steam ¥{c.steam_sell_price:.2f} → ¥{c.steam_after_tax_price:.2f} | "
                f"库存 {c.inventory_count} ({c.tradable_count}可交易) | "
                f"账号 {account_summary}"
            )
        lines.append("")
        sys.stdout.write("\n".join(lines) + "\n")

    # Show transfer candidates
    if report.transfer_candidates:
        lines = [f"=== 导余额做T 候选 (transfer_real_ratio 高优先, Top {top_n}) ==="]
        for i, c in enumerate(report.transfer_candidates[:top_n], 1):
            strategies_str = "+".join(STRATEGY_LABELS.get(s, s) for s in c.recommended_strategies)
            account_summary = "、".join(c.steam_accounts) if c.steam_accounts else "未知"
            star = "★" if c.transfer_real_ratio >= 0.10 else " "
            lines.append(
                f"{star}{i:>3}. {c.name} | "
                f"transfer_ratio {c.transfer_real_ratio_pct:+.2f}% | "
                f"listing_ratio {c.listing_ratio:.4f} | "
                f"C5 ¥{c.rebuy_price:.2f} | "
                f"Steam ¥{c.steam_sell_price:.2f} | "
                f"库存 {c.inventory_count} ({c.tradable_count}可交易) | "
                f"账号 {account_summary}"
            )
        lines.append("")
        sys.stdout.write("\n".join(lines) + "\n")

    # Show hold items (no strategy fits)
    if report.hold_items and args.show_hold:
        lines = [f"=== 持有 (不满足任何策略, 共 {report.hold_count} 个) ==="]
        for i, c in enumerate(report.hold_items[:top_n], 1):
            lines.append(
                f"  {i:>3}. {c.name} | "
                f"listing_ratio {c.listing_ratio:.4f} | "
                f"transfer_ratio {c.transfer_real_ratio_pct:+.2f}% | "
                f"C5 ¥{c.rebuy_price:.2f} | Steam ¥{c.steam_sell_price:.2f}"
            )
        lines.append("")
        sys.stdout.write("\n".join(lines) + "\n")

    if args.dump_json:
        _print_json({
            "generatedAt": report.generated_at,
            "inventorySource": report.inventory_source,
            "config": config.to_dict(),
            "totalPoolTypes": report.total_pool_types,
            "missingPriceCount": report.missing_price_count,
            "guadaoCandidates": [c.to_dict(rank=i) for i, c in enumerate(report.guadao_candidates[:top_n], 1)],
            "transferCandidates": [c.to_dict(rank=i) for i, c in enumerate(report.transfer_candidates[:top_n], 1)],
            "holdItems": [c.to_dict(rank=i) for i, c in enumerate(report.hold_items[:top_n], 1)],
        })

    # Save evaluations to DB
    if args.save_eval:
        db = _open_db(settings)
        config_json = json.dumps(config.to_dict(), ensure_ascii=False)
        for c in report.all_evaluated:
            db.save_strategy_evaluation(
                market_hash_name=c.market_hash_name,
                rebuy_price=c.rebuy_price,
                steam_sell_price=c.steam_sell_price,
                steam_after_tax_price=c.steam_after_tax_price,
                listing_ratio=c.listing_ratio,
                transfer_real_ratio=c.transfer_real_ratio,
                recommended_strategy=c.primary_strategy,
                inventory_count=c.inventory_count,
                tradable_count=c.tradable_count,
                config_json=config_json,
            )
        db.close()
        print(f"已保存 {len(report.all_evaluated)} 条策略评估记录到数据库。")

    return 0


def cmd_pool_status(args: argparse.Namespace) -> int:
    """Show current inventory pool status."""
    settings = _settings_from_args(args)
    db = _open_db(settings)
    all_pool_items = db.list_pool_items()
    pool_items = db.list_pool_items(status=args.status_filter)
    db.close()

    if not pool_items:
        if args.status_filter:
            if all_pool_items:
                print(f"未找到状态为 `{args.status_filter}` 的底仓项。")
            else:
                print("底仓为空。运行 `python .\\main.py executor start` 会自动从 C5 库存初始化。")
        else:
            print("底仓为空。运行 `python .\\main.py executor start` 会自动从 C5 库存初始化。")
        return 0

    total_qty = 0
    status_counts: dict[str, int] = {}
    print(f"{'饰品名':<50} {'数量':>6} {'状态':<12} {'备注'}")
    print("-" * 90)
    for item in pool_items:
        mhn = item["market_hash_name"]
        qty = item["quantity"]
        status = item["status"]
        note = item["note"] or ""
        total_qty += qty
        status_counts[status] = status_counts.get(status, 0) + qty
        from cs2_assistant.models import POOL_STATUS_LABELS
        status_label = POOL_STATUS_LABELS.get(status, status)
        print(f"{mhn:<50} {qty:>6} {status_label:<12} {note}")

    print("-" * 90)
    print(f"合计: {len(pool_items)} 个类型, {total_qty} 件")
    for status, count in sorted(status_counts.items()):
        from cs2_assistant.models import POOL_STATUS_LABELS
        print(f"  {POOL_STATUS_LABELS.get(status, status)}: {count} 件")
    return 0


CN_TZ = timezone(timedelta(hours=8))


def _read_note_dict(note: str | None) -> dict[str, Any]:
    if not note:
        return {}
    try:
        data = json.loads(note)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_report_boundary(value: str, *, is_end: bool) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("日期不能为空")
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        day = datetime.strptime(raw, "%Y-%m-%d").date()
        local_time = time(23, 59, 59) if is_end else time(0, 0, 0)
        return datetime.combine(day, local_time, tzinfo=CN_TZ)
    hour_only_match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T\s](\d{2})", raw)
    if hour_only_match:
        day = datetime.strptime(hour_only_match.group(1), "%Y-%m-%d").date()
        hour = int(hour_only_match.group(2))
        if hour < 0 or hour > 23:
            raise ValueError(f"无效小时: {hour_only_match.group(2)}")
        local_time = time(hour, 59, 59) if is_end else time(hour, 0, 0)
        return datetime.combine(day, local_time, tzinfo=CN_TZ)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed


def _to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _to_local_display(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _empty_guadao_report_summary() -> dict[str, Any]:
    return {
        "count": 0,
        "steamGross": 0.0,
        "steamNet": 0.0,
        "cash": 0.0,
        "totalDiscountRatio": None,
        "faceDiscountRatio": None,
    }


def _add_guadao_report_row(summary: dict[str, Any], row: dict[str, Any]) -> None:
    summary["count"] += 1
    summary["steamGross"] += float(row["steamGross"] or 0.0)
    summary["steamNet"] += float(row["steamNet"] or 0.0)
    summary["cash"] += float(row["cash"] or 0.0)


def _finalize_guadao_report_summary(summary: dict[str, Any]) -> dict[str, Any]:
    steam_net = float(summary["steamNet"] or 0.0)
    steam_gross = float(summary["steamGross"] or 0.0)
    cash = float(summary["cash"] or 0.0)
    summary["steamGross"] = round(steam_gross, 2)
    summary["steamNet"] = round(steam_net, 2)
    summary["cash"] = round(cash, 2)
    summary["totalDiscountRatio"] = cash / steam_net if steam_net > 0 else None
    summary["faceDiscountRatio"] = cash / steam_gross if steam_gross > 0 else None
    return summary


def _steam_seller_net_from_notes(
    *,
    steam_gross: float | None,
    steam_net_factor: float,
    rebuy_note: dict[str, Any] | None = None,
    sell_note: dict[str, Any] | None = None,
) -> float | None:
    for note in (rebuy_note or {}, sell_note or {}):
        for key in ("steamSellerNetPrice", "steamNetPrice", "steamSoldNetPrice"):
            value = safe_float(note.get(key))
            if value is not None and value > 0:
                return value
    if steam_gross is None or steam_gross <= 0:
        return None
    # Fallback for historical rows before we stored Steam's seller-net amount.
    cents = (
        Decimal(str(float(steam_gross)))
        * Decimal(str(float(steam_net_factor)))
        * Decimal("100")
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(cents / Decimal("100"))


def _is_balance_insufficient_rebuy_note(note: dict[str, Any]) -> bool:
    skip_reason = str(note.get("skipReason") or "")
    if skip_reason == "c5_balance_insufficient":
        return True
    failed_reason = str(note.get("failedReason") or "")
    c5_error_payload = note.get("c5ErrorPayload")
    if isinstance(c5_error_payload, dict):
        error_code = safe_int(c5_error_payload.get("errorCode"))
        error_message = str(c5_error_payload.get("errorMsg") or c5_error_payload.get("message") or "")
        if error_code == 70001 or "余额不足" in error_message:
            return True
    return "70001" in failed_reason or "余额不足" in failed_reason or "insufficient balance" in failed_reason.lower()


def _load_successful_rebuy_source_ids(
    db: Database,
    *,
    completed_at_lte: str | None = None,
) -> set[int]:
    params: list[Any] = []
    completed_filter = ""
    if completed_at_lte:
        completed_filter = " AND completed_at IS NOT NULL AND completed_at <= ?"
        params.append(completed_at_lte)
    rows = db.conn.execute(
        f"""
        SELECT note
        FROM pool_operations
        WHERE operation_type = 'rebuy_on_c5'
          AND status = 'completed'
          {completed_filter}
        """,
        tuple(params),
    ).fetchall()
    source_ids: set[int] = set()
    for row in rows:
        note = _read_note_dict(row["note"])
        c5_order_id = str(note.get("c5OrderId") or "").strip()
        c5_final_status = str(note.get("c5FinalStatus") or "").strip()
        if c5_order_id and c5_final_status != "c5_success":
            continue
        source_id = safe_int(note.get("sourceSellOperationId"))
        if source_id is not None:
            source_ids.add(source_id)
    return source_ids


def _load_balance_insufficient_rebuy_source_ids(
    db: Database,
    *,
    completed_at_lte: str | None = None,
) -> set[int]:
    rows = db.conn.execute(
        """
        SELECT note, created_at, completed_at
        FROM pool_operations
        WHERE operation_type = 'rebuy_on_c5'
        """
    ).fetchall()
    source_ids: set[int] = set()
    for row in rows:
        if completed_at_lte:
            op_time = str(row["completed_at"] or row["created_at"] or "")
            if op_time and op_time > completed_at_lte:
                continue
        note = _read_note_dict(row["note"])
        if not _is_balance_insufficient_rebuy_note(note):
            continue
        source_id = safe_int(note.get("sourceSellOperationId"))
        if source_id is not None:
            source_ids.add(source_id)
    return source_ids


def _sell_steam_report_row(sell: Any, steam_net_factor: float) -> dict[str, Any] | None:
    sell_note = _read_note_dict(sell["note"])
    steam_gross = (
        safe_float(sell_note.get("steamListPrice"))
        or safe_float(sell["actual_price"])
        or safe_float(sell["expected_price"])
    )
    if steam_gross is None or steam_gross <= 0:
        return None
    steam_net = _steam_seller_net_from_notes(
        steam_gross=steam_gross,
        steam_net_factor=steam_net_factor,
        sell_note=sell_note,
    )
    if steam_net is None or steam_net <= 0:
        return None
    return {
        "completedAt": sell["completed_at"],
        "completedAtLocal": _to_local_display(sell["completed_at"]),
        "marketHashName": str(sell["market_hash_name"] or "").strip(),
        "steamGross": steam_gross,
        "steamNet": steam_net,
        "cash": 0.0,
        "sellOperationId": int(sell["id"]),
        "assetId": str(sell["asset_id"] or ""),
        "listingId": str(sell_note.get("listingId") or ""),
    }


def _build_unclosed_sold_steam_summary(
    db: Database,
    *,
    steam_net_factor: float,
    market_hash_name: str | None = None,
) -> dict[str, Any]:
    market_filter = ""
    params: list[Any] = []
    if market_hash_name:
        market_filter = " AND market_hash_name = ?"
        params.append(market_hash_name)

    sold_rows = db.conn.execute(
        f"""
        SELECT *
        FROM pool_operations
        WHERE operation_type = 'sell_on_steam'
          AND status = 'sold'
          {market_filter}
        ORDER BY completed_at ASC, id ASC
        """,
        tuple(params),
    ).fetchall()

    closed_sell_ids = _load_successful_rebuy_source_ids(db)
    ignored_sell_ids = _load_balance_insufficient_rebuy_source_ids(db)

    summary = _empty_guadao_report_summary()
    item_summaries: dict[str, dict[str, Any]] = {}
    for sell in sold_rows:
        sell_id = int(sell["id"])
        if sell_id in closed_sell_ids or sell_id in ignored_sell_ids:
            continue
        row = _sell_steam_report_row(sell, steam_net_factor)
        if row is None:
            continue
        _add_guadao_report_row(summary, row)
        item_summary = item_summaries.setdefault(row["marketHashName"], _empty_guadao_report_summary())
        _add_guadao_report_row(item_summary, row)

    finalized_items = [
        {"marketHashName": name, **_finalize_guadao_report_summary(item_summary)}
        for name, item_summary in item_summaries.items()
    ]
    finalized_items.sort(key=lambda row: (-int(row["count"]), row["marketHashName"]))
    return {
        "summary": _finalize_guadao_report_summary(summary),
        "items": finalized_items,
    }


def _build_sold_steam_time_summary(
    db: Database,
    *,
    start_utc: str,
    end_utc: str,
    steam_net_factor: float,
    market_hash_name: str | None = None,
) -> dict[str, Any]:
    params: list[Any] = [start_utc, end_utc]
    market_filter = ""
    if market_hash_name:
        market_filter = " AND market_hash_name = ?"
        params.append(market_hash_name)

    sold_rows = db.conn.execute(
        f"""
        SELECT *
        FROM pool_operations
        WHERE operation_type = 'sell_on_steam'
          AND status = 'sold'
          AND completed_at IS NOT NULL
          AND completed_at >= ?
          AND completed_at <= ?
          {market_filter}
        ORDER BY completed_at ASC, id ASC
        """,
        tuple(params),
    ).fetchall()

    summary = _empty_guadao_report_summary()
    item_summaries: dict[str, dict[str, Any]] = {}
    detail_rows: list[dict[str, Any]] = []
    for sell in sold_rows:
        row = _sell_steam_report_row(sell, steam_net_factor)
        if row is None:
            continue
        detail_rows.append(row)
        _add_guadao_report_row(summary, row)
        item_summary = item_summaries.setdefault(row["marketHashName"], _empty_guadao_report_summary())
        _add_guadao_report_row(item_summary, row)

    finalized_items = [
        {"marketHashName": name, **_finalize_guadao_report_summary(item_summary)}
        for name, item_summary in item_summaries.items()
    ]
    finalized_items.sort(key=lambda row: (-int(row["count"]), row["marketHashName"]))
    return {
        "summary": _finalize_guadao_report_summary(summary),
        "items": finalized_items,
        "details": detail_rows,
    }


def _build_sold_steam_reconciliation_summary(
    db: Database,
    *,
    start_utc: str,
    end_utc: str,
    steam_net_factor: float,
    market_hash_name: str | None = None,
) -> dict[str, Any]:
    params: list[Any] = [start_utc, end_utc]
    market_filter = ""
    if market_hash_name:
        market_filter = " AND market_hash_name = ?"
        params.append(market_hash_name)

    sold_rows = db.conn.execute(
        f"""
        SELECT *
        FROM pool_operations
        WHERE operation_type = 'sell_on_steam'
          AND status = 'sold'
          AND completed_at IS NOT NULL
          AND completed_at >= ?
          AND completed_at <= ?
          {market_filter}
        ORDER BY completed_at ASC, id ASC
        """,
        tuple(params),
    ).fetchall()

    closed_sell_ids = _load_successful_rebuy_source_ids(db, completed_at_lte=end_utc)
    ignored_sell_ids = _load_balance_insufficient_rebuy_source_ids(db, completed_at_lte=end_utc)
    closed = _empty_guadao_report_summary()
    unclosed = _empty_guadao_report_summary()
    ignored = _empty_guadao_report_summary()

    for sell in sold_rows:
        row = _sell_steam_report_row(sell, steam_net_factor)
        if row is None:
            continue
        sell_id = int(sell["id"])
        if sell_id in closed_sell_ids:
            _add_guadao_report_row(closed, row)
        elif sell_id in ignored_sell_ids:
            _add_guadao_report_row(ignored, row)
        else:
            _add_guadao_report_row(unclosed, row)

    return {
        "closed": _finalize_guadao_report_summary(closed),
        "unclosed": _finalize_guadao_report_summary(unclosed),
        "ignored": _finalize_guadao_report_summary(ignored),
    }


def _build_guadao_discount_report(
    db: Database,
    *,
    start_utc: str,
    end_utc: str,
    steam_net_factor: float,
    market_hash_name: str | None = None,
) -> dict[str, Any]:
    params: list[Any] = [start_utc, end_utc]
    market_filter = ""
    if market_hash_name:
        market_filter = " AND market_hash_name = ?"
        params.append(market_hash_name)
    rebuy_rows = db.conn.execute(
        f"""
        SELECT *
        FROM pool_operations
        WHERE operation_type = 'rebuy_on_c5'
          AND status = 'completed'
          AND completed_at IS NOT NULL
          AND completed_at >= ?
          AND completed_at <= ?
          {market_filter}
        ORDER BY completed_at ASC, id ASC
        """,
        tuple(params),
    ).fetchall()

    source_ids: list[int] = []
    for row in rebuy_rows:
        source_id = safe_int(_read_note_dict(row["note"]).get("sourceSellOperationId"))
        if source_id is not None:
            source_ids.append(source_id)

    sell_by_id: dict[int, Any] = {}
    if source_ids:
        placeholders = ", ".join("?" for _ in source_ids)
        sell_rows = db.conn.execute(
            f"SELECT * FROM pool_operations WHERE id IN ({placeholders})",
            tuple(source_ids),
        ).fetchall()
        sell_by_id = {int(row["id"]): row for row in sell_rows}

    detail_rows: list[dict[str, Any]] = []
    item_summaries: dict[str, dict[str, Any]] = {}
    summary = _empty_guadao_report_summary()
    closed_from_sell_outside_range = _empty_guadao_report_summary()

    for rebuy in rebuy_rows:
        rebuy_note = _read_note_dict(rebuy["note"])
        c5_order_id = str(rebuy_note.get("c5OrderId") or "").strip()
        c5_final_status = str(rebuy_note.get("c5FinalStatus") or "").strip()
        if c5_order_id and c5_final_status != "c5_success":
            continue

        source_id = safe_int(rebuy_note.get("sourceSellOperationId"))
        sell = sell_by_id.get(source_id) if source_id is not None else None
        sell_note = _read_note_dict(sell["note"]) if sell is not None else {}

        item_name = str(rebuy["market_hash_name"] or (sell["market_hash_name"] if sell else "")).strip()
        steam_gross = (
            safe_float(rebuy_note.get("steamListPrice"))
            or safe_float(sell_note.get("steamListPrice"))
            or (safe_float(sell["expected_price"]) if sell is not None else None)
        )
        cash = safe_float(rebuy["actual_price"]) or safe_float(rebuy["expected_price"])
        if steam_gross is None or steam_gross <= 0 or cash is None or cash <= 0:
            continue

        steam_net = _steam_seller_net_from_notes(
            steam_gross=steam_gross,
            steam_net_factor=steam_net_factor,
            rebuy_note=rebuy_note,
            sell_note=sell_note,
        )
        if steam_net is None or steam_net <= 0:
            continue
        detail = {
            "completedAt": rebuy["completed_at"],
            "completedAtLocal": _to_local_display(rebuy["completed_at"]),
            "marketHashName": item_name,
            "steamGross": steam_gross,
            "steamNet": steam_net,
            "cash": cash,
            "totalDiscountRatio": cash / steam_net if steam_net > 0 else None,
            "faceDiscountRatio": cash / steam_gross if steam_gross > 0 else None,
            "sellOperationId": source_id,
            "rebuyOperationId": int(rebuy["id"]),
            "assetId": str(sell["asset_id"] or "") if sell is not None else "",
            "listingId": str(rebuy_note.get("sourceListing") or sell_note.get("listingId") or ""),
        }
        detail_rows.append(detail)
        _add_guadao_report_row(summary, detail)
        item_summary = item_summaries.setdefault(item_name, _empty_guadao_report_summary())
        _add_guadao_report_row(item_summary, detail)
        sell_completed_at = str(sell["completed_at"] or "") if sell is not None else ""
        if sell_completed_at and (sell_completed_at < start_utc or sell_completed_at > end_utc):
            _add_guadao_report_row(closed_from_sell_outside_range, detail)

    finalized_items = [
        {"marketHashName": market_hash_name, **_finalize_guadao_report_summary(item_summary)}
        for market_hash_name, item_summary in item_summaries.items()
    ]
    finalized_items.sort(key=lambda row: (-int(row["count"]), row["marketHashName"]))

    return {
        "startUtc": start_utc,
        "endUtc": end_utc,
        "steamNetFactor": steam_net_factor,
        "summary": _finalize_guadao_report_summary(summary),
        "items": finalized_items,
        "details": detail_rows,
        "steamSoldInRange": _build_sold_steam_time_summary(
            db,
            start_utc=start_utc,
            end_utc=end_utc,
            steam_net_factor=steam_net_factor,
            market_hash_name=market_hash_name,
        ),
        "steamSoldReconciliation": _build_sold_steam_reconciliation_summary(
            db,
            start_utc=start_utc,
            end_utc=end_utc,
            steam_net_factor=steam_net_factor,
            market_hash_name=market_hash_name,
        ),
        "closedFromSellOutsideRange": _finalize_guadao_report_summary(closed_from_sell_outside_range),
        "unclosedSoldSteam": _build_unclosed_sold_steam_summary(
            db,
            steam_net_factor=steam_net_factor,
            market_hash_name=market_hash_name,
        ),
    }


def _fmt_cny(value: float | None) -> str:
    return "-" if value is None else f"CNY {value:.2f}"


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def _print_guadao_discount_report(
    report: dict[str, Any],
    *,
    start_local: datetime,
    end_local: datetime,
    show_detail: bool,
) -> None:
    summary = report["summary"]
    print("挂刀余额折扣报表")
    print(
        "范围: "
        f"{start_local.astimezone(CN_TZ).strftime('%Y-%m-%d %H:%M:%S')} ~ "
        f"{end_local.astimezone(CN_TZ).strftime('%Y-%m-%d %H:%M:%S')} (北京时间)"
    )
    print(
        "口径: 按 C5 补仓完成时间统计；"
        "新补仓订单仅统计 C5 最终成功记录；"
        "总折比 = C5补仓现金 / Steam税后到手；"
        "面值折比 = C5补仓现金 / Steam税前售价；"
        "未闭环已卖出 Steam 余额为当前仍未成功补仓闭环的卖出流水，不按日期过滤。"
    )
    print(
        f"总览: 闭环 {summary['count']} 笔 | "
        f"Steam面值 {_fmt_cny(summary['steamGross'])} | "
        f"Steam到手 {_fmt_cny(summary['steamNet'])} | "
        f"C5现金 {_fmt_cny(summary['cash'])} | "
        f"总折比 {_fmt_pct(summary['totalDiscountRatio'])} | "
        f"面值折比 {_fmt_pct(summary['faceDiscountRatio'])}"
    )
    steam_sold = report.get("steamSoldInRange", {}).get("summary", _empty_guadao_report_summary())
    print(
        f"Steam入账口径(按卖出时间): 卖出 {steam_sold['count']} 笔 | "
        f"Steam面值 {_fmt_cny(steam_sold['steamGross'])} | "
        f"Steam到手 {_fmt_cny(steam_sold['steamNet'])}"
    )
    reconciliation = report.get("steamSoldReconciliation", {})
    sold_closed = reconciliation.get("closed", _empty_guadao_report_summary())
    sold_unclosed = reconciliation.get("unclosed", _empty_guadao_report_summary())
    sold_ignored = reconciliation.get("ignored", _empty_guadao_report_summary())
    reconciled_net = (
        float(sold_closed.get("steamNet") or 0.0)
        + float(sold_unclosed.get("steamNet") or 0.0)
        + float(sold_ignored.get("steamNet") or 0.0)
    )
    print(
        "卖出时间对账: "
        f"已闭环 {sold_closed['count']} 笔 {_fmt_cny(sold_closed['steamNet'])} + "
        f"本期未闭环 {sold_unclosed['count']} 笔 {_fmt_cny(sold_unclosed['steamNet'])}"
        + (
            f" + 排除项 {sold_ignored['count']} 笔 {_fmt_cny(sold_ignored['steamNet'])}"
            if sold_ignored["count"]
            else ""
        )
        + f" = {_fmt_cny(reconciled_net)}"
    )
    outside = report.get("closedFromSellOutsideRange", _empty_guadao_report_summary())
    if outside["count"]:
        print(
            f"对账提示: 总览含历史卖出本期补仓 {outside['count']} 笔 | "
            f"Steam到手 {_fmt_cny(outside['steamNet'])}，这部分不应计入本时间段 Steam 钱包入账。"
        )
    unclosed_sold = report.get("unclosedSoldSteam", {}).get("summary", _empty_guadao_report_summary())
    print(
        f"当前未闭环存量(不按日期过滤): {unclosed_sold['count']} 笔 | "
        f"Steam面值 {_fmt_cny(unclosed_sold['steamGross'])} | "
        f"Steam到手 {_fmt_cny(unclosed_sold['steamNet'])}"
    )
    if not report["items"]:
        print("本时间段没有已完成补仓的挂刀闭环。")
        return

    print("\n按饰品汇总:")
    print(f"{'饰品':<42} {'笔数':>4} {'Steam到手':>12} {'C5现金':>10} {'总折比':>8} {'面值折比':>8}")
    for row in report["items"]:
        print(
            f"{row['marketHashName']:<42} "
            f"{int(row['count']):>4} "
            f"{row['steamNet']:>12.2f} "
            f"{row['cash']:>10.2f} "
            f"{_fmt_pct(row['totalDiscountRatio']):>8} "
            f"{_fmt_pct(row['faceDiscountRatio']):>8}"
        )

    if not show_detail:
        return

    print("\n明细:")
    print(
        f"{'完成时间':<19} {'饰品':<30} {'Steam面值':>9} {'Steam到手':>9} "
        f"{'C5现金':>8} {'总折比':>8} {'asset':>12} {'listing':>18}"
    )
    for row in report["details"]:
        print(
            f"{row['completedAtLocal']:<19} "
            f"{row['marketHashName']:<30} "
            f"{row['steamGross']:>9.2f} "
            f"{row['steamNet']:>9.2f} "
            f"{row['cash']:>8.2f} "
            f"{_fmt_pct(row['totalDiscountRatio']):>8} "
            f"{(row['assetId'] or '-'):>12} "
            f"{(row['listingId'] or '-'):>18}"
        )


def cmd_pool_guadao_report(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    config = load_strategy_config(settings)
    start_local = _parse_report_boundary(args.date_from, is_end=False)
    end_local = (
        _parse_report_boundary(args.date_to, is_end=True)
        if args.date_to
        else datetime.now(CN_TZ)
    )
    if end_local < start_local:
        raise ValueError("--to 不能早于 --from")

    db = _open_db(settings)
    try:
        report = _build_guadao_discount_report(
            db,
            start_utc=_to_utc_iso(start_local),
            end_utc=_to_utc_iso(end_local),
            steam_net_factor=config.steam_net_factor,
            market_hash_name=args.market_hash_name,
        )
    finally:
        db.close()

    if args.dump_json:
        _print_json(
            {
                "startLocal": start_local.isoformat(timespec="seconds"),
                "endLocal": end_local.isoformat(timespec="seconds"),
                **report,
            }
        )
        return 0

    _print_guadao_discount_report(
        report,
        start_local=start_local,
        end_local=end_local,
        show_detail=args.detail,
    )
    return 0


def cmd_executor_status(args: argparse.Namespace) -> int:
    return cmd_pool_status(args)


def cmd_pool_config(args: argparse.Namespace) -> int:
    """Show or edit strategy config."""
    settings = _settings_from_args(args)
    config = load_strategy_config(settings)

    if args.edit:
        print("配置策略参数（直接回车保留当前值）：")

        def _prompt_float(label: str, current: float) -> float:
            raw = input(f"  {label} [{current}]: ").strip()
            if not raw:
                return current
            return float(raw)

        def _prompt_int(label: str, current: int) -> int:
            raw = input(f"  {label} [{current}]: ").strip()
            if not raw:
                return current
            return int(raw)

        def _prompt_str(label: str, current: str) -> str:
            raw = input(f"  {label} [{current}]: ").strip()
            return raw or current

        config.steam_net_factor = _prompt_float("Steam 税后系数 (steam_net_factor)", config.steam_net_factor)
        config.c5_settlement_factor = _prompt_float("C5 结算系数 (c5_settlement_factor)", config.c5_settlement_factor)
        config.balance_discount = _prompt_float("余额折扣率 (balance_discount)", config.balance_discount)
        config.guadao_max_listing_ratio = _prompt_float("挂刀阈值 listing_ratio ≤ (guadao_max_listing_ratio)", config.guadao_max_listing_ratio)
        config.guadao_item_scope = _prompt_str("挂刀品类范围 case_only/non_case_only (guadao_item_scope)", config.guadao_item_scope)
        config.transfer_min_real_ratio = _prompt_float("导余额阈值 transfer_real_ratio ≥ (transfer_min_real_ratio)", config.transfer_min_real_ratio)
        config.min_price = _prompt_float("最低价格过滤 (min_price)", config.min_price)
        config.poll_interval_minutes = _prompt_int("轮询间隔分钟 (poll_interval_minutes)", config.poll_interval_minutes)
        config.top_n = _prompt_int("每种策略输出前 N 个 (top_n)", config.top_n)

        path = save_strategy_config(settings, config)
        print(f"策略配置已保存到: {path}")
    else:
        print("当前策略配置:")
        print(f"  Steam 税后系数 (steam_net_factor):       {config.steam_net_factor}")
        print(f"  C5 结算系数 (c5_settlement_factor):      {config.c5_settlement_factor}")
        print(f"  余额折扣率 (balance_discount):           {config.balance_discount}")
        print(f"  挂刀阈值 (guadao_max_listing_ratio):     ≤ {config.guadao_max_listing_ratio}")
        print(f"  挂刀品类范围 (guadao_item_scope):        {config.guadao_item_scope}")
        print(f"  导余额阈值 (transfer_min_real_ratio):    ≥ {config.transfer_min_real_ratio}")
        print(f"  最低价格 (min_price):                    {config.min_price}")
        print(f"  轮询间隔 (poll_interval_minutes):        {config.poll_interval_minutes}")
        print(f"  输出数量 (top_n):                        {config.top_n}")
        print()
        print("公式说明:")
        print("  listing_ratio = rebuy_price / (steam_sell_price × steam_net_factor)")
        print("  transfer_real_ratio = listing_ratio × c5_settlement_factor - balance_discount")
        print()
        print("  listing_ratio 低 → 挂刀做T（卖 Steam，低价补仓，获得低价余额）")
        print("  transfer_real_ratio 高 → 导余额做T（利用低价余额赚钱）")
        print()
        print(f"配置文件: {settings.db_path.parent / 'strategy_config.json'}")
    return 0


def cmd_pool_monitor(args: argparse.Namespace) -> int:
    """Run continuous strategy monitoring."""
    import time

    settings = _settings_from_args(args)
    db = _open_db(settings)
    pool_names = db.get_pool_market_hash_names()
    db.close()
    if not pool_names:
        print("底仓为空。运行 `python .\\main.py executor start` 会自动从 C5 库存初始化。")
        return 0
    config = load_strategy_config(settings)
    interval = config.poll_interval_minutes * 60

    print(f"底仓策略监控启动 | 轮询间隔 {config.poll_interval_minutes} 分钟")
    print(f"挂刀阈值: listing_ratio ≤ {config.guadao_max_listing_ratio}")
    print(f"导余额阈值: transfer_real_ratio ≥ {config.transfer_min_real_ratio}")
    print()

    serverchan_client = None
    if settings.serverchan_sendkey:
        from cs2_assistant.clients import ServerChanClient
        serverchan_client = ServerChanClient(settings.serverchan_sendkey, settings.serverchan_base_url)

    cycle = 0
    while True:
        cycle += 1
        now_str = utc_now_iso()
        print(f"[{now_str}] 第 {cycle} 轮扫描...")

        try:
            report = scan_strategies(settings, config, pool_market_hash_names=pool_names)
        except Exception as exc:
            print(f"扫描失败: {exc}", file=sys.stderr)
            time.sleep(interval)
            continue

        print(
            f"  评估 {len(report.all_evaluated)} 个 | "
            f"挂刀 {report.guadao_count} | 导余额 {report.transfer_count} | "
            f"持有 {report.hold_count}"
        )

        # Alert on noteworthy candidates
        alert_lines: list[str] = []
        for c in report.guadao_candidates[:5]:
            account_summary = "、".join(c.steam_accounts) if c.steam_accounts else "未知"
            alert_lines.append(
                f"挂刀 | {c.name} | ratio={c.listing_ratio:.4f} | "
                f"补仓¥{c.rebuy_price:.2f} | Steam¥{c.steam_after_tax_price:.2f} | "
                f"账号 {account_summary}"
            )
        for c in report.transfer_candidates[:5]:
            account_summary = "、".join(c.steam_accounts) if c.steam_accounts else "未知"
            alert_lines.append(
                f"导余额 | {c.name} | ratio={c.transfer_real_ratio_pct:+.2f}% | "
                f"C5¥{c.rebuy_price:.2f} | Steam¥{c.steam_sell_price:.2f} | "
                f"账号 {account_summary}"
            )

        if alert_lines:
            for line in alert_lines:
                print(f"  {line}")

            # Push notification
            if serverchan_client and (report.guadao_count > 0 or report.transfer_count > 0):
                title = f"底仓策略: 挂刀{report.guadao_count}个 导余额{report.transfer_count}个"
                body = "\n".join(alert_lines)
                try:
                    serverchan_client.send(title, body)
                    print("  ServerChan 推送成功")
                except Exception as exc:
                    print(f"  ServerChan 推送失败: {exc}", file=sys.stderr)

        if args.once:
            return 0

        print(f"  下次扫描: {config.poll_interval_minutes} 分钟后")
        time.sleep(interval)


def cmd_executor_start(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    config = load_strategy_config(settings)
    if args.enable:
        config.execution_enabled = True
    if args.disable:
        config.execution_enabled = False
    if args.dry_run:
        config.dry_run = True
    if args.no_dry_run:
        config.dry_run = False
    if args.max_list is not None:
        config.max_list_per_cycle = args.max_list
    if args.max_transfer_buy is not None:
        config.max_transfer_buy_per_cycle = args.max_transfer_buy
    force_refresh_override = None
    if args.force_refresh:
        force_refresh_override = True
    if args.no_force_refresh:
        force_refresh_override = False
    engine = ExecutionEngine(
        settings,
        config,
        dry_run_override=None,
        force_refresh_override=force_refresh_override,
    )
    try:
        engine.run(once=args.once)
    finally:
        engine.close()
    return 0


def cmd_steam_auth_check(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    client = _build_steam_client(settings)
    payload = client.my_listings(count=1)
    if not payload or payload.get("success") not in (1, True):
        print("Steam auth failed.")
        return 1
    print("Steam auth OK.")
    return 0


def cmd_steam_listings(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    client = _build_steam_client(settings)
    listings = client.list_active_listings(count=args.count)
    print(f"Active listings: {len(listings)}")
    for listing in listings[: args.count]:
        print(
            f"- {listing.market_hash_name or '-'} | listing={listing.listing_id} | "
            f"asset={listing.asset_id or '-'} | price={listing.price or '-'}"
        )
    return 0


def cmd_steam_confirm(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    client = _build_steam_client(settings)
    count = client.confirm_all()
    print(f"Confirmed {count} listings.")
    return 0


def cmd_steam_test_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    config = load_strategy_config(settings)
    client = _build_steam_client(settings)
    try:
        payload = client.sell_item(
            app_id=settings.app_id,
            context_id=args.context_id or config.steam_context_id,
            asset_id=args.asset_id,
            price=args.price,
            quantity=1,
            steam_net_factor=config.steam_net_factor,
        )
    except SteamMarketError as exc:
        print(f"[测试上架失败] asset={args.asset_id} | Steam挂价 CNY {args.price:.2f} | 原因: {exc}")
        return 1

    listing_id = str(payload.get("listingid") or "").strip()
    print(f"[测试上架成功] asset={args.asset_id} | listing={listing_id or '-'} | Steam挂价 CNY {args.price:.2f}")

    if not args.no_confirm:
        try:
            count = client.confirm_all()
            print(f"[测试确认] 已确认 {count} 个挂单")
        except SteamMarketError as exc:
            print(f"[测试确认失败] {exc}")

    if not listing_id and not args.no_cancel:
        try:
            for listing in client.list_active_listings(count=200):
                if str(listing.asset_id or "") == str(args.asset_id):
                    listing_id = str(listing.listing_id or "").strip()
                    break
        except SteamMarketError as exc:
            print(f"[测试查找挂单失败] {exc}")

    if listing_id and not args.no_cancel:
        try:
            removed = client.remove_listing(listing_id)
            print(f"[测试撤单] listing={listing_id} | removed={removed}")
        except SteamMarketError as exc:
            print(f"[测试撤单失败] listing={listing_id} | 原因: {exc}")
    elif not listing_id:
        print("[测试撤单跳过] Steam 未返回 listingid，请到 Steam 待确认/挂单页检查。")
    return 0


def cmd_steam_price_cache_show(args: argparse.Namespace) -> int:
    snapshot = get_pricing_cache_snapshot()
    print(f"Price cache entries: {len(snapshot)}")
    for row in snapshot:
        print(
            f"- {row['market_hash_name']} | age={row['age_sec']:.0f}s | "
            f"list={row['list_price']:.2f} | wall={row['wall_price']}"
        )
    return 0


def cmd_steam_price_cache_clear(args: argparse.Namespace) -> int:
    clear_pricing_cache(args.name)
    print("Price cache cleared.")
    return 0


def cmd_steam_price_check(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    config = load_strategy_config(settings)
    client = _build_steam_client(settings)
    try:
        decision = fetch_listing_price(
            client,
            app_id=settings.app_id,
            market_hash_name=args.market_hash_name,
            wall_min_count=config.listing_wall_min_count,
            price_offset=config.listing_price_offset,
            min_price=0.01,
            country=config.steam_country,
            language=config.steam_language,
            currency=config.steam_currency,
            force_refresh=True,
            cache_ttl=config.steam_price_cache_ttl,
            debug=True,
        )
    except SteamMarketError as exc:
        print(f"Steam price check failed: {exc}")
        return 1
    if decision is None:
        print("Steam price unavailable.")
        return 1
    print(
        f"Steam list price: {decision.list_price:.2f} | wall={decision.wall_price} | reason={decision.reason}"
    )
    return 0


def cmd_account_status(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = _account_store(settings)
    accounts = store.list_accounts()
    current = store.get_current()
    print(f"账户数: {len(accounts)}")
    print(f"存储文件: {store.file_path}")
    if not accounts:
        print("当前没有已保存账户。")
        return 0

    for account in accounts:
        marker = "*" if current and account.id == current.id else "-"
        cookie_status = "未配置"
        listing_summary = "-"
        if account.cookies and account.steam_id64:
            try:
                client = SteamMarketClient(
                    cookies=account.cookies,
                    steam_id64=account.steam_id64,
                    identity_secret=account.identity_secret,
                    device_id=account.device_id,
                    account_id=account.id,
                    base_url=settings.steam_market_base_url,
                )
                payload = client.my_listings(count=1)
                if payload and payload.get("success") in (1, True):
                    cookie_status = "有效"
                    listing_summary = str(len(client.list_active_listings(count=100)))
                else:
                    cookie_status = "无效"
            except Exception as exc:
                cookie_status = f"无效 ({exc})"
        print(
            f"{marker} {account.name} | id={account.id} | steam={account.steam_id64 or '-'} | "
            f"cookie={cookie_status} | 挂单数={listing_summary}"
        )
    return 0


def _generate_steam_device_id(steam_id64: str) -> str | None:
    steam_id = str(steam_id64 or "").strip()
    if not steam_id:
        return None
    digest = hashlib.sha1(steam_id.encode("utf-8")).hexdigest()
    return f"android:{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def cmd_account_import_mafile(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = _account_store(settings)
    path = Path(args.path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("maFile 内容不是 JSON 对象")

    name = str(payload.get("account_name") or payload.get("name") or path.stem).strip()
    steam_id64 = str(payload.get("steamid") or payload.get("steam_id") or "").strip()
    device_id = str(payload.get("device_id") or "").strip() or _generate_steam_device_id(steam_id64)
    update_fields = {
        "name": str(args.name or name).strip(),
        "username": args.username,
        "password": args.password,
        "steam_id64": steam_id64 or None,
        "shared_secret": payload.get("shared_secret"),
        "identity_secret": payload.get("identity_secret"),
        "device_id": device_id,
    }

    existing = store.get_account(name)
    if existing is None and steam_id64:
        existing = next(
            (account for account in store.list_accounts() if account.steam_id64 == steam_id64),
            None,
        )

    if existing:
        account = store.update_account(existing.id, **update_fields)
        action = "updated"
    else:
        account = store.add_account(**update_fields)
        action = "imported"
    print(f"Account {action}: {account.name} | id={account.id} | steam={account.steam_id64 or '-'}")
    return 0


def cmd_account_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = _account_store(settings)
    accounts = store.list_accounts()
    current = store.get_current()
    if not accounts:
        print("当前没有已保存账户。")
        return 0
    for account in accounts:
        marker = "*" if current and account.id == current.id else "-"
        print(f"{marker} {account.name} | id={account.id} | steam={account.steam_id64 or '-'}")
    return 0


def cmd_account_use(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = _account_store(settings)
    if not store.set_current(args.name):
        raise RuntimeError(f"account not found: {args.name}")
    account = store.get_current()
    print(f"当前账户: {account.name} | id={account.id}")
    return 0


def cmd_account_remove(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = _account_store(settings)
    if not store.delete_account(args.name):
        raise RuntimeError(f"account not found: {args.name}")
    print(f"已删除账户: {args.name}")
    return 0


def cmd_account_set_credentials(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = _account_store(settings)
    account = store.update_account(
        args.name,
        username=args.username,
        password=args.password,
    )
    if account is None:
        raise RuntimeError(f"account not found: {args.name}")
    print(f"已更新凭据: {account.name}")
    return 0


def cmd_steam_login(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = _account_store(settings)
    account = store.get_account(args.account)
    if account is None:
        raise RuntimeError(f"account not found: {args.account}")
    store.set_current(account.id)

    if args.browser:
        ok, status, updated = relogin_with_browser(store, account_id=account.id)
    else:
        ok, status, updated = try_steam_auto_relogin(store, account_id=account.id, force_login=True)
    if not ok or updated is None:
        raise RuntimeError(f"steam login failed: {status}")
    print(f"Steam 登录成功: {updated.name} | steam={updated.steam_id64 or '-'} | mode={'browser' if args.browser else 'steampy'}")
    return 0


def cmd_steam_cookie_refresh(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = _account_store(settings)
    account = store.get_current()
    if account is None:
        raise RuntimeError("no current account")
    if account.cookies and _verify_steam_cookies_valid(account.cookies, account.steam_id64 or ""):
        print(f"Cookie 仍有效，无需刷新: {account.name}")
        return 0
    ok, status, updated = try_steam_auto_relogin(store, account_id=account.id, force_login=True)
    if not ok or updated is None:
        raise RuntimeError(f"cookie refresh failed: {status}")
    print(f"Cookie 已刷新: {updated.name} | steam={updated.steam_id64 or '-'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CS2 理财助手 CLI",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--db-path", help="自定义 SQLite 数据库路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="初始化数据库")
    init_db.set_defaults(handler=cmd_init_db)

    import_catalog = subparsers.add_parser("import-catalog", help="导入本地 SteamDT 基础数据")
    import_catalog.add_argument("--file", help="SteamDT 基础数据 JSON 文件路径")
    import_catalog.set_defaults(handler=cmd_import_catalog)

    catalog = subparsers.add_parser("catalog", help="饰品资料库")
    catalog_subparsers = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_sync_csgo_api = catalog_subparsers.add_parser(
        "sync-csgo-api",
        help="从 ByMykel/CSGO-API 同步饰品资料",
        description=(
            "从 ByMykel/CSGO-API 同步饰品资料到本地 items 表。\n"
            "默认同步常用可交易品类，并保留本地已有 c5_item_id / steam_item_id。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    catalog_sync_csgo_api.add_argument("--language", default=CSGO_API_DEFAULT_LANGUAGE, help="CSGO-API 语言目录")
    catalog_sync_csgo_api.add_argument("--base-url", default=CSGO_API_BASE_URL, help=argparse.SUPPRESS)
    catalog_sync_csgo_api.add_argument("--timeout", type=float, default=30.0, help="单个分类请求超时秒数")
    catalog_sync_csgo_api.add_argument(
        "--category",
        dest="categories",
        action="append",
        help="只同步指定分类；可重复传入。默认同步常用品类。",
    )
    catalog_sync_csgo_api.set_defaults(handler=cmd_catalog_sync_csgo_api)

    search_item = subparsers.add_parser("search-item", help="按关键词搜索饰品")
    search_item.add_argument("--keyword", required=True, help="中文名或 HashName 关键词")
    search_item.add_argument("--limit", type=int, default=20, help="返回条数")
    search_item.set_defaults(handler=cmd_search_item)

    watch_add = subparsers.add_parser("watch-add", help="加入单品监控")
    watch_add.add_argument("--market-hash-name", required=True)
    watch_add.add_argument("--display-name")
    watch_add.add_argument("--note")
    watch_add.set_defaults(handler=cmd_watch_add)

    watch_list = subparsers.add_parser("watch-list", help="查看监控列表")
    watch_list.add_argument("--all", action="store_true", help="包含禁用项")
    watch_list.set_defaults(handler=cmd_watch_list)

    basket_add = subparsers.add_parser("basket-add", help="创建篮子")
    basket_add.add_argument("--name", required=True)
    basket_add.add_argument("--note")
    basket_add.set_defaults(handler=cmd_basket_add)

    basket_add_item = subparsers.add_parser("basket-add-item", help="向篮子加入饰品")
    basket_add_item.add_argument("--basket-name", required=True)
    basket_add_item.add_argument("--market-hash-name", required=True)
    basket_add_item.add_argument("--quantity", type=float, default=1.0)
    basket_add_item.set_defaults(handler=cmd_basket_add_item)

    basket_list = subparsers.add_parser("basket-list", help="查看篮子")
    basket_list.add_argument("--basket-name")
    basket_list.set_defaults(handler=cmd_basket_list)

    position_add = subparsers.add_parser("position-add", help="新增人工仓位记录")
    position_add.add_argument("--market-hash-name", required=True)
    position_add.add_argument("--status", required=True)
    position_add.add_argument("--quantity", type=float, default=0)
    position_add.add_argument("--manual-cost", type=float)
    position_add.add_argument("--target-buy-price", type=float)
    position_add.add_argument("--target-sell-price", type=float)
    position_add.add_argument("--note")
    position_add.set_defaults(handler=cmd_position_add)

    position_list = subparsers.add_parser("position-list", help="查看仓位记录")
    position_list.set_defaults(handler=cmd_position_list)

    rule_add = subparsers.add_parser("rule-add", help="新增提醒规则")
    rule_add.add_argument("--target-type", choices=["item", "basket"], required=True)
    rule_add.add_argument("--target-key", required=True, help="item 用 HashName，basket 用篮子名")
    rule_add.add_argument(
        "--metric",
        choices=[
            "c5_price",
            "steam_price",
            "c5_bid_price",
            "ratio",
            "basket_total",
            "c5_change_pct",
            "steam_change_pct",
            "basket_change_pct",
        ],
        required=True,
    )
    rule_add.add_argument("--operator", choices=["lte", "gte"], required=True)
    rule_add.add_argument("--threshold", type=float, required=True)
    rule_add.add_argument("--anchor-value", type=float)
    rule_add.add_argument("--cooldown-minutes", type=int, default=60)
    rule_add.add_argument("--note")
    rule_add.set_defaults(handler=cmd_rule_add)

    rule_list = subparsers.add_parser("rule-list", help="查看提醒规则")
    rule_list.add_argument("--all", action="store_true")
    rule_list.set_defaults(handler=cmd_rule_list)

    notify_test = subparsers.add_parser("notify-test", help="发送一条 ServerChan 测试消息")
    notify_test.add_argument("--title", default="CS2 理财助手测试提醒")
    notify_test.add_argument("--message", default="如果你看到这条消息，说明 ServerChan 已经打通。")
    notify_test.set_defaults(handler=cmd_notify_test)

    check_market = subparsers.add_parser("check-market", help="采集价格并触发规则判断")
    check_market.add_argument("--notify", action="store_true", help="命中规则后通过 ServerChan 推送")
    check_market.add_argument("--dump-json", action="store_true", help="额外输出 JSON 结果")
    check_market.set_defaults(handler=cmd_check_market)

    c5_quick_buy = subparsers.add_parser("c5-quick-buy", help="C5 快速购买，需要用户确认")
    group = c5_quick_buy.add_mutually_exclusive_group(required=True)
    group.add_argument("--market-hash-name")
    group.add_argument("--item-id")
    c5_quick_buy.add_argument("--max-price", type=float)
    c5_quick_buy.add_argument("--delivery", type=int)
    c5_quick_buy.add_argument("--low-price", type=float)
    c5_quick_buy.add_argument("--out-trade-no")
    c5_quick_buy.add_argument("--yes", action="store_true", help="跳过二次确认")
    c5_quick_buy.set_defaults(handler=cmd_c5_quick_buy)

    c5_sales = subparsers.add_parser("c5-sales", help="查询当前 C5 在售列表")
    c5_sales.add_argument("--steam-id")
    c5_sales.add_argument("--delivery", type=int)
    c5_sales.add_argument("--page", type=int, default=1)
    c5_sales.add_argument("--limit", type=int, default=20)
    c5_sales.set_defaults(handler=cmd_c5_sales)

    c5_steam_list = subparsers.add_parser("c5-steam-list", help="列出 C5 绑定的 Steam 账号")
    c5_steam_list.set_defaults(handler=cmd_c5_steam_list_safe)

    c5_inventory = subparsers.add_parser("c5-inventory", help="查询单个 Steam 账号的 C5 库存")
    c5_inventory.add_argument("--steam-id")
    c5_inventory.set_defaults(handler=cmd_c5_inventory)

    c5_inventory_all = subparsers.add_parser("c5-inventory-all", help="汇总所有绑定 Steam 账号的 C5 库存")
    c5_inventory_all.set_defaults(handler=cmd_c5_inventory_all)

    account = subparsers.add_parser(
        "account",
        help="账户管理",
        description="管理本地保存的 Steam 账户信息。",
    )
    account_subparsers = account.add_subparsers(dest="account_command", required=True)
    account_import = account_subparsers.add_parser(
        "import-mafile",
        help="导入 SDA maFile，可同时保存账号密码",
    )
    account_import.add_argument("path")
    account_import.add_argument("--name", help="账户显示名，默认取 maFile 里的账户名或文件名")
    account_import.add_argument("--username", help="Steam 登录账号")
    account_import.add_argument("--password", help="Steam 登录密码")
    account_import.set_defaults(handler=cmd_account_import_mafile)
    account_list = account_subparsers.add_parser("list", help="列出账户")
    account_list.set_defaults(handler=cmd_account_list)
    account_use = account_subparsers.add_parser("use", help="切换当前账户")
    account_use.add_argument("name")
    account_use.set_defaults(handler=cmd_account_use)
    account_set_credentials = account_subparsers.add_parser(
        "set-credentials",
        help="更新账户的 Steam 账号密码",
    )
    account_set_credentials.add_argument("name")
    account_set_credentials.add_argument("--username", required=True)
    account_set_credentials.add_argument("--password", required=True)
    account_set_credentials.set_defaults(handler=cmd_account_set_credentials)
    account_remove = account_subparsers.add_parser("remove", help="删除账户")
    account_remove.add_argument("name")
    account_remove.set_defaults(handler=cmd_account_remove)
    account_status = account_subparsers.add_parser("status", help="查看账户状态")
    account_status.set_defaults(handler=cmd_account_status)

    def add_t_profit_parser(name: str, *, hidden: bool = False) -> None:
        t_profit = subparsers.add_parser(
            name,
            help="兼容旧命令（不推荐）" if hidden else "做T扫描与结果输出",
            description=(
                "做T扫描相关命令。\n\n"
                "常用：\n"
                "  python .\\main.py t-profit scan -h\n"
                "  python .\\main.py t-profit scan --top 10 --min-price 10 --inventory-filter all\n"
                "  python .\\main.py t-profit scan --bottom 10 --min-price 10\n"
                "  python .\\main.py t-profit missing-steam"
            ),
            epilog="提示：要看 scan 的完整参数，请执行 `python .\\main.py t-profit scan -h`。",
            formatter_class=argparse.RawTextHelpFormatter,
        )
        t_profit_subparsers = t_profit.add_subparsers(dest=f"{name.replace('-', '_')}_command", required=True)

        t_profit_scan = t_profit_subparsers.add_parser(
            "scan",
            help="扫描全部库存并输出做T结果",
            description=(
                "扫描全部绑定 Steam 账号的 C5 库存，计算做T候选，并支持按库存状态筛选。\n\n"
                "inventory-filter 说明：\n"
                "  all: 全部库存\n"
                "  all_cooldown: 这个饰品类型全部为冷却中\n"
                "  has_tradable: 这个饰品类型只要存在不冷却就算命中"
            ),
            formatter_class=argparse.RawTextHelpFormatter,
        )
        mode_group = t_profit_scan.add_mutually_exclusive_group()
        mode_group.add_argument("--top", type=int, help="按收益率从高到低输出前 N 个候选，默认 10")
        mode_group.add_argument("--bottom", type=int, help="按收益率从低到高输出前 N 个候选")
        t_profit_scan.add_argument("--min-price", type=float, default=10.0, help="只保留 C5 最低售价不低于该值的饰品")
        t_profit_scan.add_argument("--steam-discount", type=float, default=DEFAULT_STEAM_BALANCE_DISCOUNT)
        t_profit_scan.add_argument(
            "--inventory-filter",
            type=normalize_inventory_filter,
            metavar="{all,all_cooldown,has_tradable}",
            default=INVENTORY_FILTER_ALL,
            help=(
                "库存筛选: "
                f"{INVENTORY_FILTER_ALL}={inventory_filter_label(INVENTORY_FILTER_ALL)}, "
                f"{INVENTORY_FILTER_ALL_COOLDOWN}={inventory_filter_label(INVENTORY_FILTER_ALL_COOLDOWN)}, "
                f"{INVENTORY_FILTER_HAS_TRADABLE}={inventory_filter_label(INVENTORY_FILTER_HAS_TRADABLE)}"
            ),
        )
        t_profit_scan.add_argument("--star-threshold", type=float, default=10.0, help="达到该收益率时在本地输出中标星")
        t_profit_scan.add_argument("--cache-max-age-minutes", type=int, default=180, help="允许使用的库存缓存最大时长")
        t_profit_scan.add_argument("--no-cache-fallback", action="store_true", help="库存拉取失败时不回退到缓存")
        t_profit_scan.add_argument("--dump-json", action="store_true", help="额外输出 JSON 结果")
        t_profit_scan.set_defaults(handler=cmd_t_profit_scan, top=10, bottom=None)

        t_profit_missing = t_profit_subparsers.add_parser("missing-steam", help="查看最近一次缺失 Steam 价格的明细")
        t_profit_missing.set_defaults(handler=cmd_t_yield_missing_steam_v2)

    add_t_profit_parser("t-profit")
    add_t_profit_parser("t-yield", hidden=True)

    # ---- pool (C5 inventory cooldown lookup) ----
    pool = subparsers.add_parser(
        "pool",
        help="C5库存查询 / 执行器挂刀报表",
        description=(
            "常用：\n"
            "  python .\\main.py pool item\n"
            "  python .\\main.py pool guadao-report --from 2026-06-01T02 --to 2026-06-01T23"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    pool_subparsers = pool.add_subparsers(dest="pool_command", required=True)

    pool_item = pool_subparsers.add_parser(
        "item",
        help="查询 C5 API 全库存中符合 guadaoItemScope 的广义箱子可交易/冷却状态",
        description=(
            "直接读取 C5 API 的全部绑定 Steam 库存，按当前 guadaoItemScope 过滤。\n"
            "case_only 会识别 CSGO-API crates，以及 Case/Capsule/Package/Container 等广义箱子。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    pool_item.set_defaults(
        handler=cmd_pool_inventory,
        scope=None,
        days=7,
        cache_fallback=False,
        cache_max_age_minutes=180,
        dump_json=False,
    )

    pool_guadao_report = pool_subparsers.add_parser(
        "guadao-report",
        help="查看执行器挂刀余额折扣报表",
        description=(
            "按日期范围统计执行器挂刀闭环流水。\n\n"
            "总折比 = C5补仓现金 / Steam税后到手余额。\n"
            "面值折比 = C5补仓现金 / Steam税前售价。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    pool_guadao_report.add_argument(
        "--from",
        dest="date_from",
        required=True,
        help="开始日期/时间，例如 2026-05-10 或 2026-05-10T08",
    )
    pool_guadao_report.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="结束日期/时间，默认到现在；支持精确到小时，例如 2026-05-10T17",
    )
    pool_guadao_report.add_argument("--item", dest="market_hash_name", default=None, help="只统计某个 market_hash_name")
    pool_guadao_report.add_argument("--detail", action="store_true", help="显示每笔闭环明细")
    pool_guadao_report.add_argument("--dump-json", action="store_true", help="输出 JSON")
    pool_guadao_report.set_defaults(handler=cmd_pool_guadao_report)

    # ---- executor ----
    executor = subparsers.add_parser(
        "executor",
        help="Auto execution runner",
        description="Run automated execution (list/guadao + transfer).",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    executor_subparsers = executor.add_subparsers(dest="executor_command", required=True)

    executor_start = executor_subparsers.add_parser(
        "start",
        help="Start execution loop",
        description="Run scan + execute loop for list/guadao and transfer paths.",
    )
    executor_start.add_argument("--once", action="store_true", help="Run once and exit")
    executor_start.add_argument("--dry-run", action="store_true", help="Force dry run")
    executor_start.add_argument("--no-dry-run", action="store_true", help="Force real execution")
    executor_start.add_argument("--enable", action="store_true", help="Enable execution for this run")
    executor_start.add_argument("--disable", action="store_true", help="Disable execution for this run")
    executor_start.add_argument("--max-list", type=int, help="Max listings per cycle")
    executor_start.add_argument(
        "--max-transfer-buy",
        "--max-buy",
        dest="max_transfer_buy",
        type=int,
        help="Max transfer buys per cycle",
    )
    executor_start.add_argument("--force-refresh", action="store_true", help="Force steam refresh")
    executor_start.add_argument("--no-force-refresh", action="store_true", help="Disable steam refresh")
    executor_start.set_defaults(handler=cmd_executor_start)

    executor_status = executor_subparsers.add_parser(
        "status",
        help="Show executor pool status",
        description="Show current pool states including transfer execution states.",
    )
    executor_status.add_argument(
        "--status-filter",
        default=None,
        help=(
            "Filter by status: holding, listed, sold, pending_rebuy, listing_pending, "
            "transfer_buying, transfer_holding, transfer_listed_c5, transfer_sold"
        ),
    )
    executor_status.set_defaults(handler=cmd_executor_status)

    # ---- steam tools ----
    steam = subparsers.add_parser(
        "steam",
        help="Steam tools",
        description="Steam market auth/listings/confirm helpers.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    steam_subparsers = steam.add_subparsers(dest="steam_command", required=True)

    steam_auth = steam_subparsers.add_parser("auth-check", help="Check Steam auth")
    steam_auth.set_defaults(handler=cmd_steam_auth_check)

    steam_login = steam_subparsers.add_parser("login", help="登录 Steam 并保存 Cookie")
    steam_login.add_argument("--account", required=True, help="账户名")
    steam_login.add_argument("--browser", action="store_true", help="使用 Playwright 浏览器登录")
    steam_login.set_defaults(handler=cmd_steam_login)

    steam_listings = steam_subparsers.add_parser("listings", help="List Steam listings")
    steam_listings.add_argument("--count", type=int, default=20)
    steam_listings.set_defaults(handler=cmd_steam_listings)

    steam_confirm = steam_subparsers.add_parser("confirm", help="Confirm pending listings")
    steam_confirm.set_defaults(handler=cmd_steam_confirm)

    steam_test_list = steam_subparsers.add_parser(
        "test-list",
        help="测试单个 asset 是否能被 Steam sellitem 接受，默认高价上架后撤单",
    )
    steam_test_list.add_argument("--asset-id", required=True)
    steam_test_list.add_argument("--price", type=float, default=999.0, help="测试挂价，默认 999 CNY")
    steam_test_list.add_argument("--context-id", default=None)
    steam_test_list.add_argument("--no-confirm", action="store_true", help="不自动确认测试挂单")
    steam_test_list.add_argument("--no-cancel", action="store_true", help="成功后不自动撤单")
    steam_test_list.set_defaults(handler=cmd_steam_test_list)

    steam_cookie_refresh = steam_subparsers.add_parser("cookie-refresh", help="验证并自动刷新当前账户 Cookie")
    steam_cookie_refresh.set_defaults(handler=cmd_steam_cookie_refresh)

    steam_price_cache = steam_subparsers.add_parser("price-cache", help="Manage steam price cache")
    steam_price_cache_sub = steam_price_cache.add_subparsers(dest="price_cache_command", required=True)

    steam_price_cache_show = steam_price_cache_sub.add_parser("show", help="Show price cache")
    steam_price_cache_show.set_defaults(handler=cmd_steam_price_cache_show)

    steam_price_cache_clear = steam_price_cache_sub.add_parser("clear", help="Clear price cache")
    steam_price_cache_clear.add_argument("--name", help="Market hash name")
    steam_price_cache_clear.set_defaults(handler=cmd_steam_price_cache_clear)

    steam_price_check = steam_subparsers.add_parser("price-check", help="Check steam price by name")
    steam_price_check.add_argument("market_hash_name")
    steam_price_check.set_defaults(handler=cmd_steam_price_check)

    notify = subparsers.add_parser(
        "notify",
        help="提醒模块入口",
        description=(
            "提醒模块入口。\n\n"
            "常用：\n"
            "  python .\\main.py notify t-profit -h\n"
            "  python .\\main.py notify t-profit --configure\n"
            "  python .\\main.py notify t-profit --show-config\n"
            "  python .\\main.py notify t-profit --once"
        ),
        epilog="提示：要看做T提醒的完整参数，请执行 `python .\\main.py notify t-profit -h`。",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    notify_subparsers = notify.add_subparsers(dest="notify_command", required=True)

    def add_notify_t_profit_parser(name: str, *, hidden: bool = False) -> None:
        notify_t_profit = notify_subparsers.add_parser(
            name,
            help="兼容旧命令（不推荐）" if hidden else "做T提醒",
            description=(
                "做T提醒命令。\n\n"
                "说明：\n"
                "  --configure      重新配置提醒参数\n"
                "  --show-config    查看当前提醒配置\n"
                "  --show-missing-steam  查看最近一次 Steam 缺价明细\n"
                "  --once           仅执行一次扫描和提醒判断"
            ),
            formatter_class=argparse.RawTextHelpFormatter,
        )
        notify_t_profit.add_argument("--configure", action="store_true", help="重新配置做T提醒参数")
        notify_t_profit.add_argument("--once", action="store_true", help="只执行一次提醒判断")
        notify_t_profit.add_argument("--show-config", action="store_true", help="输出当前提醒配置")
        notify_t_profit.add_argument("--show-missing-steam", action="store_true", help="输出最近一次缺失 Steam 价格的明细")
        notify_t_profit.set_defaults(handler=cmd_notify_t_profit)

    add_notify_t_profit_parser("t-profit")
    add_notify_t_profit_parser("t-yield", hidden=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

