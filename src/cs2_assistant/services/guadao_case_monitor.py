from __future__ import annotations

import csv
import json
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cs2_assistant.accounts import AccountStore
from cs2_assistant.catalog import is_csgo_api_weapon_case
from cs2_assistant.clients import C5GameClient, SteamMarketClient
from cs2_assistant.clients.steam_market import SteamMarketError
from cs2_assistant.config import PROJECT_ROOT, Settings
from cs2_assistant.db import Database
from cs2_assistant.models import StrategyConfig, looks_like_weapon_case_name
from cs2_assistant.services.market import calculate_listing_ratio, calculate_steam_after_tax
from cs2_assistant.services.pricing import choose_orderbook_price, summarize_orderbook_prices
from cs2_assistant.utils import chunked, ensure_parent_dir, safe_float, safe_int, utc_now_iso


DEFAULT_RATIO_BUCKET_SIZE = 0.005
DEFAULT_REPORT_HOURS = 24
DEFAULT_COLLECTION_INTERVAL_MINUTES = 5.0
STEAM_ORDERBOOK_PRICE_PARSER_VERSION = 2

CRATE_TYPE_WEAPON_CASE = "weapon_case"
CRATE_TYPE_CAPSULE = "capsule"
CRATE_TYPE_SOUVENIR_PACKAGE = "souvenir_package"
CRATE_TYPE_CONTAINER = "container"
CRATE_TYPE_CRATE = "crate"
CRATE_TYPE_OTHER = "other"

CRATE_TYPE_LABELS = {
    CRATE_TYPE_WEAPON_CASE: "武器箱",
    CRATE_TYPE_CAPSULE: "胶囊",
    CRATE_TYPE_SOUVENIR_PACKAGE: "纪念包",
    CRATE_TYPE_CONTAINER: "容器",
    CRATE_TYPE_CRATE: "箱子",
    CRATE_TYPE_OTHER: "其他",
}


@dataclass(slots=True)
class CaseMonitorTarget:
    market_hash_name: str
    name_cn: str | None = None
    c5_item_id: str | None = None
    crate_type: str = CRATE_TYPE_OTHER


@dataclass(slots=True)
class CaseRatioSnapshot:
    market_hash_name: str
    observed_at: str
    name_cn: str | None = None
    c5_sell_price: float | None = None
    c5_sell_count: int | None = None
    steam_list_price: float | None = None
    steam_wall_price: float | None = None
    steam_after_tax_price: float | None = None
    listing_ratio: float | None = None
    c5_price_source: str | None = None
    steam_price_source: str | None = None
    status: str = "ok"
    error: str | None = None
    raw_json: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "market_hash_name": self.market_hash_name,
            "observed_at": self.observed_at,
            "name_cn": self.name_cn,
            "c5_sell_price": self.c5_sell_price,
            "c5_sell_count": self.c5_sell_count,
            "steam_list_price": self.steam_list_price,
            "steam_wall_price": self.steam_wall_price,
            "steam_after_tax_price": self.steam_after_tax_price,
            "listing_ratio": self.listing_ratio,
            "c5_price_source": self.c5_price_source,
            "steam_price_source": self.steam_price_source,
            "status": self.status,
            "error": self.error,
            "raw_json": self.raw_json or {},
        }


@dataclass(slots=True)
class RatioSegment:
    market_hash_name: str
    ratio: float
    started_at: datetime
    ended_at: datetime

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.ended_at - self.started_at).total_seconds())


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _floor_to_bucket(value: float, bucket_size: float) -> float:
    return math.floor(value / bucket_size) * bucket_size


def _ceil_ratio(value: float, places: int = 4) -> float:
    scale = 10**places
    return math.ceil(value * scale) / scale


def _duration_label(seconds: float) -> str:
    total_minutes = int(round(max(0.0, seconds) / 60.0))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _weighted_percentile(values: list[tuple[float, float]], percentile: float) -> float | None:
    weighted = [(float(value), max(0.0, float(weight))) for value, weight in values if weight > 0]
    if not weighted:
        return None
    weighted.sort(key=lambda row: row[0])
    total = sum(weight for _, weight in weighted)
    if total <= 0:
        return None
    threshold = total * percentile
    cumulative = 0.0
    for value, weight in weighted:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return weighted[-1][0]


def _raw_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _steam_parser_version(raw_json: dict[str, Any]) -> int:
    config = raw_json.get("config") if isinstance(raw_json.get("config"), dict) else {}
    version = safe_int(config.get("steamOrderbookPriceParserVersion"))
    return int(version or 1)


def _price_offset_from_raw(raw_json: dict[str, Any]) -> float:
    config = raw_json.get("config") if isinstance(raw_json.get("config"), dict) else {}
    case_offset = safe_float(config.get("caseListingPriceOffset"))
    if case_offset is not None:
        return case_offset
    listing_offset = safe_float(config.get("listingPriceOffset"))
    return listing_offset if listing_offset is not None else 0.01


def _correct_legacy_minor_unit_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_json = _raw_dict(row.get("raw_json"))
    if _steam_parser_version(raw_json) >= STEAM_ORDERBOOK_PRICE_PARSER_VERSION:
        row["legacySteamMinorUnitCorrected"] = False
        return row

    wall_price = safe_float(row.get("steam_wall_price"))
    list_price = safe_float(row.get("steam_list_price"))
    ratio = safe_float(row.get("listing_ratio"))
    c5_price = safe_float(row.get("c5_sell_price"))
    config = raw_json.get("config") if isinstance(raw_json.get("config"), dict) else {}
    steam_net_factor = safe_float(config.get("steamNetFactor")) or 0.869

    # Parser v1 treated compact orderbook values below 100 as whole CNY
    # instead of fen. Only repair rows that are clearly in that failure mode:
    # sub-100 wall price plus an impossible near-zero ratio.
    if (
        wall_price is None
        or list_price is None
        or ratio is None
        or c5_price is None
        or wall_price <= 0
        or wall_price >= 100
        or ratio >= 0.05
    ):
        row["legacySteamMinorUnitCorrected"] = False
        return row

    corrected_wall = wall_price / 100.0
    corrected_list = max(0.01, corrected_wall - _price_offset_from_raw(raw_json))
    corrected_after_tax = calculate_steam_after_tax(
        corrected_list,
        steam_net_factor=steam_net_factor,
    )
    corrected_ratio = calculate_listing_ratio(
        c5_price,
        corrected_list,
        steam_net_factor=steam_net_factor,
    )
    row = dict(row)
    row["steam_wall_price"] = corrected_wall
    row["steam_list_price"] = corrected_list
    row["steam_after_tax_price"] = corrected_after_tax
    row["listing_ratio"] = corrected_ratio
    row["legacySteamMinorUnitCorrected"] = True
    return row


