from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from cs2_assistant.catalog import load_steamdt_catalog
from cs2_assistant.clients import C5GameClient, CSQAQClient, ServerChanClient, SteamDTClient
from cs2_assistant.config import Settings, load_settings
from cs2_assistant.db import Database
from cs2_assistant.reminders.t_yield import main as t_yield_reminder_main
from cs2_assistant.services import AlertService, MarketService, NotificationService
from cs2_assistant.models import (
    STRATEGY_GUADAO,
    STRATEGY_HOLD,
    STRATEGY_LABELS,
    STRATEGY_TRANSFER,
    StrategyConfig,
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
from cs2_assistant.services.t_yield_alerts import build_t_yield_notification
from cs2_assistant.services.t_yield_scan import (
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
from cs2_assistant.utils import ensure_parent_dir, safe_float, utc_now_iso


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    if getattr(args, "db_path", None):
        settings.db_path = Path(args.db_path)
    return settings


def _open_db(settings: Settings) -> Database:
    db = Database(settings.db_path)
    db.initialize()
    return db


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

    raise RuntimeError("鏈兘浠?C5 璐﹀彿淇℃伅閲岃В鏋愬埌 Steam ID锛岃鎵嬪姩浼犲叆 --steam-id")


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
        raise RuntimeError("鏈壘鍒扮粦瀹氱殑 Steam 璐﹀彿")

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
        confirm = input("杈撳叆 YES 纭涓嬪崟锛屽叾瀹冧换鎰忛敭鍙栨秷: ")
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
            print(
                f"{marker} {index}. {candidate.name} | 收益率 {candidate.t_yield_pct:.2f}% | "
                f"{candidate.inventory_status_summary} | 折算比 {candidate.ratio:.4f} | "
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


def cmd_pool_scan(args: argparse.Namespace) -> int:
    """Scan inventory pool and evaluate strategies for each item type."""
    settings = _settings_from_args(args)
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
        print(f"=== 挂刀做T 候选 (listing_ratio 低优先, Top {top_n}) ===")
        for i, c in enumerate(report.guadao_candidates[:top_n], 1):
            strategies_str = "+".join(STRATEGY_LABELS.get(s, s) for s in c.recommended_strategies)
            star = "★" if c.listing_ratio <= args.star_threshold else " "
            print(
                f"{star}{i:>3}. {c.name} | "
                f"listing_ratio {c.listing_ratio:.4f} | "
                f"transfer_ratio {c.transfer_real_ratio_pct:+.2f}% | "
                f"补仓 ¥{c.rebuy_price:.2f} | "
                f"Steam ¥{c.steam_sell_price:.2f} → ¥{c.steam_after_tax_price:.2f} | "
                f"余额差 ¥{c.guadao_profit_per_unit:.2f} | "
                f"库存 {c.inventory_count} ({c.tradable_count}可交易) | "
                f"策略 [{strategies_str}]"
            )
        print()

    # Show transfer candidates
    if report.transfer_candidates:
        print(f"=== 导余额做T 候选 (transfer_real_ratio 高优先, Top {top_n}) ===")
        for i, c in enumerate(report.transfer_candidates[:top_n], 1):
            strategies_str = "+".join(STRATEGY_LABELS.get(s, s) for s in c.recommended_strategies)
            star = "★" if c.transfer_real_ratio >= 0.10 else " "
            print(
                f"{star}{i:>3}. {c.name} | "
                f"transfer_ratio {c.transfer_real_ratio_pct:+.2f}% | "
                f"listing_ratio {c.listing_ratio:.4f} | "
                f"C5 ¥{c.rebuy_price:.2f} | "
                f"Steam ¥{c.steam_sell_price:.2f} | "
                f"导余额利润 ¥{c.transfer_profit_per_unit:.2f} | "
                f"库存 {c.inventory_count} ({c.tradable_count}可交易) | "
                f"策略 [{strategies_str}]"
            )
        print()

    # Show hold items (no strategy fits)
    if report.hold_items and args.show_hold:
        print(f"=== 持有 (不满足任何策略, 共 {report.hold_count} 个) ===")
        for i, c in enumerate(report.hold_items[:top_n], 1):
            print(
                f"  {i:>3}. {c.name} | "
                f"listing_ratio {c.listing_ratio:.4f} | "
                f"transfer_ratio {c.transfer_real_ratio_pct:+.2f}% | "
                f"C5 ¥{c.rebuy_price:.2f} | Steam ¥{c.steam_sell_price:.2f}"
            )
        print()

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


def cmd_pool_sync(args: argparse.Namespace) -> int:
    """Sync inventory pool from C5 inventory."""
    settings = _settings_from_args(args)
    if not settings.c5_api_key:
        print("缺少 C5GAME_API_KEY / C5_API_KEY 环境变量。", file=sys.stderr)
        return 1

    from cs2_assistant.clients import C5GameClient
    from cs2_assistant.services.t_yield_scan import fetch_all_c5_inventories, summarize_inventory_types

    c5_client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    inventory_payload = fetch_all_c5_inventories(
        c5_client,
        settings,
        allow_cached_fallback=False,
        cache_max_age_minutes=None,
    )
    all_types = summarize_inventory_types(list(inventory_payload.get("list") or []))
    db = _open_db(settings)
    count = db.sync_pool_from_inventory(all_types)
    db.close()
    print(f"底仓同步完成: {count} 个饰品类型已更新到 inventory_pool 表。")
    return 0


def cmd_pool_status(args: argparse.Namespace) -> int:
    """Show current inventory pool status."""
    settings = _settings_from_args(args)
    db = _open_db(settings)
    pool_items = db.list_pool_items(status=args.status_filter)
    db.close()

    if not pool_items:
        print("底仓为空。使用 `pool sync` 从 C5 库存同步。")
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

        config.steam_net_factor = _prompt_float("Steam 税后系数 (steam_net_factor)", config.steam_net_factor)
        config.c5_settlement_factor = _prompt_float("C5 结算系数 (c5_settlement_factor)", config.c5_settlement_factor)
        config.balance_discount = _prompt_float("余额折扣率 (balance_discount)", config.balance_discount)
        config.guadao_max_listing_ratio = _prompt_float("挂刀阈值 listing_ratio ≤ (guadao_max_listing_ratio)", config.guadao_max_listing_ratio)
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
            report = scan_strategies(settings, config)
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
            alert_lines.append(
                f"挂刀 | {c.name} | ratio={c.listing_ratio:.4f} | "
                f"补仓¥{c.rebuy_price:.2f} | Steam¥{c.steam_after_tax_price:.2f} | "
                f"差额¥{c.guadao_profit_per_unit:.2f}"
            )
        for c in report.transfer_candidates[:5]:
            alert_lines.append(
                f"导余额 | {c.name} | ratio={c.transfer_real_ratio_pct:+.2f}% | "
                f"C5¥{c.rebuy_price:.2f} | Steam¥{c.steam_sell_price:.2f} | "
                f"利润¥{c.transfer_profit_per_unit:.2f}"
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

    # ---- pool (底仓策略) ----
    pool = subparsers.add_parser(
        "pool",
        help="底仓策略系统（挂刀做T / 导余额做T）",
        description=(
            "基于固定库存池的自动化 T 工具。\n\n"
            "两种策略共用同一批底仓:\n"
            "  挂刀做T: listing_ratio 低 → 卖 Steam，低价补仓\n"
            "  导余额做T: transfer_real_ratio 高 → 利用低价余额赚钱\n\n"
            "常用:\n"
            "  python .\\main.py pool scan\n"
            "  python .\\main.py pool scan --top-n 20 --min-price 10\n"
            "  python .\\main.py pool sync\n"
            "  python .\\main.py pool status\n"
            "  python .\\main.py pool config\n"
            "  python .\\main.py pool config --edit\n"
            "  python .\\main.py pool monitor\n"
            "  python .\\main.py pool monitor --once"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    pool_subparsers = pool.add_subparsers(dest="pool_command", required=True)

    pool_scan = pool_subparsers.add_parser(
        "scan",
        help="扫描底仓，评估挂刀/导余额策略",
        description=(
            "扫描所有绑定 Steam 账号的库存，计算 listing_ratio 和 transfer_real_ratio，\n"
            "将每个饰品分类到挂刀做T / 导余额做T / 持有。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    pool_scan.add_argument("--min-price", type=float, default=None, help="最低价格过滤（覆盖配置文件）")
    pool_scan.add_argument("--guadao-max-ratio", type=float, default=None, help="挂刀 listing_ratio 阈值（覆盖配置文件）")
    pool_scan.add_argument("--transfer-min-ratio", type=float, default=None, help="导余额 transfer_real_ratio 阈值（覆盖配置文件）")
    pool_scan.add_argument("--steam-net-factor", type=float, default=None, help="Steam 税后系数（覆盖配置文件）")
    pool_scan.add_argument("--top-n", type=int, default=None, help="每种策略输出前 N 个")
    pool_scan.add_argument("--star-threshold", type=float, default=0.90, help="listing_ratio 低于该值时标星")
    pool_scan.add_argument("--show-hold", action="store_true", help="显示持有（不满足任何策略）的饰品")
    pool_scan.add_argument("--cache-max-age-minutes", type=int, default=180, help="库存缓存最大时长")
    pool_scan.add_argument("--no-cache-fallback", action="store_true", help="不回退到缓存")
    pool_scan.add_argument("--save-eval", action="store_true", help="保存评估记录到数据库")
    pool_scan.add_argument("--dump-json", action="store_true", help="输出 JSON 结果")
    pool_scan.set_defaults(handler=cmd_pool_scan)

    pool_sync = pool_subparsers.add_parser("sync", help="从 C5 库存同步底仓")
    pool_sync.set_defaults(handler=cmd_pool_sync)

    pool_status = pool_subparsers.add_parser("status", help="查看底仓状态")
    pool_status.add_argument("--status-filter", default=None, help="按状态过滤: holding, listed, sold, pending_rebuy")
    pool_status.set_defaults(handler=cmd_pool_status)

    pool_config = pool_subparsers.add_parser(
        "config",
        help="查看或编辑策略配置",
        description="查看或交互式编辑策略参数（阈值、系数等）。",
    )
    pool_config.add_argument("--edit", action="store_true", help="交互式编辑配置")
    pool_config.set_defaults(handler=cmd_pool_config)

    pool_monitor = pool_subparsers.add_parser(
        "monitor",
        help="持续监控底仓策略",
        description="按配置的轮询间隔持续扫描底仓策略，发现机会时通过 ServerChan 推送。",
    )
    pool_monitor.add_argument("--once", action="store_true", help="仅执行一次后退出")
    pool_monitor.set_defaults(handler=cmd_pool_monitor)

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