def _case_type_lookup(db: Database) -> dict[str, dict[str, str]]:
    rows = db.conn.execute(
        """
        SELECT market_hash_name, name_cn, raw_json
        FROM items
        """
    ).fetchall()
    lookup: dict[str, dict[str, str]] = {}
    for row in rows:
        raw_json = _raw_dict(row["raw_json"])
        market_hash_name = str(row["market_hash_name"] or "")
        crate_type = classify_case_monitor_target(
            raw_json,
            market_hash_name=market_hash_name,
            name_cn=str(row["name_cn"] or ""),
        )
        lookup[market_hash_name] = {
            "crateType": crate_type,
            "crateTypeLabel": CRATE_TYPE_LABELS.get(crate_type, crate_type),
        }
    return lookup


def _is_broad_case_name(value: str | None) -> bool:
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


def classify_case_monitor_target(
    raw_json: dict[str, Any],
    *,
    market_hash_name: str,
    name_cn: str | None = None,
) -> str:
    values = [
        market_hash_name,
        name_cn,
        raw_json.get("marketHashName"),
        raw_json.get("market_hash_name"),
        raw_json.get("name"),
        raw_json.get("typeName"),
        raw_json.get("type"),
    ]
    normalized_values = [str(value or "").strip().lower() for value in values]
    joined_cn = " ".join(str(value or "") for value in values)

    if (
        any(looks_like_weapon_case_name(str(value or "")) for value in values)
        or any(value == "case" for value in normalized_values)
        or any("weaponcase" in value for value in normalized_values)
        or "武器箱" in joined_cn
    ):
        return CRATE_TYPE_WEAPON_CASE
    if any("souvenir" in value for value in normalized_values) or "纪念包" in joined_cn:
        return CRATE_TYPE_SOUVENIR_PACKAGE
    if any("capsule" in value for value in normalized_values) or "胶囊" in joined_cn:
        return CRATE_TYPE_CAPSULE
    if any(value.endswith(" container") or value == "container" for value in normalized_values):
        return CRATE_TYPE_CONTAINER
    if isinstance(raw_json.get("csgoApi"), dict) and is_csgo_api_weapon_case(raw_json):
        return CRATE_TYPE_CRATE
    return CRATE_TYPE_OTHER


def is_case_monitor_target(raw_json: dict[str, Any], *, market_hash_name: str, name_cn: str | None = None) -> bool:
    if isinstance(raw_json.get("csgoApi"), dict) and is_csgo_api_weapon_case(raw_json):
        return True
    for value in (
        market_hash_name,
        name_cn,
        raw_json.get("marketHashName"),
        raw_json.get("market_hash_name"),
        raw_json.get("name"),
        raw_json.get("typeName"),
        raw_json.get("type"),
    ):
        if _is_broad_case_name(str(value or "")):
            return True
    type_name = str(raw_json.get("typeName") or raw_json.get("type") or "")
    return "weaponcase" in type_name.lower()


def list_case_monitor_targets(
    db: Database,
    *,
    market_hash_names: list[str] | None = None,
    limit: int | None = None,
) -> list[CaseMonitorTarget]:
    if market_hash_names:
        rows = []
        for market_hash_name in market_hash_names:
            item = db.get_item(market_hash_name)
            if item is None:
                rows.append(
                    {
                        "market_hash_name": market_hash_name,
                        "name_cn": market_hash_name,
                        "c5_item_id": None,
                        "raw_json": "{}",
                    }
                )
            else:
                rows.append(dict(item))
    else:
        rows = [
            dict(row)
            for row in db.conn.execute(
                """
                SELECT market_hash_name, name_cn, c5_item_id, raw_json
                FROM items
                ORDER BY market_hash_name ASC
                """
            ).fetchall()
        ]

    targets: list[CaseMonitorTarget] = []
    seen: set[str] = set()
    for row in rows:
        market_hash_name = str(row.get("market_hash_name") or "").strip()
        if not market_hash_name or market_hash_name in seen:
            continue
        try:
            raw_json = json.loads(str(row.get("raw_json") or "{}"))
        except ValueError:
            raw_json = {}
        if market_hash_names is None and not is_case_monitor_target(
            raw_json,
            market_hash_name=market_hash_name,
            name_cn=str(row.get("name_cn") or ""),
        ):
            continue
        seen.add(market_hash_name)
        targets.append(
            CaseMonitorTarget(
                market_hash_name=market_hash_name,
                name_cn=str(row.get("name_cn") or market_hash_name),
                c5_item_id=str(row.get("c5_item_id") or "") or None,
                crate_type=classify_case_monitor_target(
                    raw_json,
                    market_hash_name=market_hash_name,
                    name_cn=str(row.get("name_cn") or ""),
                ),
            )
        )
        if limit is not None and len(targets) >= limit:
            break
    return targets


def build_steam_clients_for_monitor(settings: Settings) -> list[SteamMarketClient]:
    store = AccountStore(PROJECT_ROOT / "config")
    current = store.get_current()
    accounts = store.list_accounts()
    ordered_accounts = []
    if current is not None:
        ordered_accounts.append(current)
    ordered_accounts.extend(account for account in accounts if current is None or account.id != current.id)

    clients: list[SteamMarketClient] = []
    for account in ordered_accounts:
        if not account.cookies:
            continue
        try:
            clients.append(
                SteamMarketClient(
                    cookies=account.cookies,
                    steam_id64=account.steam_id64,
                    identity_secret=account.identity_secret,
                    device_id=account.device_id,
                    account_id=account.id,
                    base_url=settings.steam_market_base_url,
                    request_source="guadao_monitor",
                )
            )
        except SteamMarketError:
            continue

    if not clients and settings.steam_cookies:
        clients.append(
            SteamMarketClient(
                cookies=settings.steam_cookies,
                base_url=settings.steam_market_base_url,
                request_source="guadao_monitor",
            )
        )
    return clients


def _load_c5_prices(
    c5_client: C5GameClient,
    targets: list[CaseMonitorTarget],
    *,
    app_id: int,
) -> dict[str, dict[str, Any]]:
    prices: dict[str, dict[str, Any]] = {}
    for batch in chunked([target.market_hash_name for target in targets], 100):
        data = c5_client.price_batch(batch, app_id=app_id)
        for market_hash_name, payload in data.items():
            if isinstance(payload, dict):
                prices[str(market_hash_name)] = payload
    return prices


def _compact_orderbook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("success", "sell_order_count", "buy_order_count"):
        if key in payload:
            compact[key] = payload.get(key)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if isinstance(data, dict):
        for key in ("eCurrency", "amtMinSellOrder", "amtMaxBuyOrder", "cSellOrders", "cBuyOrders"):
            if key in data:
                compact[key] = data.get(key)
        sell_rows = data.get("rgCompactSellOrders")
        if isinstance(sell_rows, list):
            compact["rgCompactSellOrdersHead"] = sell_rows[:80]
            if sell_rows and not all(isinstance(row, list) for row in sell_rows):
                compact["rgCompactSellOrdersHeadPairs"] = [
                    sell_rows[index : index + 2]
                    for index in range(0, min(len(sell_rows), 80), 2)
                    if index + 1 < len(sell_rows)
                ]
            else:
                compact["rgCompactSellOrdersHeadPairs"] = sell_rows[:40]
    return compact


def _fetch_steam_decision(
    steam_clients: list[SteamMarketClient],
    *,
    app_id: int,
    market_hash_name: str,
    wall_min_count: int,
    price_offset: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    for client in steam_clients:
        account_label = getattr(client, "account_id", None) or getattr(client, "steam_id64", None) or "steam"
        try:
            payload = client.order_book(app_id=app_id, market_hash_name=market_hash_name)
        except Exception as exc:
            errors.append(f"{account_label}: {exc}")
            continue
        decision = choose_orderbook_price(
            payload or {},
            wall_min_count=wall_min_count,
            price_offset=price_offset,
            min_price=0.01,
        )
        if decision is None:
            errors.append(f"{account_label}: empty sell orderbook")
            continue
        summary = summarize_orderbook_prices(
            payload or {},
            wall_min_count=wall_min_count,
            price_offset=price_offset,
            min_price=0.01,
        )
        return (
            {
                "payload": payload,
                "decision": {
                    "listPrice": decision.list_price,
                    "wallPrice": decision.wall_price,
                    "reason": decision.reason,
                },
                "summary": summary.to_dict(),
                "account": str(account_label),
            },
            errors,
        )
    return None, errors


def collect_case_ratio_snapshots(
    *,
    settings: Settings,
    config: StrategyConfig,
    targets: list[CaseMonitorTarget],
    c5_client: C5GameClient,
    steam_clients: list[SteamMarketClient],
    observed_at: str | None = None,
    max_workers: int = 1,
) -> list[CaseRatioSnapshot]:
    if not targets:
        return []
    if not steam_clients:
        raise RuntimeError("No usable Steam cookie/client found for Steam orderbook.")

    observed = observed_at or utc_now_iso()
    c5_prices = _load_c5_prices(c5_client, targets, app_id=settings.app_id)
    case_offset = config.case_listing_price_offset
    price_offset = case_offset if case_offset is not None else config.listing_price_offset

    def build_snapshot(target: CaseMonitorTarget) -> CaseRatioSnapshot:
        c5_payload = c5_prices.get(target.market_hash_name) or {}
        c5_sell_price = safe_float(c5_payload.get("price"))
        c5_sell_count = safe_int(c5_payload.get("count"))
        raw: dict[str, Any] = {
            "c5": c5_payload,
            "target": {
                "crateType": target.crate_type,
                "crateTypeLabel": CRATE_TYPE_LABELS.get(target.crate_type, target.crate_type),
            },
            "config": {
                "steamNetFactor": config.steam_net_factor,
                "listingWallMinCount": config.listing_wall_min_count,
                "caseListingPriceOffset": config.case_listing_price_offset,
                "listingPriceOffset": config.listing_price_offset,
                "steamOrderbookPriceParserVersion": STEAM_ORDERBOOK_PRICE_PARSER_VERSION,
            },
        }
        if c5_sell_price is None or c5_sell_price <= 0:
            return CaseRatioSnapshot(
                market_hash_name=target.market_hash_name,
                name_cn=target.name_cn,
                observed_at=observed,
                c5_sell_price=c5_sell_price,
                c5_sell_count=c5_sell_count,
                c5_price_source="c5_api_batch",
                status="missing_c5",
                error="C5 price unavailable",
                raw_json=raw,
            )

        steam_result, steam_errors = _fetch_steam_decision(
            steam_clients,
            app_id=settings.app_id,
            market_hash_name=target.market_hash_name,
            wall_min_count=config.listing_wall_min_count,
            price_offset=price_offset,
        )
        if steam_errors:
            raw["steamErrors"] = steam_errors
        if steam_result is None:
            return CaseRatioSnapshot(
                market_hash_name=target.market_hash_name,
                name_cn=target.name_cn,
                observed_at=observed,
                c5_sell_price=c5_sell_price,
                c5_sell_count=c5_sell_count,
                c5_price_source="c5_api_batch",
                status="missing_steam",
                error=" | ".join(steam_errors) if steam_errors else "Steam orderbook unavailable",
                raw_json=raw,
            )

        decision = steam_result["decision"]
        steam_list_price = safe_float(decision.get("listPrice"))
        steam_wall_price = safe_float(decision.get("wallPrice"))
        steam_after_tax = calculate_steam_after_tax(
            steam_list_price,
            steam_net_factor=config.steam_net_factor,
        )
        listing_ratio = calculate_listing_ratio(
            c5_sell_price,
            steam_list_price,
            steam_net_factor=config.steam_net_factor,
        )
        raw["steam"] = {
            "account": steam_result.get("account"),
            "decision": decision,
            "summary": steam_result.get("summary") or {},
            "orderbook": _compact_orderbook_payload(steam_result.get("payload") or {}),
        }
        return CaseRatioSnapshot(
            market_hash_name=target.market_hash_name,
            name_cn=target.name_cn,
            observed_at=observed,
            c5_sell_price=c5_sell_price,
            c5_sell_count=c5_sell_count,
            steam_list_price=steam_list_price,
            steam_wall_price=steam_wall_price,
            steam_after_tax_price=steam_after_tax,
            listing_ratio=listing_ratio,
            c5_price_source="c5_api_batch",
            steam_price_source="steam_orderbook",
            status="ok" if listing_ratio is not None else "invalid_ratio",
            error=None if listing_ratio is not None else "Invalid listing ratio",
            raw_json=raw,
        )

    workers = max(1, int(max_workers))
    if workers == 1:
        return [build_snapshot(target) for target in targets]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(build_snapshot, targets))


def save_case_ratio_snapshots(db: Database, snapshots: list[CaseRatioSnapshot]) -> int:
    rows = [snapshot.to_row() for snapshot in snapshots]
    return db.save_guadao_case_ratio_snapshots(rows)


def _segments_for_rows(
    rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    expected_interval_seconds: float,
    max_gap_seconds: float,
) -> list[RatioSegment]:
    parsed_rows: list[tuple[datetime, float, str]] = []
    for row in rows:
        observed_at = _parse_iso_datetime(str(row.get("observed_at") or ""))
        ratio = safe_float(row.get("listing_ratio"))
        market_hash_name = str(row.get("market_hash_name") or "").strip()
        if observed_at is None or ratio is None or ratio <= 0 or not market_hash_name:
            continue
        parsed_rows.append((observed_at, ratio, market_hash_name))
    parsed_rows.sort(key=lambda row: row[0])

    segments: list[RatioSegment] = []
    for index, (observed_at, ratio, market_hash_name) in enumerate(parsed_rows):
        if observed_at >= end:
            continue
        next_at = parsed_rows[index + 1][0] if index + 1 < len(parsed_rows) else observed_at + timedelta(seconds=expected_interval_seconds)
        capped_end = min(next_at, observed_at + timedelta(seconds=max_gap_seconds), end)
        segment_start = max(observed_at, start)
        if capped_end <= segment_start:
            continue
        segments.append(
            RatioSegment(
                market_hash_name=market_hash_name,
                ratio=ratio,
                started_at=segment_start,
                ended_at=capped_end,
            )
        )
    return segments


def _duration_at_or_below(segments: list[RatioSegment], threshold: float | None) -> float:
    if threshold is None:
        return 0.0
    return sum(segment.duration_seconds for segment in segments if segment.ratio <= threshold)


def _ratio_thresholds(
    segments: list[RatioSegment],
    *,
    total_duration: float,
    p50: float | None,
    p75: float | None,
    p90: float | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, label, ratio in (
        ("conservative", "保守", p50),
        ("stable", "稳健", p75),
        ("aggressive", "激进", p90),
    ):
        if ratio is None:
            continue
        duration = _duration_at_or_below(segments, ratio)
        rows.append(
            {
                "key": key,
                "label": label,
                "ratio": _ceil_ratio(ratio),
                "durationSeconds": round(duration, 2),
                "durationMinutes": round(duration / 60.0, 2),
                "durationLabel": _duration_label(duration),
                "coveragePct": round((duration / total_duration) * 100.0 if total_duration > 0 else 0.0, 2),
            }
        )
    return rows


def _timeline_rows(
    segments: list[RatioSegment],
    *,
    start: datetime,
    range_seconds: float,
    bucket_size: float,
) -> list[dict[str, Any]]:
    if range_seconds <= 0:
        return []
    merged: list[dict[str, Any]] = []
    for segment in segments:
        lower = _floor_to_bucket(segment.ratio, bucket_size)
        bucket = f"{lower:.4f}-{lower + bucket_size:.4f}"
        previous = merged[-1] if merged else None
        if previous and previous["bucket"] == bucket and previous["endedAtRaw"] == segment.started_at:
            previous["endedAtRaw"] = segment.ended_at
            previous["durationSecondsRaw"] += segment.duration_seconds
            previous["weightedRatio"] += segment.ratio * segment.duration_seconds
            continue
        merged.append(
            {
                "startedAtRaw": segment.started_at,
                "endedAtRaw": segment.ended_at,
                "durationSecondsRaw": segment.duration_seconds,
                "weightedRatio": segment.ratio * segment.duration_seconds,
                "bucket": bucket,
                "colorRatio": lower,
            }
        )

    rows: list[dict[str, Any]] = []
    for segment in merged:
        segment_start = segment["startedAtRaw"]
        segment_end = segment["endedAtRaw"]
        duration = float(segment["durationSecondsRaw"])
        ratio = float(segment["weightedRatio"]) / duration if duration > 0 else float(segment["colorRatio"])
        rows.append(
            {
                "startedAt": segment_start.isoformat(),
                "endedAt": segment_end.isoformat(),
                "ratio": round(ratio, 4),
                "bucket": segment["bucket"],
                "durationSeconds": round(duration, 2),
                "durationLabel": _duration_label(duration),
                "leftPct": round(((segment_start - start).total_seconds() / range_seconds) * 100.0, 4),
                "widthPct": round((duration / range_seconds) * 100.0, 4),
            }
        )
    return rows


def _parse_price_history_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(" +0"):
        text = f"{text[:-3]} +0000"
    for fmt in (
        "%b %d %Y %H: %z",
        "%b %d %Y %H:%M %z",
        "%b %d %Y %z",
        "%b %d %Y",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _parse_price_history_liquidity(payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []

    volume_24h = 0
    volume_7d = 0
    latest_price: float | None = None
    latest_at: str | None = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        observed_at = _parse_price_history_time(row[0])
        price = safe_float(row[1])
        volume = safe_int(str(row[2]).replace(",", ""))
        if observed_at is None or volume is None:
            continue
        if price is not None:
            latest_price = price
            latest_at = observed_at.isoformat()
        age_seconds = (current - observed_at).total_seconds()
        if 0 <= age_seconds <= 24 * 3600:
            volume_24h += int(volume)
        if 0 <= age_seconds <= 7 * 24 * 3600:
            volume_7d += int(volume)

    avg_daily_7d = volume_7d / 7.0 if volume_7d else 0.0
    return {
        "steamVolume24h": volume_24h,
        "steamVolume7d": volume_7d,
        "steamAvgDailyVolume7d": round(avg_daily_7d, 2),
        "steamPriceHistoryPointCount": len(rows),
        "steamPriceHistoryLatestPrice": latest_price,
        "steamPriceHistoryLatestAt": latest_at,
    }


def _liquidity_level(volume_24h: int | None) -> tuple[str, str, float]:
    volume = int(volume_24h or 0)
    if volume >= 1000:
        return "high", "快", 1.0
    if volume >= 100:
        return "medium", "正常", 0.78
    if volume >= 20:
        return "low", "偏慢", 0.45
    if volume >= 5:
        return "very_low", "很慢", 0.18
    return "dry", "极慢", 0.06


def _select_report_reference(summary: dict[str, Any], *, volume_24h: int | None) -> dict[str, Any]:
    wall_price = safe_float(summary.get("sellerWallListPrice"))
    floor_price = safe_float(summary.get("sellerFloorPrice"))
    buyer_price = safe_float(summary.get("buyerMaxPrice"))
    wall_to_buyer = safe_float(summary.get("wallToBuyerRatio"))
    wall_to_floor = safe_float(summary.get("wallToFloorRatio"))
    spread_ratio = safe_float(summary.get("spreadRatio"))
    volume = int(volume_24h or 0)

    source = "sell_wall"
    source_label = "20墙挂价"
    price = wall_price
    reason = "seller wall"
    if buyer_price is not None and buyer_price > 0 and (
        volume < 50
        or (wall_to_buyer is not None and wall_to_buyer > 1.10)
        or (spread_ratio is not None and spread_ratio > 0.08)
    ):
        source = "buy_order"
        source_label = "最高求购"
        price = buyer_price
        reason = "low liquidity or wide spread"
    elif floor_price is not None and floor_price > 0 and (
        price is None
        or (wall_to_floor is not None and wall_to_floor > 1.05)
    ):
        source = "sell_floor"
        source_label = "最低在售"
        price = floor_price
        reason = "seller wall is above floor"
    elif price is None and buyer_price is not None and buyer_price > 0:
        source = "buy_order"
        source_label = "最高求购"
        price = buyer_price
        reason = "seller price unavailable"

    return {
        "steamReferenceSource": source,
        "steamReferenceSourceLabel": source_label,
        "steamReferencePrice": price,
        "steamReferenceReason": reason,
    }


def _ratio_for_reference(c5_price: Any, steam_price: Any, *, steam_net_factor: float) -> float | None:
    c5 = safe_float(c5_price)
    price = safe_float(steam_price)
    if c5 is None or price is None or c5 <= 0 or price <= 0:
        return None
    return calculate_listing_ratio(c5, price, steam_net_factor=steam_net_factor)


def enrich_case_ratio_report_with_steam_liquidity(
    report: dict[str, Any],
    *,
    settings: Settings,
    config: StrategyConfig,
    steam_clients: list[SteamMarketClient],
    recommendation_crate_type: str | None = "all",
    top_n: int = 10,
    max_workers: int = 4,
) -> dict[str, Any]:
    if not steam_clients:
        report["steamLiquidityStatus"] = "skipped_no_steam_client"
        return report
    rec_type = str(recommendation_crate_type or "all")
    items = list(report.get("items") or [])
    candidates = items
    if not candidates:
        report["steamLiquidityStatus"] = "skipped_no_candidates"
        return report

    price_offset = (
        config.case_listing_price_offset
        if config.case_listing_price_offset is not None
        else config.listing_price_offset
    )

    def fetch_liquidity(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        market_hash_name = str(item.get("marketHashName") or "")
        errors: list[str] = []
        for client in steam_clients:
            try:
                orderbook = client.order_book(app_id=settings.app_id, market_hash_name=market_hash_name)
                history = client.price_history(
                    app_id=settings.app_id,
                    market_hash_name=market_hash_name,
                    currency=config.steam_currency,
                )
            except Exception as exc:
                errors.append(str(exc))
                continue
            summary = summarize_orderbook_prices(
                orderbook or {},
                wall_min_count=config.listing_wall_min_count,
                price_offset=price_offset,
                min_price=0.01,
            ).to_dict()
            liquidity = _parse_price_history_liquidity(history or {})
            reference = _select_report_reference(
                summary,
                volume_24h=safe_int(liquidity.get("steamVolume24h")),
            )
            return market_hash_name, {
                **summary,
                **liquidity,
                **reference,
                "steamLiquidityError": None,
            }
        return market_hash_name, {"steamLiquidityError": " | ".join(errors) if errors else "unavailable"}

    workers = max(1, int(max_workers))
    if workers == 1:
        results = [fetch_liquidity(item) for item in candidates]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(fetch_liquidity, candidates))
    by_name = {name: payload for name, payload in results}

    for item in items:
        payload = by_name.get(str(item.get("marketHashName") or ""))
        if not payload:
            continue
        item.update(payload)
        volume_24h = safe_int(item.get("steamVolume24h"))
        level, label, liquidity_score = _liquidity_level(volume_24h)
        item["liquidityLevel"] = level
        item["liquidityLabel"] = label
        item["liquidityScore"] = liquidity_score
        selected_ratio = _ratio_for_reference(
            item.get("latestC5SellPrice"),
            item.get("steamReferencePrice"),
            steam_net_factor=config.steam_net_factor,
        )
        item["selectedReferenceRatio"] = round(selected_ratio, 4) if selected_ratio is not None else None
        stable_ratio = safe_float(item.get("recommendedMaxListingRatio"))
        if selected_ratio is not None and stable_ratio is not None:
            effective_ratio = max(stable_ratio, selected_ratio)
        else:
            effective_ratio = selected_ratio if selected_ratio is not None else stable_ratio
        item["effectiveRecommendedMaxListingRatio"] = _ceil_ratio(effective_ratio) if effective_ratio is not None else None
        coverage = max(0.0, min(1.0, float(item.get("coveragePct") or 0.0) / 100.0))
        ratio_score = max(0.0, 1.0 - float(item.get("effectiveRecommendedMaxListingRatio") or 1.0))
        stability_score = max(0.15, 1.0 - min(float(item.get("stddevRatio") or 0.0) / 0.03, 1.0) * 0.45)
        item["recommendationScore"] = round(ratio_score * coverage * liquidity_score * stability_score, 6)

    source = [
        item
        for item in items
        if rec_type == "all" or str(item.get("crateType") or "") == rec_type
    ]
    source.sort(
        key=lambda row: (
            -float(row.get("recommendationScore") or 0.0),
            float(row.get("effectiveRecommendedMaxListingRatio") or row.get("recommendedMaxListingRatio") or 9999),
            -float(row.get("steamVolume24h") or 0.0),
            str(row.get("marketHashName") or ""),
        )
    )
    report["recommendations"] = source[: max(0, int(top_n))]
    report["steamLiquidityStatus"] = "ok"
    report["steamLiquidityRefreshedAt"] = utc_now_iso()
    return report


def _summarize_item_segments(
    *,
    market_hash_name: str,
    rows: list[dict[str, Any]],
    segments: list[RatioSegment],
    range_seconds: float,
    bucket_size: float,
    start: datetime,
    crate_type: str = CRATE_TYPE_OTHER,
    crate_type_label: str | None = None,
) -> dict[str, Any] | None:
    if not segments:
        return None
    duration = sum(segment.duration_seconds for segment in segments)
    if duration <= 0:
        return None

    weighted_values = [(segment.ratio, segment.duration_seconds) for segment in segments]
    ratios = [segment.ratio for segment in segments]
    avg_ratio = sum(ratio * seconds for ratio, seconds in weighted_values) / duration
    variance = sum(((ratio - avg_ratio) ** 2) * seconds for ratio, seconds in weighted_values) / duration
    stddev = math.sqrt(max(0.0, variance))
    min_ratio = min(ratios)
    max_ratio = max(ratios)
    min_key = round(min_ratio, 4)
    max_key = round(max_ratio, 4)
    min_duration = sum(segment.duration_seconds for segment in segments if round(segment.ratio, 4) == min_key)
    max_duration = sum(segment.duration_seconds for segment in segments if round(segment.ratio, 4) == max_key)
    p50 = _weighted_percentile(weighted_values, 0.5)
    p75 = _weighted_percentile(weighted_values, 0.75)
    p90 = _weighted_percentile(weighted_values, 0.9)

    buckets: dict[str, dict[str, Any]] = {}
    for segment in segments:
        lower = _floor_to_bucket(segment.ratio, bucket_size)
        upper = lower + bucket_size
        label = f"{lower:.4f}-{upper:.4f}"
        bucket = buckets.setdefault(
            label,
            {
                "bucket": label,
                "lower": round(lower, 4),
                "upper": round(upper, 4),
                "durationSeconds": 0.0,
            },
        )
        bucket["durationSeconds"] += segment.duration_seconds
    bucket_rows = list(buckets.values())
    for bucket in bucket_rows:
        bucket["durationSeconds"] = round(float(bucket["durationSeconds"]), 2)
        bucket["durationMinutes"] = round(float(bucket["durationSeconds"]) / 60.0, 2)
        bucket["durationLabel"] = _duration_label(float(bucket["durationSeconds"]))
        bucket["coveragePct"] = round(
            (float(bucket["durationSeconds"]) / duration) * 100.0 if duration > 0 else 0.0,
            2,
        )
    bucket_rows.sort(key=lambda row: float(row["lower"]))
    dominant_buckets = sorted(bucket_rows, key=lambda row: (-float(row["durationSeconds"]), float(row["lower"])))[:5]

    latest_row = max(rows, key=lambda row: str(row.get("observed_at") or ""))
    recommended_ratio = _ceil_ratio(p75 if p75 is not None else avg_ratio)
    conservative_ratio = _ceil_ratio(p50 if p50 is not None else avg_ratio)
    aggressive_ratio = _ceil_ratio(p90 if p90 is not None else avg_ratio)
    coverage_ratio = duration / range_seconds if range_seconds > 0 else 0.0
    stability_penalty = min(stddev / max(bucket_size * 2.0, 0.0001), 1.0)
    recommendation_score = max(0.0, (1.0 - recommended_ratio)) * max(0.0, min(1.0, coverage_ratio)) * (1.0 - stability_penalty * 0.35)
    threshold_rows = _ratio_thresholds(
        segments,
        total_duration=duration,
        p50=p50,
        p75=p75,
        p90=p90,
    )
    corrected_count = len([row for row in rows if row.get("legacySteamMinorUnitCorrected")])

    return {
        "marketHashName": market_hash_name,
        "name": latest_row.get("name_cn") or market_hash_name,
        "crateType": crate_type,
        "crateTypeLabel": crate_type_label or CRATE_TYPE_LABELS.get(crate_type, crate_type),
        "sampleCount": len(rows),
        "okSampleCount": len([row for row in rows if row.get("status") == "ok"]),
        "legacySteamMinorUnitCorrectedCount": corrected_count,
        "firstObservedAt": min(str(row.get("observed_at") or "") for row in rows),
        "lastObservedAt": max(str(row.get("observed_at") or "") for row in rows),
        "latestRatio": round(float(latest_row.get("listing_ratio") or 0.0), 4),
        "latestC5SellPrice": latest_row.get("c5_sell_price"),
        "latestSteamListPrice": latest_row.get("steam_list_price"),
        "latestSteamAfterTaxPrice": latest_row.get("steam_after_tax_price"),
        "minRatio": round(min_ratio, 4),
        "minRatioDurationSeconds": round(min_duration, 2),
        "minRatioDurationMinutes": round(min_duration / 60.0, 2),
        "minRatioDurationLabel": _duration_label(min_duration),
        "maxRatio": round(max_ratio, 4),
        "maxRatioDurationSeconds": round(max_duration, 2),
        "maxRatioDurationMinutes": round(max_duration / 60.0, 2),
        "maxRatioDurationLabel": _duration_label(max_duration),
        "avgRatio": round(avg_ratio, 4),
        "p50Ratio": round(p50, 4) if p50 is not None else None,
        "p75Ratio": round(p75, 4) if p75 is not None else None,
        "p90Ratio": round(p90, 4) if p90 is not None else None,
        "stddevRatio": round(stddev, 5),
        "coveredSeconds": round(duration, 2),
        "coveredMinutes": round(duration / 60.0, 2),
        "coveredHours": round(duration / 3600.0, 2),
        "coveragePct": round(coverage_ratio * 100.0, 2),
        "conservativeMaxListingRatio": conservative_ratio,
        "recommendedMaxListingRatio": recommended_ratio,
        "aggressiveMaxListingRatio": aggressive_ratio,
        "recommendationScore": round(recommendation_score, 6),
        "buckets": bucket_rows,
        "dominantBuckets": dominant_buckets,
        "ratioThresholds": threshold_rows,
        "timelineSegments": _timeline_rows(
            segments,
            start=start,
            range_seconds=range_seconds,
            bucket_size=bucket_size,
        ),
    }


def build_case_ratio_report(
    db: Database,
    *,
    start_utc: str,
    end_utc: str,
    market_hash_name: str | None = None,
    recommendation_crate_type: str | None = "all",
    bucket_size: float = DEFAULT_RATIO_BUCKET_SIZE,
    expected_interval_minutes: float = DEFAULT_COLLECTION_INTERVAL_MINUTES,
    max_gap_minutes: float | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    start = _parse_iso_datetime(start_utc)
    end = _parse_iso_datetime(end_utc)
    if start is None or end is None:
        raise ValueError("start_utc/end_utc must be ISO datetimes")
    if end <= start:
        raise ValueError("end_utc must be after start_utc")

    bucket = max(0.0001, float(bucket_size))
    expected_seconds = max(1.0, float(expected_interval_minutes) * 60.0)
    max_gap_seconds = max(expected_seconds, float(max_gap_minutes or expected_interval_minutes * 2.5) * 60.0)
    range_seconds = (end - start).total_seconds()

    rows = db.list_guadao_case_ratio_snapshots(
        start_utc=start.isoformat(),
        end_utc=end.isoformat(),
        market_hash_name=market_hash_name,
    )
    type_lookup = _case_type_lookup(db)
    grouped: dict[str, list[dict[str, Any]]] = {}
    status_counts: dict[str, int] = {}
    crate_type_snapshot_counts: dict[str, int] = {}
    legacy_corrected_count = 0
    for row in rows:
        payload = _correct_legacy_minor_unit_row(dict(row))
        status = str(payload.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "ok" or payload.get("listing_ratio") is None:
            continue
        market_name = str(payload["market_hash_name"])
        raw_json = _raw_dict(payload.get("raw_json"))
        raw_target = raw_json.get("target") if isinstance(raw_json.get("target"), dict) else {}
        type_info = type_lookup.get(market_name) or {}
        crate_type = str(
            raw_target.get("crateType")
            or type_info.get("crateType")
            or classify_case_monitor_target(raw_json, market_hash_name=market_name, name_cn=str(payload.get("name_cn") or ""))
        )
        crate_type_label = str(
            raw_target.get("crateTypeLabel")
            or type_info.get("crateTypeLabel")
            or CRATE_TYPE_LABELS.get(crate_type, crate_type)
        )
        payload["crateType"] = crate_type
        payload["crateTypeLabel"] = crate_type_label
        crate_type_snapshot_counts[crate_type] = crate_type_snapshot_counts.get(crate_type, 0) + 1
        if payload.get("legacySteamMinorUnitCorrected"):
            legacy_corrected_count += 1
        grouped.setdefault(market_name, []).append(payload)

    item_rows: list[dict[str, Any]] = []
    for name, item_rows_raw in grouped.items():
        type_info = type_lookup.get(name) or {}
        crate_type = str(item_rows_raw[-1].get("crateType") or type_info.get("crateType") or CRATE_TYPE_OTHER)
        crate_type_label = str(item_rows_raw[-1].get("crateTypeLabel") or type_info.get("crateTypeLabel") or CRATE_TYPE_LABELS.get(crate_type, crate_type))
        segments = _segments_for_rows(
            item_rows_raw,
            start=start,
            end=end,
            expected_interval_seconds=expected_seconds,
            max_gap_seconds=max_gap_seconds,
        )
        summary = _summarize_item_segments(
            market_hash_name=name,
            rows=item_rows_raw,
            segments=segments,
            range_seconds=range_seconds,
            bucket_size=bucket,
            start=start,
            crate_type=crate_type,
            crate_type_label=crate_type_label,
        )
        if summary is not None:
            item_rows.append(summary)

    item_rows.sort(
        key=lambda row: (
            -float(row["recommendationScore"]),
            float(row["recommendedMaxListingRatio"]),
            -float(row["coveragePct"]),
            str(row["marketHashName"]),
        )
    )
    crate_type_counts: dict[str, int] = {}
    for row in item_rows:
        crate_type = str(row.get("crateType") or CRATE_TYPE_OTHER)
        crate_type_counts[crate_type] = crate_type_counts.get(crate_type, 0) + 1
    rec_type = str(recommendation_crate_type or "all")
    recommendation_source = (
        [row for row in item_rows if row.get("crateType") == rec_type]
        if rec_type and rec_type != "all"
        else item_rows
    )
    recommendations = recommendation_source[: max(0, int(top_n))]
    return {
        "generatedAt": utc_now_iso(),
        "startUtc": start.isoformat(),
        "endUtc": end.isoformat(),
        "rangeHours": round(range_seconds / 3600.0, 2),
        "bucketSize": bucket,
        "expectedIntervalMinutes": expected_interval_minutes,
        "maxGapMinutes": round(max_gap_seconds / 60.0, 2),
        "snapshotCount": len(rows),
        "statusCounts": status_counts,
        "crateTypeCounts": crate_type_counts,
        "crateTypeSnapshotCounts": crate_type_snapshot_counts,
        "crateTypeLabels": CRATE_TYPE_LABELS,
        "recommendationCrateType": rec_type,
        "legacySteamMinorUnitCorrectedCount": legacy_corrected_count,
        "itemCount": len(item_rows),
        "items": item_rows,
        "recommendations": recommendations,
    }


def write_case_ratio_report_files(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / "guadao_case_ratio_report.json",
        "summaryCsv": output_dir / "guadao_case_ratio_summary.csv",
        "bucketCsv": output_dir / "guadao_case_ratio_buckets.csv",
        "markdown": output_dir / "guadao_case_ratio_report.md",
    }
    paths["json"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_fields = [
        "marketHashName",
        "name",
        "crateType",
        "crateTypeLabel",
        "sampleCount",
        "coveragePct",
        "latestRatio",
        "minRatio",
        "minRatioDurationMinutes",
        "maxRatio",
        "maxRatioDurationMinutes",
        "avgRatio",
        "p50Ratio",
        "p75Ratio",
        "p90Ratio",
        "stddevRatio",
        "conservativeMaxListingRatio",
        "recommendedMaxListingRatio",
        "aggressiveMaxListingRatio",
        "effectiveRecommendedMaxListingRatio",
        "selectedReferenceRatio",
        "steamReferenceSource",
        "steamReferenceSourceLabel",
        "steamReferencePrice",
        "steamVolume24h",
        "steamVolume7d",
        "steamAvgDailyVolume7d",
        "liquidityLevel",
        "liquidityLabel",
        "recommendationScore",
        "legacySteamMinorUnitCorrectedCount",
        "latestC5SellPrice",
        "latestSteamListPrice",
        "latestSteamAfterTaxPrice",
        "sellerFloorPrice",
        "sellerWallListPrice",
        "buyerMaxPrice",
    ]
    with paths["summaryCsv"].open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=summary_fields)
        writer.writeheader()
        for item in report.get("items", []):
            writer.writerow({field: item.get(field) for field in summary_fields})

    bucket_fields = [
        "marketHashName",
        "crateType",
        "bucket",
        "lower",
        "upper",
        "durationMinutes",
        "coveragePct",
    ]
    with paths["bucketCsv"].open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=bucket_fields)
        writer.writeheader()
        for item in report.get("items", []):
            for bucket in item.get("buckets", []):
                writer.writerow(
                    {
                        "marketHashName": item.get("marketHashName"),
                        "crateType": item.get("crateType"),
                        "bucket": bucket.get("bucket"),
                        "lower": bucket.get("lower"),
                        "upper": bucket.get("upper"),
                        "durationMinutes": bucket.get("durationMinutes"),
                        "coveragePct": bucket.get("coveragePct"),
                    }
                )

    paths["markdown"].write_text(_render_report_markdown(report), encoding="utf-8")
    return {key: str(path) for key, path in paths.items()}


def _render_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# CS2 箱子挂刀比监控报告",
        "",
        f"- 范围: {report.get('startUtc')} ~ {report.get('endUtc')}",
        f"- 快照数: {report.get('snapshotCount')} | 有效箱子: {report.get('itemCount')}",
        f"- 推荐类别: {report.get('recommendationCrateType')} | 旧价格单位修正快照: {report.get('legacySteamMinorUnitCorrectedCount')}",
        f"- 口径: C5最低在售价 / Steam预计税后到手；Steam取官方 orderbook 卖家累计墙。",
        "",
        "## 推荐",
        "",
        "|排名|类别|箱子|建议比例|采用源|24h成交|速度|稳健墙|最低|最高|覆盖率|",
        "|---:|---|---|---:|---|---:|---|---:|---:|---:|---:|",
    ]
    for index, item in enumerate(report.get("recommendations", []), start=1):
        lines.append(
            "|{rank}|{kind}|{name}|{effective:.4f}|{source}|{volume}|{speed}|{rec:.4f}|{minv:.4f}|{maxv:.4f}|{coverage:.2f}%|".format(
                rank=index,
                kind=item.get("crateTypeLabel") or item.get("crateType") or "-",
                name=item.get("marketHashName"),
                effective=float(item.get("effectiveRecommendedMaxListingRatio") or item.get("recommendedMaxListingRatio") or 0),
                source=item.get("steamReferenceSourceLabel") or "-",
                volume=int(item.get("steamVolume24h") or 0),
                speed=item.get("liquidityLabel") or "-",
                rec=float(item.get("recommendedMaxListingRatio") or 0),
                minv=float(item.get("minRatio") or 0),
                maxv=float(item.get("maxRatio") or 0),
                coverage=float(item.get("coveragePct") or 0),
            )
        )
    lines.extend(["", "## 明细", ""])
    for item in report.get("items", []):
        lines.append(f"### {item.get('marketHashName')} ({item.get('crateTypeLabel') or item.get('crateType')})")
        lines.append(
            "- 当前 {latest:.4f} | 平均 {avg:.4f} | 最低 {minv:.4f} 持续 {mind} | 最高 {maxv:.4f} 持续 {maxd} | 建议 {effective:.4f} | 源 {source} | 24h成交 {volume} | 速度 {speed}".format(
                latest=float(item.get("latestRatio") or 0),
                avg=float(item.get("avgRatio") or 0),
                minv=float(item.get("minRatio") or 0),
                mind=item.get("minRatioDurationLabel") or "-",
                maxv=float(item.get("maxRatio") or 0),
                maxd=item.get("maxRatioDurationLabel") or "-",
                effective=float(item.get("effectiveRecommendedMaxListingRatio") or item.get("recommendedMaxListingRatio") or 0),
                source=item.get("steamReferenceSourceLabel") or "-",
                volume=int(item.get("steamVolume24h") or 0),
                speed=item.get("liquidityLabel") or "-",
            )
        )
        if item.get("ratioThresholds"):
            threshold_text = "；".join(
                f"{threshold.get('label')} {threshold.get('ratio'):.4f} 覆盖 {threshold.get('coveragePct'):.2f}%/{threshold.get('durationLabel')}"
                for threshold in item.get("ratioThresholds", [])
            )
            lines.append(f"- 阈值覆盖: {threshold_text}")
        if item.get("buckets"):
            bucket_text = "；".join(
                f"{bucket.get('bucket')} {bucket.get('durationLabel')}"
                for bucket in item.get("buckets", [])[:8]
            )
            lines.append(f"- 比例区间: {bucket_text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def default_case_report_window(hours: float = DEFAULT_REPORT_HOURS) -> tuple[str, str]:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=float(hours))
    return start.isoformat(), end.isoformat()


def ensure_case_report_frontend_payload(report: dict[str, Any], path: Path) -> Path:
    ensure_parent_dir(path)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
