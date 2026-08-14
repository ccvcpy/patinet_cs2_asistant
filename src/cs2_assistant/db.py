from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from cs2_assistant.models import CatalogItem, MarketState, POOL_STATUS_HOLDING
from cs2_assistant.utils import ensure_parent_dir, utc_now_iso


PROFIT_TRADE_HISTORY_TREND_MAX_POINTS = 240


_CATALOG_SEARCH_SEPARATOR_RE = re.compile(r"[\s/|·,，;；]+")
_CATALOG_SEARCH_TRIM_RE = re.compile(r"^[★☆（）()\[\]{}<>《》【】]+|[★☆（）()\[\]{}<>《》【】]+$")
_CATALOG_NORMAL_MARKERS = ("普通版", "普通款", "非暗金", "non-stattrak", "non stattrak", "（★）")
_CATALOG_STATTRAK_MARKERS = ("stattrak™", "stattrak", "暗金版", "暗金")


def _catalog_search_parts(keyword: str) -> tuple[list[str], str | None]:
    """Return searchable tokens plus an optional edition constraint.

    The catalog stores normal knives as ``（★）`` rather than with a literal
    ``普通版`` label.  Search input is therefore interpreted, not compared as
    one opaque substring.  This also makes space-separated Chinese/English
    terms behave like a user expects.
    """

    normalized = str(keyword or "").strip().casefold().replace("伽马多普勒", "伽玛多普勒")
    edition: str | None = None
    for marker in _CATALOG_NORMAL_MARKERS:
        if marker in normalized:
            normalized = normalized.replace(marker, " ")
            edition = "normal"
    for marker in _CATALOG_STATTRAK_MARKERS:
        if marker in normalized:
            normalized = normalized.replace(marker, " ")
            edition = "stattrak"
    tokens: list[str] = []
    for raw in _CATALOG_SEARCH_SEPARATOR_RE.split(normalized):
        token = _CATALOG_SEARCH_TRIM_RE.sub("", raw.strip())
        if token and token not in {"-", "_"} and token not in tokens:
            tokens.append(token)
    return tokens, edition


def _catalog_is_stattrak(row: sqlite3.Row) -> bool:
    return "stattrak" in str(row["market_hash_name"] or "").casefold()


def _catalog_canonical_name(value: str) -> str:
    text = str(value or "").casefold().replace("伽马多普勒", "伽玛多普勒")
    text = re.sub(r"（★\s*stattrak™?）|（★）", "", text)
    text = re.sub(r"^★\s*stattrak™?\s*|^★\s*", "", text)
    return " ".join(text.split())


def _catalog_search_sort_key(row: sqlite3.Row, tokens: list[str]) -> tuple[Any, ...]:
    name = str(row["name_cn"] or row["market_hash_name"] or "")
    market_hash_name = str(row["market_hash_name"] or "")
    canonical_name = _catalog_canonical_name(name)
    canonical_market = _catalog_canonical_name(market_hash_name)
    query = " ".join(tokens)
    candidates = (name.casefold(), market_hash_name.casefold(), canonical_name, canonical_market)
    if query and any(value == query for value in candidates):
        relevance = 0
    elif query and any(value.startswith(query) for value in candidates):
        relevance = 1
    else:
        relevance = 2
    positions = tuple(
        min((value.find(token) for value in candidates if token in value), default=10**9)
        for token in tokens
    )
    # Sort editions beside each other by their name without the StatTrak marker.
    # This prevents 100+ StatTrak rows from occupying the whole first page.
    return (
        relevance,
        positions,
        canonical_name,
        1 if _catalog_is_stattrak(row) else 0,
        name.casefold(),
        market_hash_name.casefold(),
    )


PROFIT_TRADE_OBSERVABILITY_TABLES = frozenset(
    {
        "profit_trade_roi_watch",
        "profit_trade_roi_observations",
        "profit_trade_state_events",
        "profit_trade_acknowledgements",
        "profit_trade_runtime_state",
        "profit_trade_long_buy_orders",
        "profit_trade_long_buy_fills",
        "profit_trade_long_buy_events",
    }
)

RUNTIME_COORDINATION_TABLES = frozenset(
    {
        "executor_runtime_state",
        "scheduled_tasks",
        "steam_cookie_health",
        "steam_request_queue",
        "steam_route_circuits",
        "c5_api_circuits",
        "guadao_issue_acknowledgements",
        "guadao_operation_audit_events",
        "strategy_config_audit",
        "case_monitor_runtime_state",
        "case_monitor_jobs",
    }
)


def _utc_iso(value: str | datetime | None = None) -> str:
    """Return a timezone-aware UTC timestamp suitable for SQLite ordering."""

    if value is None:
        return utc_now_iso()
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("timestamp is required")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _lease_expiry(now: str, lease_seconds: float) -> str:
    seconds = float(lease_seconds)
    if seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    parsed = datetime.fromisoformat(now)
    return (parsed + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def _json_object(value: dict[str, Any] | None) -> str:
    return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False, separators=(",", ":"))


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _profit_trade_history_trend(
    rows: Iterable[sqlite3.Row],
    *,
    max_points: int = PROFIT_TRADE_HISTORY_TREND_MAX_POINTS,
) -> dict[str, Any]:
    """Build a bounded, chronological ROI trend from real observation rows.

    When history is larger than the wire budget, equal time buckets retain
    their local minimum and maximum ROI rows.  The first and last observation
    are always retained, so the chart keeps its actual time boundary without
    hiding short-lived ROI spikes.
    """

    normalized: list[dict[str, Any]] = []
    for row in rows:
        observed_at = str(row["observed_at"] or "").strip()
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
            timestamp = parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        try:
            expected_roi = float(row["expected_roi"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(expected_roi):
            continue

        def optional_number(column: str) -> float | None:
            try:
                value = float(row[column])
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        normalized.append(
            {
                "observedAt": parsed.isoformat(),
                "expectedRoi": expected_roi,
                "buyOrderReferenceRoi": optional_number("buy_order_reference_roi"),
                "roiBasis": optional_number("balance_discount"),
                "_timestamp": timestamp,
            }
        )

    total = len(normalized)
    point_limit = max(2, int(max_points))
    sampled = total > point_limit
    if not sampled:
        selected = normalized
    else:
        bucket_count = max(1, (point_limit - 2) // 2)
        first_timestamp = normalized[0]["_timestamp"]
        last_timestamp = normalized[-1]["_timestamp"]
        duration = last_timestamp - first_timestamp
        buckets: list[list[int]] = [[] for _ in range(bucket_count)]
        interior_count = max(1, total - 2)
        for index in range(1, total - 1):
            if duration > 0:
                bucket_index = int(
                    ((normalized[index]["_timestamp"] - first_timestamp) / duration)
                    * bucket_count
                )
            else:
                bucket_index = int(((index - 1) / interior_count) * bucket_count)
            buckets[min(bucket_count - 1, max(0, bucket_index))].append(index)

        selected_indexes = {0, total - 1}
        for bucket in buckets:
            if not bucket:
                continue
            selected_indexes.add(min(bucket, key=lambda index: normalized[index]["expectedRoi"]))
            selected_indexes.add(max(bucket, key=lambda index: normalized[index]["expectedRoi"]))
        selected = [normalized[index] for index in sorted(selected_indexes)]

    return {
        "totalValidPoints": total,
        "sampled": sampled,
        "points": [
            {key: value for key, value in point.items() if not key.startswith("_")}
            for point in selected
        ],
    }


def _emit_profit_trade_local_event(
    *,
    component: str,
    operation: str,
    message: str,
    **fields: Any,
) -> None:
    """Best-effort Profit Trade event emission after a committed DB change."""

    try:
        from cs2_assistant.services.profit_trade_logging import get_profit_trade_event_logger

        get_profit_trade_event_logger().emit(
            provider="local",
            component=component,
            operation=operation,
            message=message,
            **fields,
        )
    except Exception:
        # Observability must never make a committed trading transition fail.
        return


class Database:
    def __init__(self, path: Path):
        self.path = path
        ensure_parent_dir(path)
        self.conn = sqlite3.connect(path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.conn.close()

    def _backup_before_profit_trade_observability_upgrade(self) -> Path | None:
        """Back up an existing legacy DB once before adding observability tables."""

        if str(self.path) == ":memory:" or not self.path.exists():
            return None
        existing_tables = {
            str(row[0])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "profit_trades" not in existing_tables:
            return None
        if PROFIT_TRADE_OBSERVABILITY_TABLES.issubset(existing_tables):
            return None

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = self.path.suffix or ".db"
        base_name = f"{self.path.stem}.pre-profit-trade-observability-{timestamp}"
        backup_path = self.path.with_name(f"{base_name}{suffix}")
        counter = 1
        while backup_path.exists():
            backup_path = self.path.with_name(f"{base_name}-{counter}{suffix}")
            counter += 1
        backup_conn = sqlite3.connect(backup_path)
        try:
            self.conn.backup(backup_conn)
        finally:
            backup_conn.close()
        return backup_path

    def _backup_before_runtime_coordination_upgrade(self) -> Path | None:
        """Back up an existing operational DB once before adding runtime tables."""

        if str(self.path) == ":memory:" or not self.path.exists():
            return None
        existing_tables = {
            str(row[0])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"inventory_pool", "pool_operations"}.intersection(existing_tables):
            return None
        if RUNTIME_COORDINATION_TABLES.issubset(existing_tables):
            return None

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = self.path.suffix or ".db"
        base_name = f"{self.path.stem}.pre-runtime-coordination-{timestamp}"
        backup_path = self.path.with_name(f"{base_name}{suffix}")
        counter = 1
        while backup_path.exists():
            backup_path = self.path.with_name(f"{base_name}-{counter}{suffix}")
            counter += 1
        backup_conn = sqlite3.connect(backup_path)
        try:
            self.conn.backup(backup_conn)
            for table in sorted(RUNTIME_COORDINATION_TABLES):
                backup_conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            backup_conn.commit()
        finally:
            backup_conn.close()
        return backup_path

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            str(row[1])
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns.items():
            if name in existing:
                continue
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def _initialize_profit_trade_selection_watch_schema(self) -> None:
        """Create the isolated, research-only Profit Trade selection-watch store.

        This deliberately does *not* reuse ``profit_trade_roi_watch``.  The
        inventory watch is driven by real tradable inventory and retires a row
        whenever ROI stops being positive.  A user-selected market may have no
        inventory, no current price, or negative ROI, all of which must remain
        observable without ever becoming a trade candidate.
        """

        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profit_trade_selection_watch (
                market_hash_name TEXT PRIMARY KEY,
                name_cn TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending_first_scan',
                selected_at TEXT NOT NULL,
                first_seen_at TEXT,
                last_observed_at TEXT,
                next_scan_at TEXT,
                last_error TEXT,
                steam_buy_price REAL,
                steam_seller_floor_count REAL,
                steam_buyer_max_price REAL,
                steam_buyer_max_count REAL,
                steam_spread_amount REAL,
                steam_spread_pct REAL,
                steam_orderbook_crossed INTEGER,
                steam_currency_id INTEGER,
                steam_orderbook_observed_at TEXT,
                steam_price_source TEXT,
                c5_listing_price REAL,
                c5_price_source TEXT,
                c5_expected_net_price REAL,
                balance_discount REAL,
                expected_profit REAL,
                expected_roi REAL,
                buy_order_reference_roi REAL,
                buy_order_reference_profit REAL,
                buy_order_reference_status TEXT,
                inventory_count INTEGER NOT NULL DEFAULT 0,
                tradable_count INTEGER NOT NULL DEFAULT 0,
                risk_status TEXT,
                risk_reason TEXT,
                execution_status TEXT NOT NULL DEFAULT 'selection_only',
                execution_reason TEXT,
                removed_at TEXT,
                updated_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_selection_watch_due
            ON profit_trade_selection_watch(active, next_scan_at, selected_at);

            CREATE INDEX IF NOT EXISTS idx_profit_trade_selection_watch_roi
            ON profit_trade_selection_watch(active, expected_roi DESC, last_observed_at DESC);

            CREATE TABLE IF NOT EXISTS profit_trade_selection_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                market_hash_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                error TEXT,
                steam_buy_price REAL,
                steam_seller_floor_count REAL,
                steam_buyer_max_price REAL,
                steam_buyer_max_count REAL,
                steam_spread_amount REAL,
                steam_spread_pct REAL,
                steam_orderbook_crossed INTEGER,
                steam_currency_id INTEGER,
                steam_orderbook_observed_at TEXT,
                steam_price_source TEXT,
                c5_listing_price REAL,
                c5_price_source TEXT,
                c5_expected_net_price REAL,
                balance_discount REAL,
                expected_profit REAL,
                expected_roi REAL,
                buy_order_reference_roi REAL,
                buy_order_reference_profit REAL,
                buy_order_reference_status TEXT,
                inventory_count INTEGER NOT NULL DEFAULT 0,
                tradable_count INTEGER NOT NULL DEFAULT 0,
                risk_status TEXT,
                risk_reason TEXT,
                execution_status TEXT,
                execution_reason TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_selection_observations_name_time
            ON profit_trade_selection_observations(market_hash_name, observed_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_profit_trade_selection_observations_scan
            ON profit_trade_selection_observations(scan_id, id);
            """
        )

    def initialize(self) -> None:
        observability_backup = self._backup_before_profit_trade_observability_upgrade()
        if observability_backup is None:
            self._backup_before_runtime_coordination_upgrade()
        self.conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS items (
                market_hash_name TEXT PRIMARY KEY,
                name_cn TEXT NOT NULL,
                c5_item_id TEXT,
                steam_item_id TEXT,
                raw_json TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watch_items (
                market_hash_name TEXT PRIMARY KEY,
                display_name TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (market_hash_name) REFERENCES items(market_hash_name)
            );

            CREATE TABLE IF NOT EXISTS baskets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                pricing_metric TEXT NOT NULL DEFAULT 'c5_price',
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS basket_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                basket_id INTEGER NOT NULL,
                market_hash_name TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (basket_id, market_hash_name),
                FOREIGN KEY (basket_id) REFERENCES baskets(id) ON DELETE CASCADE,
                FOREIGN KEY (market_hash_name) REFERENCES items(market_hash_name)
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_hash_name TEXT NOT NULL,
                status TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0,
                manual_cost REAL,
                target_buy_price REAL,
                target_sell_price REAL,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (market_hash_name) REFERENCES items(market_hash_name)
            );

            CREATE TABLE IF NOT EXISTS alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,
                target_key TEXT NOT NULL,
                metric TEXT NOT NULL,
                operator TEXT NOT NULL,
                threshold REAL NOT NULL,
                anchor_value REAL,
                cooldown_minutes INTEGER NOT NULL DEFAULT 60,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                last_triggered_at TEXT,
                last_triggered_value REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_alert_rules_target
            ON alert_rules(target_type, target_key, enabled);

            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_hash_name TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                c5_sell_price REAL,
                c5_sell_count INTEGER,
                c5_bid_price REAL,
                c5_bid_count INTEGER,
                steam_sell_price REAL,
                steam_sell_count INTEGER,
                steam_bid_price REAL,
                steam_bid_count INTEGER,
                ratio REAL,
                raw_json TEXT NOT NULL,
                FOREIGN KEY (market_hash_name) REFERENCES items(market_hash_name)
            );

            CREATE TABLE IF NOT EXISTS basket_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                basket_name TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                basket_total REAL NOT NULL,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,
                target_key TEXT NOT NULL,
                metric TEXT NOT NULL,
                observed_value REAL NOT NULL,
                threshold REAL NOT NULL,
                message TEXT NOT NULL,
                notified_at TEXT NOT NULL,
                FOREIGN KEY (rule_id) REFERENCES alert_rules(id)
            );

            -- ===== Strategy / inventory-pool tables =====

            CREATE TABLE IF NOT EXISTS inventory_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_hash_name TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'holding',
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_inventory_pool_mhn
            ON inventory_pool(market_hash_name);

            CREATE INDEX IF NOT EXISTS idx_inventory_pool_status
            ON inventory_pool(status);

            CREATE TABLE IF NOT EXISTS inventory_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id TEXT NOT NULL UNIQUE,
                market_hash_name TEXT NOT NULL,
                steam_id TEXT,
                tradable INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'available',
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_inventory_assets_mhn
            ON inventory_assets(market_hash_name);

            CREATE INDEX IF NOT EXISTS idx_inventory_assets_status
            ON inventory_assets(status);

            CREATE INDEX IF NOT EXISTS idx_inventory_assets_steam
            ON inventory_assets(steam_id);

            CREATE TABLE IF NOT EXISTS asset_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id TEXT NOT NULL,
                market_hash_name TEXT NOT NULL,
                owner TEXT NOT NULL,
                purpose TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                operation_id INTEGER,
                reserved_until TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                released_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_asset_reservations_asset
            ON asset_reservations(asset_id, status);

            CREATE INDEX IF NOT EXISTS idx_asset_reservations_owner
            ON asset_reservations(owner, status);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_asset_reservations_live_asset
            ON asset_reservations(asset_id)
            WHERE status IN ('active', 'consumed');

            CREATE TABLE IF NOT EXISTS profit_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_no TEXT NOT NULL UNIQUE,
                market_hash_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate',
                step_key TEXT NOT NULL DEFAULT 'discovered',
                step_index INTEGER NOT NULL DEFAULT 0,
                a_asset_id TEXT,
                a_steam_id TEXT,
                b_asset_id TEXT,
                steam_listing_id TEXT,
                c5_product_id TEXT,
                steam_buy_price REAL,
                steam_balance_discount REAL,
                steam_real_cost REAL,
                c5_listing_price REAL,
                c5_expected_net_price REAL,
                c5_sold_net_price REAL,
                expected_profit REAL,
                realized_profit REAL,
                expected_roi REAL,
                realized_roi REAL,
                error TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trades_status
            ON profit_trades(status, updated_at);

            CREATE INDEX IF NOT EXISTS idx_profit_trades_asset
            ON profit_trades(a_asset_id, status);

            CREATE TABLE IF NOT EXISTS profit_trade_roi_watch (
                market_hash_name TEXT PRIMARY KEY,
                name_cn TEXT,
                scan_id TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                steam_buy_price REAL,
                steam_seller_floor_count REAL,
                steam_buyer_max_price REAL,
                steam_buyer_max_count REAL,
                steam_spread_amount REAL,
                steam_spread_pct REAL,
                steam_orderbook_crossed INTEGER,
                steam_currency_id INTEGER,
                steam_orderbook_observed_at TEXT,
                steam_price_source TEXT,
                c5_listing_price REAL,
                c5_price_source TEXT,
                c5_expected_net_price REAL,
                balance_discount REAL,
                expected_profit REAL,
                expected_roi REAL,
                buy_order_reference_roi REAL,
                buy_order_reference_profit REAL,
                buy_order_reference_status TEXT,
                min_roi REAL,
                manual_review_roi REAL,
                inventory_count INTEGER,
                tradable_count INTEGER,
                c5_recent_sold_net_price REAL,
                c5_recent_sold_count INTEGER,
                c5_current_sell_price REAL,
                c5_on_sale_count INTEGER,
                c5_purchase_max_price REAL,
                c5_purchase_count INTEGER,
                risk_status TEXT,
                risk_reason TEXT,
                execution_status TEXT NOT NULL,
                execution_reason TEXT,
                first_seen_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                exited_at TEXT,
                exit_reason TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_roi_watch_active_roi
            ON profit_trade_roi_watch(active, expected_roi DESC, last_observed_at DESC);

            CREATE TABLE IF NOT EXISTS profit_trade_roi_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                market_hash_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                steam_buy_price REAL,
                steam_seller_floor_count REAL,
                steam_buyer_max_price REAL,
                steam_buyer_max_count REAL,
                steam_spread_amount REAL,
                steam_spread_pct REAL,
                steam_orderbook_crossed INTEGER,
                steam_currency_id INTEGER,
                steam_orderbook_observed_at TEXT,
                c5_listing_price REAL,
                c5_expected_net_price REAL,
                balance_discount REAL,
                expected_profit REAL,
                expected_roi REAL,
                buy_order_reference_roi REAL,
                buy_order_reference_profit REAL,
                buy_order_reference_status TEXT,
                min_roi REAL,
                manual_review_roi REAL,
                inventory_count INTEGER,
                tradable_count INTEGER,
                risk_status TEXT,
                risk_reason TEXT,
                execution_status TEXT,
                execution_reason TEXT,
                exit_reason TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_roi_observations_name_time
            ON profit_trade_roi_observations(market_hash_name, observed_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_profit_trade_roi_observations_scan
            ON profit_trade_roi_observations(scan_id, id);

            CREATE TABLE IF NOT EXISTS profit_trade_state_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                status_from TEXT,
                status_to TEXT NOT NULL,
                step_key_from TEXT,
                step_key_to TEXT NOT NULL,
                step_index_from INTEGER,
                step_index_to INTEGER NOT NULL,
                reason TEXT,
                log_event_id TEXT,
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (trade_id) REFERENCES profit_trades(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_state_events_trade_time
            ON profit_trade_state_events(trade_id, created_at, id);

            CREATE TABLE IF NOT EXISTS profit_trade_acknowledgements (
                trade_id INTEGER PRIMARY KEY,
                acknowledged INTEGER NOT NULL DEFAULT 1,
                reason TEXT,
                acknowledged_at TEXT,
                restored_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (trade_id) REFERENCES profit_trades(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_acknowledgements_state
            ON profit_trade_acknowledgements(acknowledged, updated_at DESC);

            CREATE TABLE IF NOT EXISTS profit_trade_runtime_state (
                state_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profit_trade_long_buy_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_hash_name TEXT NOT NULL,
                steam_account_id TEXT NOT NULL,
                steam_id TEXT,
                buy_order_id TEXT,
                create_request_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'creating',
                bid_price_cents INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                filled_quantity INTEGER NOT NULL DEFAULT 0,
                remaining_quantity INTEGER NOT NULL,
                c5_price_batch REAL,
                c5_expected_net_price REAL,
                balance_discount REAL,
                standard_roi REAL,
                aggressive_roi REAL,
                standard_safe_price_cents INTEGER,
                aggressive_safe_price_cents INTEGER,
                competitor_buy_price_cents INTEGER,
                competitor_buy_status TEXT,
                worst_case_roi REAL,
                source_scan_id TEXT,
                previous_bid_price_cents INTEGER,
                previous_price_expires_at TEXT,
                wallet_before REAL,
                replaces_order_id INTEGER,
                replaced_by_order_id INTEGER,
                terminal_reason TEXT,
                note_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_filled_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (replaces_order_id)
                    REFERENCES profit_trade_long_buy_orders(id),
                FOREIGN KEY (replaced_by_order_id)
                    REFERENCES profit_trade_long_buy_orders(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_profit_trade_long_buy_remote_order
            ON profit_trade_long_buy_orders(steam_account_id, buy_order_id)
            WHERE buy_order_id IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_profit_trade_long_buy_live_item
            ON profit_trade_long_buy_orders(market_hash_name)
            WHERE state IN (
                'creating', 'active', 'partial', 'cancel_pending',
                'terminal_uncertain'
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_long_buy_account_state
            ON profit_trade_long_buy_orders(
                steam_account_id, state, last_checked_at, id
            );

            CREATE TABLE IF NOT EXISTS profit_trade_long_buy_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                long_buy_order_id INTEGER NOT NULL,
                steam_account_id TEXT NOT NULL,
                purchase_id TEXT NOT NULL,
                listing_id TEXT,
                market_hash_name TEXT NOT NULL,
                paid_total_cents INTEGER NOT NULL,
                asset_id TEXT,
                new_asset_id TEXT,
                purchased_at TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                profit_trade_id INTEGER,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                processed_at TEXT,
                UNIQUE (steam_account_id, purchase_id),
                FOREIGN KEY (long_buy_order_id)
                    REFERENCES profit_trade_long_buy_orders(id),
                FOREIGN KEY (profit_trade_id)
                    REFERENCES profit_trades(id)
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_long_buy_fills_pending
            ON profit_trade_long_buy_fills(state, created_at, id);

            CREATE TABLE IF NOT EXISTS profit_trade_long_buy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                long_buy_order_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                state_from TEXT,
                state_to TEXT,
                reason TEXT,
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (long_buy_order_id)
                    REFERENCES profit_trade_long_buy_orders(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_profit_trade_long_buy_events_order_time
            ON profit_trade_long_buy_events(
                long_buy_order_id, created_at, id
            );

            CREATE TABLE IF NOT EXISTS strategy_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_hash_name TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                rebuy_price REAL,
                steam_sell_price REAL,
                steam_after_tax_price REAL,
                listing_ratio REAL,
                transfer_real_ratio REAL,
                recommended_strategy TEXT,
                inventory_count INTEGER,
                tradable_count INTEGER,
                config_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_strategy_eval_mhn
            ON strategy_evaluations(market_hash_name, evaluated_at);

            CREATE TABLE IF NOT EXISTS guadao_case_ratio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_hash_name TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                name_cn TEXT,
                c5_sell_price REAL,
                c5_sell_count INTEGER,
                steam_list_price REAL,
                steam_wall_price REAL,
                steam_after_tax_price REAL,
                listing_ratio REAL,
                c5_price_source TEXT,
                steam_price_source TEXT,
                status TEXT NOT NULL DEFAULT 'ok',
                error TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_guadao_case_ratio_mhn_time
            ON guadao_case_ratio_snapshots(market_hash_name, observed_at);

            CREATE INDEX IF NOT EXISTS idx_guadao_case_ratio_time
            ON guadao_case_ratio_snapshots(observed_at);

            CREATE TABLE IF NOT EXISTS case_monitor_runtime_state (
                runtime_key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                interval_minutes REAL NOT NULL DEFAULT 5,
                runtime_status TEXT NOT NULL DEFAULT 'paused',
                current_job_id TEXT,
                next_run_at TEXT,
                last_collection_at TEXT,
                last_report_at TEXT,
                last_error TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_case_monitor_runtime_status
            ON case_monitor_runtime_state(enabled, runtime_status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS case_monitor_jobs (
                job_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                trigger_source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                requested_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_case_monitor_jobs_status_time
            ON case_monitor_jobs(status, requested_at, job_id);

            CREATE INDEX IF NOT EXISTS idx_case_monitor_jobs_type_time
            ON case_monitor_jobs(job_type, requested_at DESC, job_id);

            CREATE TABLE IF NOT EXISTS pool_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_hash_name TEXT NOT NULL,
                strategy TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                quantity INTEGER NOT NULL DEFAULT 1,
                expected_price REAL,
                actual_price REAL,
                asset_id TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pool_ops_status
            ON pool_operations(status);

            -- ===== Persistent runtime coordination =====

            CREATE TABLE IF NOT EXISTS executor_runtime_state (
                executor_key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                runtime_status TEXT NOT NULL DEFAULT 'stopped',
                migration_hold INTEGER NOT NULL DEFAULT 1,
                gate_reason TEXT,
                heartbeat_at TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_executor_runtime_status
            ON executor_runtime_state(enabled, runtime_status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                task_type TEXT NOT NULL,
                account_id TEXT,
                operation_id TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 2,
                next_attempt_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
            ON scheduled_tasks(status, next_attempt_at, priority, id);

            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_source
            ON scheduled_tasks(source, task_type, status, next_attempt_at);

            CREATE TABLE IF NOT EXISTS steam_cookie_health (
                account_id TEXT PRIMARY KEY,
                account_name TEXT,
                steam_id TEXT,
                status TEXT NOT NULL DEFAULT 'unknown',
                batch_id TEXT,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_validated_at TEXT,
                next_retry_at TEXT,
                retry_after_seconds REAL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_steam_cookie_health_status
            ON steam_cookie_health(status, next_retry_at, updated_at);

            CREATE TABLE IF NOT EXISTS steam_request_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                route TEXT NOT NULL,
                method TEXT,
                account_id TEXT,
                operation_id TEXT,
                priority INTEGER NOT NULL DEFAULT 3,
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                available_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                http_status INTEGER,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_steam_request_queue_claim
            ON steam_request_queue(status, available_at, priority, id);

            CREATE INDEX IF NOT EXISTS idx_steam_request_queue_route_time
            ON steam_request_queue(route, account_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_steam_request_queue_http_status
            ON steam_request_queue(http_status, completed_at DESC);

            CREATE TABLE IF NOT EXISTS steam_route_circuits (
                circuit_key TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                account_id TEXT,
                route TEXT,
                state TEXT NOT NULL DEFAULT 'closed',
                consecutive_429 INTEGER NOT NULL DEFAULT 0,
                first_429_at TEXT,
                last_429_at TEXT,
                cooldown_until TEXT,
                next_probe_at TEXT,
                probe_lease_owner TEXT,
                probe_lease_expires_at TEXT,
                reason TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_steam_route_circuits_state
            ON steam_route_circuits(state, next_probe_at, updated_at);

            CREATE TABLE IF NOT EXISTS c5_api_circuits (
                circuit_key TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'closed',
                error_code INTEGER,
                request_ip TEXT,
                trigger_source TEXT,
                trigger_operation TEXT,
                first_error_at TEXT,
                last_error_at TEXT,
                next_probe_at TEXT,
                probe_lease_owner TEXT,
                probe_lease_expires_at TEXT,
                alert_sent_at TEXT,
                recovered_at TEXT,
                recovery_alert_sent_at TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_c5_api_circuits_state
            ON c5_api_circuits(state, next_probe_at, updated_at);

            CREATE TABLE IF NOT EXISTS guadao_issue_acknowledgements (
                issue_key TEXT PRIMARY KEY,
                acknowledged INTEGER NOT NULL DEFAULT 1,
                reason TEXT,
                actor TEXT,
                acknowledged_at TEXT,
                restored_at TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_guadao_issue_ack_state
            ON guadao_issue_acknowledgements(acknowledged, updated_at DESC);

            CREATE TABLE IF NOT EXISTS guadao_operation_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                sell_operation_id INTEGER NOT NULL,
                rebuy_operation_id INTEGER NOT NULL,
                batch_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'web_user',
                reason TEXT,
                old_value_json TEXT NOT NULL DEFAULT '{}',
                new_value_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(event_type, rebuy_operation_id, request_id)
            );

            CREATE INDEX IF NOT EXISTS idx_guadao_operation_audit_sell_time
            ON guadao_operation_audit_events(sell_operation_id, created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_guadao_operation_audit_batch
            ON guadao_operation_audit_events(batch_id, created_at, id);

            CREATE TABLE IF NOT EXISTS strategy_config_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_scope TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'update',
                old_value_json TEXT NOT NULL DEFAULT '{}',
                new_value_json TEXT NOT NULL DEFAULT '{}',
                diff_json TEXT NOT NULL DEFAULT '{}',
                actor TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_strategy_config_audit_scope_time
            ON strategy_config_audit(config_scope, created_at DESC, id DESC);
            """
        )
        roi_orderbook_columns = {
            "steam_seller_floor_count": "REAL",
            "steam_buyer_max_price": "REAL",
            "steam_buyer_max_count": "REAL",
            "steam_spread_amount": "REAL",
            "steam_spread_pct": "REAL",
            "steam_orderbook_crossed": "INTEGER",
            "steam_currency_id": "INTEGER",
            "steam_orderbook_observed_at": "TEXT",
            "buy_order_reference_roi": "REAL",
            "buy_order_reference_profit": "REAL",
            "buy_order_reference_status": "TEXT",
        }
        self._ensure_columns("profit_trade_roi_watch", roi_orderbook_columns)
        self._ensure_columns("profit_trade_roi_watch", {"scan_id": "TEXT"})
        self._ensure_columns("profit_trade_roi_observations", roi_orderbook_columns)
        self._initialize_profit_trade_selection_watch_schema()
        now = utc_now_iso()
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO executor_runtime_state (
                executor_key, enabled, runtime_status, migration_hold,
                payload_json, created_at, updated_at
            ) VALUES (?, 0, 'stopped', 1, '{}', ?, ?)
            """,
            (("guadao", now, now), ("profit_trade", now, now)),
        )
        self.conn.commit()

    def upsert_items(self, items: Iterable[CatalogItem], *, preserve_existing_ids: bool = False) -> int:
        now = utc_now_iso()
        rows = [
            (
                item.market_hash_name,
                item.name_cn,
                item.c5_item_id,
                item.steam_item_id,
                json.dumps(item.raw_json, ensure_ascii=False),
                now,
                now,
            )
            for item in items
        ]
        c5_update = "COALESCE(excluded.c5_item_id, items.c5_item_id)" if preserve_existing_ids else "excluded.c5_item_id"
        steam_update = (
            "COALESCE(excluded.steam_item_id, items.steam_item_id)"
            if preserve_existing_ids
            else "excluded.steam_item_id"
        )
        self.conn.executemany(
            f"""
            INSERT INTO items (
                market_hash_name,
                name_cn,
                c5_item_id,
                steam_item_id,
                raw_json,
                imported_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_hash_name) DO UPDATE SET
                name_cn = excluded.name_cn,
                c5_item_id = {c5_update},
                steam_item_id = {steam_update},
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def search_items_page(
        self,
        keyword: str,
        *,
        limit: int | None = 20,
        offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        tokens, edition = _catalog_search_parts(keyword)
        conditions: list[str] = []
        params: list[Any] = []
        for token in tokens:
            conditions.append("(name_cn LIKE ? OR market_hash_name LIKE ?)")
            like = f"%{token}%"
            params.extend((like, like))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            f"""
            SELECT market_hash_name, name_cn, c5_item_id, steam_item_id, raw_json
            FROM items
            {where}
            """,
            params,
        ).fetchall()
        if edition == "normal":
            rows = [row for row in rows if not _catalog_is_stattrak(row)]
        elif edition == "stattrak":
            rows = [row for row in rows if _catalog_is_stattrak(row)]
        rows.sort(key=lambda row: _catalog_search_sort_key(row, tokens))
        total = len(rows)
        start = max(0, int(offset))
        if limit is None:
            return rows[start:], total
        safe_limit = max(0, int(limit))
        return rows[start : start + safe_limit], total

    def search_items(
        self,
        keyword: str,
        limit: int | None = 20,
        *,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        rows, _total = self.search_items_page(keyword, limit=limit, offset=offset)
        return rows

    def get_item(self, market_hash_name: str) -> sqlite3.Row | None:
        cursor = self.conn.execute(
            """
            SELECT market_hash_name, name_cn, c5_item_id, steam_item_id, raw_json
            FROM items
            WHERE market_hash_name = ?
            """,
            (market_hash_name,),
        )
        return cursor.fetchone()

    def add_watch_item(
        self,
        market_hash_name: str,
        *,
        display_name: str | None = None,
        note: str | None = None,
    ) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO watch_items (
                market_hash_name,
                display_name,
                enabled,
                note,
                created_at,
                updated_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(market_hash_name) DO UPDATE SET
                display_name = excluded.display_name,
                note = excluded.note,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (market_hash_name, display_name, note, now, now),
        )
        self.conn.commit()

    def list_watch_items(self, enabled_only: bool = True) -> list[sqlite3.Row]:
        sql = """
            SELECT
                w.market_hash_name,
                COALESCE(w.display_name, i.name_cn) AS display_name,
                i.name_cn,
                i.c5_item_id,
                w.enabled,
                w.note
            FROM watch_items w
            JOIN items i ON i.market_hash_name = w.market_hash_name
        """
        if enabled_only:
            sql += " WHERE w.enabled = 1"
        sql += " ORDER BY display_name ASC"
        return self.conn.execute(sql).fetchall()

    def add_basket(self, name: str, note: str | None = None) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO baskets (name, pricing_metric, enabled, note, created_at, updated_at)
            VALUES (?, 'c5_price', 1, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                note = excluded.note,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (name, note, now, now),
        )
        self.conn.commit()

    def add_basket_item(self, basket_name: str, market_hash_name: str, quantity: float = 1) -> None:
        basket = self.conn.execute(
            "SELECT id FROM baskets WHERE name = ?",
            (basket_name,),
        ).fetchone()
        if basket is None:
            raise ValueError(f"Basket not found: {basket_name}")

        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO basket_items (basket_id, market_hash_name, quantity, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(basket_id, market_hash_name) DO UPDATE SET
                quantity = excluded.quantity
            """,
            (basket["id"], market_hash_name, quantity, now),
        )
        self.conn.commit()

    def list_baskets(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, name, pricing_metric, enabled, note
            FROM baskets
            ORDER BY name ASC
            """
        ).fetchall()

    def list_basket_items(self, basket_name: str | None = None) -> list[sqlite3.Row]:
        sql = """
            SELECT
                b.name AS basket_name,
                bi.market_hash_name,
                bi.quantity,
                i.name_cn
            FROM basket_items bi
            JOIN baskets b ON b.id = bi.basket_id
            JOIN items i ON i.market_hash_name = bi.market_hash_name
        """
        params: tuple[Any, ...] = ()
        if basket_name:
            sql += " WHERE b.name = ?"
            params = (basket_name,)
        sql += " ORDER BY b.name ASC, i.name_cn ASC"
        return self.conn.execute(sql, params).fetchall()

    def add_position(
        self,
        market_hash_name: str,
        *,
        status: str,
        quantity: float,
        manual_cost: float | None,
        target_buy_price: float | None,
        target_sell_price: float | None,
        note: str | None,
    ) -> int:
        now = utc_now_iso()
        cursor = self.conn.execute(
            """
            INSERT INTO positions (
                market_hash_name,
                status,
                quantity,
                manual_cost,
                target_buy_price,
                target_sell_price,
                note,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_hash_name,
                status,
                quantity,
                manual_cost,
                target_buy_price,
                target_sell_price,
                note,
                now,
                now,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_positions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT
                p.id,
                p.market_hash_name,
                i.name_cn,
                p.status,
                p.quantity,
                p.manual_cost,
                p.target_buy_price,
                p.target_sell_price,
                p.note
            FROM positions p
            JOIN items i ON i.market_hash_name = p.market_hash_name
            ORDER BY p.id DESC
            """
        ).fetchall()

    def add_alert_rule(
        self,
        *,
        target_type: str,
        target_key: str,
        metric: str,
        operator: str,
        threshold: float,
        anchor_value: float | None,
        cooldown_minutes: int,
        note: str | None,
    ) -> int:
        now = utc_now_iso()
        cursor = self.conn.execute(
            """
            INSERT INTO alert_rules (
                target_type,
                target_key,
                metric,
                operator,
                threshold,
                anchor_value,
                cooldown_minutes,
                enabled,
                note,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                target_type,
                target_key,
                metric,
                operator,
                threshold,
                anchor_value,
                cooldown_minutes,
                note,
                now,
                now,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_alert_rules(self, enabled_only: bool = True) -> list[sqlite3.Row]:
        sql = """
            SELECT
                id,
                target_type,
                target_key,
                metric,
                operator,
                threshold,
                anchor_value,
                cooldown_minutes,
                enabled,
                note,
                last_triggered_at,
                last_triggered_value
            FROM alert_rules
        """
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id ASC"
        return self.conn.execute(sql).fetchall()

    def set_rule_triggered(self, rule_id: int, observed_value: float) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            UPDATE alert_rules
            SET last_triggered_at = ?, last_triggered_value = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, observed_value, now, rule_id),
        )
        self.conn.commit()

    def save_price_snapshot(self, state: MarketState) -> None:
        self.conn.execute(
            """
            INSERT INTO price_snapshots (
                market_hash_name,
                observed_at,
                c5_sell_price,
                c5_sell_count,
                c5_bid_price,
                c5_bid_count,
                steam_sell_price,
                steam_sell_count,
                steam_bid_price,
                steam_bid_count,
                ratio,
                raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.market_hash_name,
                utc_now_iso(),
                state.c5_sell_price,
                state.c5_sell_count,
                state.c5_bid_price,
                state.c5_bid_count,
                state.steam_sell_price,
                state.steam_sell_count,
                state.steam_bid_price,
                state.steam_bid_count,
                state.ratio,
                json.dumps(state.raw_json, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def save_basket_snapshot(self, basket_name: str, basket_total: float, raw_json: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO basket_snapshots (basket_name, observed_at, basket_total, raw_json)
            VALUES (?, ?, ?, ?)
            """,
            (basket_name, utc_now_iso(), basket_total, json.dumps(raw_json, ensure_ascii=False)),
        )
        self.conn.commit()

    def add_alert_event(
        self,
        *,
        rule_id: int,
        target_type: str,
        target_key: str,
        metric: str,
        observed_value: float,
        threshold: float,
        message: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO alert_events (
                rule_id,
                target_type,
                target_key,
                metric,
                observed_value,
                threshold,
                message,
                notified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                target_type,
                target_key,
                metric,
                observed_value,
                threshold,
                message,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Inventory pool
    # ------------------------------------------------------------------

    def upsert_pool_item(
        self,
        market_hash_name: str,
        quantity: int,
        *,
        status: str = "holding",
        note: str | None = None,
    ) -> None:
        now = utc_now_iso()
        existing = self.conn.execute(
            "SELECT id FROM inventory_pool WHERE market_hash_name = ?",
            (market_hash_name,),
        ).fetchone()
        if existing:
            self.conn.execute(
                """
                UPDATE inventory_pool
                SET quantity = ?, status = ?, note = ?, updated_at = ?
                WHERE market_hash_name = ?
                """,
                (quantity, status, note, now, market_hash_name),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO inventory_pool (market_hash_name, quantity, status, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (market_hash_name, quantity, status, note, now, now),
            )
        self.conn.commit()

    def sync_pool_from_inventory(
        self,
        inventory_summaries: list[dict[str, Any]],
        *,
        zero_missing_holding: bool = False,
    ) -> int:
        """Sync inventory pool from C5 inventory scan results.

        Upserts each item type with its total inventory_count.
        Optionally zeroes idle holding types that are absent from the live scan.
        Returns the number of item types synced.
        """
        now = utc_now_iso()
        count = 0
        seen_market_hash_names: set[str] = set()
        for summary in inventory_summaries:
            mhn = str(summary.get("market_hash_name") or "").strip()
            if not mhn:
                continue
            qty = int(summary.get("inventory_count", 0))
            if qty <= 0:
                continue
            seen_market_hash_names.add(mhn)
            existing = self.conn.execute(
                "SELECT id, status FROM inventory_pool WHERE market_hash_name = ?",
                (mhn,),
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE inventory_pool SET quantity = ?, updated_at = ? WHERE market_hash_name = ?",
                    (qty, now, mhn),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO inventory_pool (market_hash_name, quantity, status, note, created_at, updated_at)
                    VALUES (?, ?, 'holding', NULL, ?, ?)
                    """,
                    (mhn, qty, now, now),
                )
            count += 1
        if zero_missing_holding:
            if seen_market_hash_names:
                placeholders = ",".join("?" for _ in seen_market_hash_names)
                self.conn.execute(
                    f"""
                    UPDATE inventory_pool
                    SET quantity = 0, updated_at = ?
                    WHERE status = ?
                    AND quantity > 0
                    AND market_hash_name NOT IN ({placeholders})
                    """,
                    (now, POOL_STATUS_HOLDING, *sorted(seen_market_hash_names)),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE inventory_pool
                    SET quantity = 0, updated_at = ?
                    WHERE status = ?
                    AND quantity > 0
                    """,
                    (now, POOL_STATUS_HOLDING),
                )
        self.conn.commit()
        return count

    def list_pool_items(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT id, market_hash_name, quantity, status, note, created_at, updated_at FROM inventory_pool"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY market_hash_name ASC"
        return self.conn.execute(sql, params).fetchall()

    def get_pool_status_map(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT market_hash_name, status FROM inventory_pool ORDER BY market_hash_name ASC"
        ).fetchall()
        return {row["market_hash_name"]: row["status"] for row in rows}

    def get_pool_market_hash_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT market_hash_name FROM inventory_pool WHERE quantity > 0 ORDER BY market_hash_name"
        ).fetchall()
        return [row["market_hash_name"] for row in rows]

    def remove_pool_item(self, market_hash_name: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM inventory_pool WHERE market_hash_name = ?",
            (market_hash_name,),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_pool_status(self, market_hash_name: str, status: str) -> None:
        now = utc_now_iso()
        self.conn.execute(
            "UPDATE inventory_pool SET status = ?, updated_at = ? WHERE market_hash_name = ?",
            (status, now, market_hash_name),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Inventory assets (per-asset tracking for execution)
    # ------------------------------------------------------------------

    def upsert_inventory_assets(self, items: list[dict[str, Any]]) -> int:
        now = utc_now_iso()
        rows: list[tuple[Any, ...]] = []
        for item in items:
            asset_id = str(item.get("assetId") or "").strip()
            market_hash_name = str(item.get("marketHashName") or "").strip()
            if not asset_id or not market_hash_name:
                continue
            steam_id = str(item.get("steamId") or "").strip() or None
            tradable = 1 if item.get("ifTradable") is True else 0
            status = "available" if tradable else "locked"
            rows.append((asset_id, market_hash_name, steam_id, tradable, status, now, now))
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO inventory_assets (
                asset_id, market_hash_name, steam_id, tradable, status, last_seen_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                market_hash_name = excluded.market_hash_name,
                steam_id = excluded.steam_id,
                tradable = excluded.tradable,
                status = CASE
                    WHEN inventory_assets.status IN ('listed', 'sold', 'listing_pending') THEN inventory_assets.status
                    ELSE excluded.status
                END,
                last_seen_at = excluded.last_seen_at
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def delete_assets_absent_from_live_inventory(self, seen_asset_ids: set[str]) -> int:
        self.conn.execute("DROP TABLE IF EXISTS temp_seen_inventory_assets")
        self.conn.execute(
            "CREATE TEMP TABLE temp_seen_inventory_assets (asset_id TEXT PRIMARY KEY)"
        )
        if seen_asset_ids:
            self.conn.executemany(
                "INSERT OR IGNORE INTO temp_seen_inventory_assets (asset_id) VALUES (?)",
                [(asset_id,) for asset_id in sorted(seen_asset_ids)],
            )
        cursor = self.conn.execute(
            """
            DELETE FROM inventory_assets
            WHERE status IN ('available', 'locked')
              AND NOT EXISTS (
                  SELECT 1
                  FROM temp_seen_inventory_assets seen
                  WHERE seen.asset_id = inventory_assets.asset_id
              )
            """,
        )
        self.conn.execute("DROP TABLE IF EXISTS temp_seen_inventory_assets")
        self.conn.commit()
        return cursor.rowcount

    def pick_tradable_asset(
        self,
        market_hash_name: str,
        *,
        steam_id: str | None = None,
        exclude_asset_ids: set[str] | None = None,
    ) -> sqlite3.Row | None:
        now = utc_now_iso()
        sql = """
            SELECT asset_id, market_hash_name, steam_id
            FROM inventory_assets
            WHERE market_hash_name = ?
              AND tradable = 1
              AND status = 'available'
              AND NOT EXISTS (
                  SELECT 1
                  FROM asset_reservations r
                  WHERE r.asset_id = inventory_assets.asset_id
                    AND (
                        r.status = 'consumed'
                        OR (
                            r.status = 'active'
                            AND (r.reserved_until IS NULL OR r.reserved_until > ?)
                        )
                    )
              )
        """
        params: list[Any] = [market_hash_name, now]
        if steam_id:
            sql += " AND steam_id = ?"
            params.append(steam_id)
        if exclude_asset_ids:
            placeholders = ", ".join("?" for _ in exclude_asset_ids)
            sql += f" AND asset_id NOT IN ({placeholders})"
            params.extend(sorted(exclude_asset_ids))
        sql += " ORDER BY asset_id ASC LIMIT 1"
        return self.conn.execute(sql, tuple(params)).fetchone()

    def set_asset_status(self, asset_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE inventory_assets SET status = ?, last_seen_at = ? WHERE asset_id = ?",
            (status, utc_now_iso(), asset_id),
        )
        self.conn.commit()

    def get_asset(self, asset_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT asset_id, market_hash_name, steam_id, tradable, status, last_seen_at, created_at
            FROM inventory_assets
            WHERE asset_id = ?
            """,
            (asset_id,),
        ).fetchone()

    def list_assets(
        self,
        *,
        market_hash_name: str | None = None,
        steam_id: str | None = None,
        tradable: bool | None = None,
        status: str | None = None,
        exclude_reserved: bool = False,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT asset_id, market_hash_name, steam_id, tradable, status, last_seen_at, created_at
            FROM inventory_assets
            WHERE 1 = 1
        """
        params: list[Any] = []
        if market_hash_name is not None:
            sql += " AND market_hash_name = ?"
            params.append(market_hash_name)
        if steam_id is not None:
            sql += " AND steam_id = ?"
            params.append(steam_id)
        if tradable is not None:
            sql += " AND tradable = ?"
            params.append(1 if tradable else 0)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if exclude_reserved:
            sql += """
                AND NOT EXISTS (
                    SELECT 1
                    FROM asset_reservations r
                    WHERE r.asset_id = inventory_assets.asset_id
                      AND (
                          r.status = 'consumed'
                          OR (
                              r.status = 'active'
                              AND (r.reserved_until IS NULL OR r.reserved_until > ?)
                          )
                      )
                )
            """
            params.append(utc_now_iso())
        sql += " ORDER BY asset_id ASC"
        return self.conn.execute(sql, tuple(params)).fetchall()

    def list_asset_ids(
        self,
        market_hash_name: str,
        *,
        steam_id: str | None = None,
    ) -> list[str]:
        rows = self.list_assets(market_hash_name=market_hash_name, steam_id=steam_id)
        return [str(row["asset_id"]) for row in rows]

    # ------------------------------------------------------------------
    # Asset reservations / profit-trade isolation
    # ------------------------------------------------------------------

    def release_expired_asset_reservations(self) -> int:
        now = utc_now_iso()
        cursor = self.conn.execute(
            """
            UPDATE asset_reservations
            SET status = 'released',
                updated_at = ?,
                released_at = ?
            WHERE status = 'active'
              AND reserved_until IS NOT NULL
              AND reserved_until <= ?
            """,
            (now, now, now),
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def reserve_asset(
        self,
        *,
        asset_id: str,
        market_hash_name: str,
        owner: str,
        purpose: str,
        reserved_until: str | None = None,
        operation_id: int | None = None,
        note: str | None = None,
    ) -> int | None:
        self.release_expired_asset_reservations()
        now = utc_now_iso()
        try:
            cursor = self.conn.execute(
                """
                INSERT INTO asset_reservations (
                    asset_id,
                    market_hash_name,
                    owner,
                    purpose,
                    status,
                    operation_id,
                    reserved_until,
                    note,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    market_hash_name,
                    owner,
                    purpose,
                    operation_id,
                    reserved_until,
                    note,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return None
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_active_asset_reservation(self, asset_id: str) -> sqlite3.Row | None:
        now = utc_now_iso()
        return self.conn.execute(
            """
            SELECT *
            FROM asset_reservations
            WHERE asset_id = ?
              AND (
                  status = 'consumed'
                  OR (
                      status = 'active'
                      AND (reserved_until IS NULL OR reserved_until > ?)
                  )
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (asset_id, now),
        ).fetchone()

    def consume_asset_reservation(
        self,
        *,
        asset_id: str,
        owner: str | None = None,
        operation_id: int | None = None,
        note: str | None = None,
    ) -> bool:
        now = utc_now_iso()
        sql = """
            UPDATE asset_reservations
            SET status = 'consumed',
                updated_at = ?,
                operation_id = COALESCE(?, operation_id),
                note = COALESCE(?, note)
            WHERE asset_id = ?
              AND status = 'active'
        """
        params: list[Any] = [now, operation_id, note, asset_id]
        if owner:
            sql += " AND owner = ?"
            params.append(owner)
        cursor = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return int(cursor.rowcount) > 0

    def update_asset_reservation_deadline(
        self,
        *,
        asset_id: str,
        owner: str | None = None,
        operation_id: int | None = None,
        reserved_until: str | None = None,
        note: str | None = None,
    ) -> bool:
        now = utc_now_iso()
        sql = """
            UPDATE asset_reservations
            SET reserved_until = ?,
                updated_at = ?,
                operation_id = COALESCE(?, operation_id),
                note = COALESCE(?, note)
            WHERE asset_id = ?
              AND status = 'active'
        """
        params: list[Any] = [reserved_until, now, operation_id, note, asset_id]
        if owner:
            sql += " AND owner = ?"
            params.append(owner)
        cursor = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return int(cursor.rowcount) > 0

    def attach_asset_reservation_operation(
        self,
        *,
        reservation_id: int,
        operation_id: int,
        note: str | None = None,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE asset_reservations
            SET operation_id = ?,
                updated_at = ?,
                note = COALESCE(?, note)
            WHERE id = ?
              AND status IN ('active', 'consumed')
            """,
            (operation_id, utc_now_iso(), note, reservation_id),
        )
        self.conn.commit()
        return int(cursor.rowcount) > 0

    def release_asset_reservation(
        self,
        *,
        asset_id: str,
        owner: str | None = None,
        reason: str | None = None,
    ) -> bool:
        now = utc_now_iso()
        sql = """
            UPDATE asset_reservations
            SET status = 'released',
                updated_at = ?,
                released_at = ?,
                note = COALESCE(?, note)
            WHERE asset_id = ?
              AND status IN ('active', 'consumed')
        """
        params: list[Any] = [now, now, reason, asset_id]
        if owner:
            sql += " AND owner = ?"
            params.append(owner)
        cursor = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return int(cursor.rowcount) > 0

    def list_asset_reservations(
        self,
        *,
        owner: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM asset_reservations WHERE 1 = 1"
        params: list[Any] = []
        if owner:
            sql += " AND owner = ?"
            params.append(owner)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------
    # Profit trades
    # ------------------------------------------------------------------

    def add_profit_trade(
        self,
        *,
        trade_no: str,
        market_hash_name: str,
        status: str = "candidate",
        step_key: str = "discovered",
        step_index: int = 0,
        a_asset_id: str | None = None,
        a_steam_id: str | None = None,
        b_asset_id: str | None = None,
        steam_listing_id: str | None = None,
        c5_product_id: str | None = None,
        steam_buy_price: float | None = None,
        steam_balance_discount: float | None = None,
        steam_real_cost: float | None = None,
        c5_listing_price: float | None = None,
        c5_expected_net_price: float | None = None,
        c5_sold_net_price: float | None = None,
        expected_profit: float | None = None,
        realized_profit: float | None = None,
        expected_roi: float | None = None,
        realized_roi: float | None = None,
        error: str | None = None,
        note: str | None = None,
    ) -> int:
        now = utc_now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO profit_trades (
                    trade_no,
                    market_hash_name,
                    status,
                    step_key,
                    step_index,
                    a_asset_id,
                    a_steam_id,
                    b_asset_id,
                    steam_listing_id,
                    c5_product_id,
                    steam_buy_price,
                    steam_balance_discount,
                    steam_real_cost,
                    c5_listing_price,
                    c5_expected_net_price,
                    c5_sold_net_price,
                    expected_profit,
                    realized_profit,
                    expected_roi,
                    realized_roi,
                    error,
                    note,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_no,
                    market_hash_name,
                    status,
                    step_key,
                    step_index,
                    a_asset_id,
                    a_steam_id,
                    b_asset_id,
                    steam_listing_id,
                    c5_product_id,
                    steam_buy_price,
                    steam_balance_discount,
                    steam_real_cost,
                    c5_listing_price,
                    c5_expected_net_price,
                    c5_sold_net_price,
                    expected_profit,
                    realized_profit,
                    expected_roi,
                    realized_roi,
                    error,
                    note,
                    now,
                    now,
                ),
            )
            trade_id = int(cursor.lastrowid)
            self.conn.execute(
                """
                INSERT INTO profit_trade_state_events (
                    trade_id,
                    event_type,
                    status_from,
                    status_to,
                    step_key_from,
                    step_key_to,
                    step_index_from,
                    step_index_to,
                    reason,
                    context_json,
                    created_at
                ) VALUES (?, 'created', NULL, ?, NULL, ?, NULL, ?, ?, '{}', ?)
                """,
                (trade_id, status, step_key, int(step_index), error, now),
            )
        _emit_profit_trade_local_event(
            component="profit_trade_state_machine",
            operation="trade_created",
            message="Profit Trade record created",
            trade_id=trade_id,
            trade_no=trade_no,
            market_hash_name=market_hash_name,
            asset_id=a_asset_id,
            state_to=status,
            step_to=step_key,
            safe_context={"step_index": int(step_index), "reason": error},
        )
        return trade_id

    def create_profit_trade_from_long_buy_fill(
        self,
        *,
        fill_id: int,
        trade_no: str,
        market_hash_name: str,
        a_asset_id: str | None,
        a_steam_id: str | None,
        b_asset_id: str | None,
        steam_listing_id: str | None,
        steam_buy_price: float,
        steam_balance_discount: float,
        steam_real_cost: float,
        c5_listing_price: float,
        c5_expected_net_price: float,
        expected_profit: float,
        expected_roi: float,
        note: str,
        manual_reason: str | None = None,
    ) -> int:
        """Atomically consume one fill, reserve A, and create its trade.

        The purchase receipt is already irreversible when this method runs.
        Keeping the fill row pending until the reservation and trade insert
        commit makes a restart safe: the same receipt can never create two
        Profit Trade rows.
        """

        now = utc_now_iso()
        with self.conn:
            fill = self.conn.execute(
                "SELECT * FROM profit_trade_long_buy_fills WHERE id = ?",
                (int(fill_id),),
            ).fetchone()
            if fill is None:
                raise LookupError(f"profit trade long buy fill not found: {fill_id}")
            if str(fill["state"] or "") != "pending":
                existing_trade_id = fill["profit_trade_id"]
                if existing_trade_id is None:
                    raise RuntimeError(
                        f"long-buy fill {fill_id} is already {fill['state']} without a trade"
                    )
                return int(existing_trade_id)

            status = "manual_required" if manual_reason else "steam_bought"
            step_key = "steam_bought"
            step_index = 3
            reservation_id: int | None = None
            if a_asset_id:
                try:
                    reservation_cursor = self.conn.execute(
                        """
                        INSERT INTO asset_reservations (
                            asset_id,
                            market_hash_name,
                            owner,
                            purpose,
                            status,
                            operation_id,
                            reserved_until,
                            note,
                            created_at,
                            updated_at
                        ) VALUES (?, ?, 'profit_trade', 'sell_existing_a',
                                  'active', NULL, NULL, ?, ?, ?)
                        """,
                        (
                            str(a_asset_id),
                            str(market_hash_name),
                            note,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise RuntimeError(
                        f"long-buy fill A asset is already reserved: {a_asset_id}"
                    ) from exc
                reservation_id = int(reservation_cursor.lastrowid)
            elif not manual_reason:
                raise ValueError("a_asset_id is required unless manual_reason is set")

            cursor = self.conn.execute(
                """
                INSERT INTO profit_trades (
                    trade_no,
                    market_hash_name,
                    status,
                    step_key,
                    step_index,
                    a_asset_id,
                    a_steam_id,
                    b_asset_id,
                    steam_listing_id,
                    steam_buy_price,
                    steam_balance_discount,
                    steam_real_cost,
                    c5_listing_price,
                    c5_expected_net_price,
                    expected_profit,
                    expected_roi,
                    error,
                    note,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(trade_no),
                    str(market_hash_name),
                    status,
                    step_key,
                    step_index,
                    str(a_asset_id or "").strip() or None,
                    str(a_steam_id or "").strip() or None,
                    str(b_asset_id or "").strip() or None,
                    str(steam_listing_id or "").strip() or None,
                    float(steam_buy_price),
                    float(steam_balance_discount),
                    float(steam_real_cost),
                    float(c5_listing_price),
                    float(c5_expected_net_price),
                    float(expected_profit),
                    float(expected_roi),
                    manual_reason,
                    note,
                    now,
                    now,
                ),
            )
            trade_id = int(cursor.lastrowid)
            if reservation_id is not None:
                self.conn.execute(
                    """
                    UPDATE asset_reservations
                    SET operation_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (trade_id, now, reservation_id),
                )
            self.conn.execute(
                """
                INSERT INTO profit_trade_state_events (
                    trade_id,
                    event_type,
                    status_from,
                    status_to,
                    step_key_from,
                    step_key_to,
                    step_index_from,
                    step_index_to,
                    reason,
                    context_json,
                    created_at
                ) VALUES (?, 'long_buy_fill_imported', NULL, ?, NULL, ?, NULL,
                          ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    status,
                    step_key,
                    step_index,
                    manual_reason,
                    _json_object(
                        {
                            "longBuyFillId": int(fill_id),
                            "purchaseId": fill["purchase_id"],
                            "longBuyOrderId": fill["long_buy_order_id"],
                        }
                    ),
                    now,
                ),
            )
            fill_state = "manual_required" if manual_reason else "processed"
            self.conn.execute(
                """
                UPDATE profit_trade_long_buy_fills
                SET state = ?,
                    profit_trade_id = ?,
                    processed_at = ?,
                    updated_at = ?
                WHERE id = ? AND state = 'pending'
                """,
                (fill_state, trade_id, now, now, int(fill_id)),
            )
        _emit_profit_trade_local_event(
            component="profit_trade_long_buy",
            operation="fill_imported",
            message="Confirmed long-term buy-order fill entered Profit Trade",
            trade_id=trade_id,
            trade_no=str(trade_no),
            market_hash_name=str(market_hash_name),
            asset_id=str(a_asset_id or "") or None,
            state_to=status,
            step_to=step_key,
            safe_context={
                "fill_id": int(fill_id),
                "manual_required": bool(manual_reason),
            },
        )
        return trade_id

    def update_profit_trade(
        self,
        trade_id: int,
        **fields: Any,
    ) -> None:
        event_reason = fields.pop("_event_reason", None)
        event_context = fields.pop("_event_context", None)
        log_event_id = fields.pop("_log_event_id", None)
        allowed = {
            "market_hash_name",
            "status",
            "step_key",
            "step_index",
            "a_asset_id",
            "a_steam_id",
            "b_asset_id",
            "steam_listing_id",
            "c5_product_id",
            "steam_buy_price",
            "steam_balance_discount",
            "steam_real_cost",
            "c5_listing_price",
            "c5_expected_net_price",
            "c5_sold_net_price",
            "expected_profit",
            "realized_profit",
            "expected_roi",
            "realized_roi",
            "error",
            "note",
            "completed_at",
        }
        current = self.get_profit_trade(trade_id)
        if current is None:
            return
        parts: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "note" and isinstance(value, str):
                try:
                    current_note = json.loads(str(current["note"] or "{}"))
                    next_note = json.loads(value)
                except (TypeError, ValueError):
                    current_note = {}
                    next_note = {}
                if isinstance(current_note, dict) and isinstance(next_note, dict):
                    for evidence_key in (
                        "scanOrderbookSnapshot",
                        "executionOrderbookSnapshots",
                    ):
                        if evidence_key not in next_note and evidence_key in current_note:
                            next_note[evidence_key] = current_note[evidence_key]
                    value = json.dumps(next_note, ensure_ascii=False)
            parts.append(f"{key} = ?")
            params.append(value)
        if not parts:
            return
        now = utc_now_iso()
        parts.append("updated_at = ?")
        params.append(now)
        status = fields.get("status")
        if status == "completed" and "completed_at" not in fields:
            parts.append("completed_at = ?")
            params.append(now)
        params.append(trade_id)
        status_from = str(current["status"] or "")
        status_to = str(fields.get("status", current["status"]) or "")
        step_key_from = str(current["step_key"] or "")
        step_key_to = str(fields.get("step_key", current["step_key"]) or "")
        step_index_from = int(current["step_index"] or 0)
        step_index_to = int(fields.get("step_index", current["step_index"]) or 0)
        state_changed = (
            status_from != status_to
            or step_key_from != step_key_to
            or step_index_from != step_index_to
        )
        if event_reason is None:
            event_reason = fields.get("error")
        if not isinstance(event_context, dict):
            event_context = {}
        with self.conn:
            self.conn.execute(
                f"UPDATE profit_trades SET {', '.join(parts)} WHERE id = ?",
                tuple(params),
            )
            if state_changed:
                self.conn.execute(
                    """
                    INSERT INTO profit_trade_state_events (
                        trade_id,
                        event_type,
                        status_from,
                        status_to,
                        step_key_from,
                        step_key_to,
                        step_index_from,
                        step_index_to,
                        reason,
                        log_event_id,
                        context_json,
                        created_at
                    ) VALUES (?, 'transition', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        status_from,
                        status_to,
                        step_key_from,
                        step_key_to,
                        step_index_from,
                        step_index_to,
                        str(event_reason) if event_reason is not None else None,
                        str(log_event_id) if log_event_id is not None else None,
                        json.dumps(event_context, ensure_ascii=False),
                        now,
                    ),
                )
        if state_changed:
            _emit_profit_trade_local_event(
                component="profit_trade_state_machine",
                operation="state_transition",
                message="Profit Trade state changed",
                trade_id=trade_id,
                trade_no=str(current["trade_no"] or "") or None,
                market_hash_name=str(current["market_hash_name"] or "") or None,
                asset_id=str(current["a_asset_id"] or "") or None,
                state_from=status_from,
                state_to=status_to,
                step_from=step_key_from,
                step_to=step_key_to,
                safe_context={
                    "step_index_from": step_index_from,
                    "step_index_to": step_index_to,
                    "reason": event_reason,
                    "context": event_context,
                },
            )

    def add_profit_trade_audit_event(
        self,
        trade_id: int,
        *,
        event_type: str,
        reason: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit event without changing the trade state machine."""

        current = self.get_profit_trade(trade_id)
        if current is None:
            raise ValueError(f"profit trade not found: {trade_id}")
        normalized_event_type = str(event_type or "").strip()
        if not normalized_event_type:
            raise ValueError("event_type is required")
        now = utc_now_iso()
        safe_context = context if isinstance(context, dict) else {}
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO profit_trade_state_events (
                    trade_id,
                    event_type,
                    status_from,
                    status_to,
                    step_key_from,
                    step_key_to,
                    step_index_from,
                    step_index_to,
                    reason,
                    context_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    normalized_event_type,
                    current["status"],
                    current["status"],
                    current["step_key"],
                    current["step_key"],
                    int(current["step_index"] or 0),
                    int(current["step_index"] or 0),
                    str(reason) if reason is not None else None,
                    json.dumps(safe_context, ensure_ascii=False),
                    now,
                ),
            )
        _emit_profit_trade_local_event(
            component="profit_trade_manual_record",
            operation=normalized_event_type,
            message="Profit Trade manual record audit event",
            trade_id=trade_id,
            trade_no=str(current["trade_no"] or "") or None,
            market_hash_name=str(current["market_hash_name"] or "") or None,
            asset_id=str(current["a_asset_id"] or "") or None,
            state_from=str(current["status"] or "") or None,
            state_to=str(current["status"] or "") or None,
            step_from=str(current["step_key"] or "") or None,
            step_to=str(current["step_key"] or "") or None,
            safe_context={"reason": reason, **safe_context},
        )

    def get_profit_trade_runtime_state(self, state_key: str) -> dict[str, Any] | None:
        normalized_key = str(state_key or "").strip()
        if not normalized_key:
            raise ValueError("state_key is required")
        row = self.conn.execute(
            "SELECT * FROM profit_trade_runtime_state WHERE state_key = ?",
            (normalized_key,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        result = payload if isinstance(payload, dict) else {}
        return {
            **result,
            "stateKey": normalized_key,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def set_profit_trade_runtime_state(
        self,
        state_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_key = str(state_key or "").strip()
        if not normalized_key:
            raise ValueError("state_key is required")
        safe_payload = payload if isinstance(payload, dict) else {}
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO profit_trade_runtime_state (
                    state_key,
                    payload_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_key,
                    json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        return self.get_profit_trade_runtime_state(normalized_key) or {
            **safe_payload,
            "stateKey": normalized_key,
            "createdAt": now,
            "updatedAt": now,
        }

    def get_profit_trade(self, trade_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM profit_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()

    def get_live_profit_trade_for_asset(self, asset_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM profit_trades
            WHERE a_asset_id = ?
              AND status NOT IN ('completed', 'failed', 'manual_required', 'cancelled')
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (asset_id,),
        ).fetchone()

    def list_profit_trades(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM profit_trades"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        return self.conn.execute(sql, tuple(params)).fetchall()

    def list_profit_trades_for_market_hash_name(
        self,
        market_hash_name: str,
        *,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT *
            FROM profit_trades
            WHERE market_hash_name = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (str(market_hash_name), max(1, int(limit))),
        ).fetchall()

    def list_profit_trades_for_manual_request(
        self,
        request_id: str,
        *,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT *
            FROM profit_trades
            WHERE json_extract(note, '$.manualExecutionRequestId') = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (str(request_id or "").strip(), max(1, int(limit))),
        ).fetchall()

    # ------------------------------------------------------------------
    # Profit Trade observability
    # ------------------------------------------------------------------

    @staticmethod
    def _profit_trade_roi_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        try:
            raw_payload = json.loads(str(row["raw_json"] or "{}")) if "raw_json" in keys else {}
        except (TypeError, ValueError):
            raw_payload = {}
        raw_orderbook = (
            raw_payload.get("steamOrderbook")
            if isinstance(raw_payload, dict) and isinstance(raw_payload.get("steamOrderbook"), dict)
            else {}
        )

        def orderbook_value(column: str, raw_key: str) -> Any:
            if column in keys and row[column] is not None:
                return row[column]
            return raw_orderbook.get(raw_key)

        def row_or_raw_value(column: str, raw_key: str) -> Any:
            if column in keys and row[column] is not None:
                return row[column]
            return raw_payload.get(raw_key) if isinstance(raw_payload, dict) else None

        crossed_value = orderbook_value("steam_orderbook_crossed", "crossed")
        manual_executable_quantity: int | None = None
        if isinstance(raw_payload, dict):
            raw_manual_quantity = raw_payload.get("manualExecutableQuantity")
            if raw_manual_quantity is not None:
                try:
                    manual_executable_quantity = max(0, int(raw_manual_quantity))
                except (TypeError, ValueError, OverflowError):
                    manual_executable_quantity = None
        steam_orderbook = {
            "observedAt": orderbook_value("steam_orderbook_observed_at", "observedAt"),
            "currencyId": orderbook_value("steam_currency_id", "currencyId"),
            "sellerFloorPrice": row["steam_buy_price"],
            "sellerFloorCount": orderbook_value("steam_seller_floor_count", "sellerFloorCount"),
            "buyerMaxPrice": orderbook_value("steam_buyer_max_price", "buyerMaxPrice"),
            "buyerMaxCount": orderbook_value("steam_buyer_max_count", "buyerMaxCount"),
            "spreadAmount": orderbook_value("steam_spread_amount", "spreadAmount"),
            "spreadPct": orderbook_value("steam_spread_pct", "spreadPct"),
            "crossed": bool(crossed_value) if crossed_value is not None else None,
            "sellOrderCountTotal": raw_orderbook.get("sellOrderCountTotal"),
            "buyOrderCountTotal": raw_orderbook.get("buyOrderCountTotal"),
            "sellLevels": list(raw_orderbook.get("sellLevels") or []),
            "buyLevels": list(raw_orderbook.get("buyLevels") or []),
        }
        crossed_listing_probe = (
            dict(raw_payload.get("crossedListingProbe") or {})
            if isinstance(raw_payload, dict)
            and isinstance(raw_payload.get("crossedListingProbe"), dict)
            else None
        )
        is_active = bool(row["active"]) if "active" in keys else None
        execution_status_code = str(row["execution_status"] or "watch_only")
        if is_active is False:
            execution_status = "exited"
        elif execution_status_code == "executable":
            execution_status = "executable"
        elif execution_status_code == "manual_review":
            execution_status = "manual_review"
        elif execution_status_code in {"listings_cooldown", "listings_probe_ready"}:
            execution_status = execution_status_code
        elif execution_status_code in {"c5_risk_blocked", "ai_audit_blocked"}:
            execution_status = "blocked"
        else:
            execution_status = "observe_only"
        return {
            "id": int(row["id"]) if "id" in keys and row["id"] is not None else None,
            "scanId": row["scan_id"] if "scan_id" in keys else None,
            "marketHashName": row["market_hash_name"],
            "name": row["name_cn"] if "name_cn" in keys else None,
            "active": is_active,
            "eventType": row["event_type"] if "event_type" in keys else None,
            "steamBuyPrice": row["steam_buy_price"],
            "steamOrderbook": steam_orderbook,
            "crossedListingProbe": crossed_listing_probe,
            "latestRefresh": (
                dict(raw_payload.get("latestRefresh") or {})
                if isinstance(raw_payload, dict)
                and isinstance(raw_payload.get("latestRefresh"), dict)
                else None
            ),
            "steamPriceSource": row["steam_price_source"] if "steam_price_source" in keys else None,
            "c5ListingPrice": row["c5_listing_price"],
            "c5PriceSource": row["c5_price_source"] if "c5_price_source" in keys else None,
            "c5ExpectedNetPrice": row["c5_expected_net_price"],
            "balanceDiscount": row["balance_discount"],
            # `balance_discount` is the exact per-snapshot ROI basis.  Expose
            # an explicit name for the observation UI without duplicating a
            # source-of-truth column in SQLite.
            "roiBasis": row["balance_discount"],
            "expectedProfit": row["expected_profit"],
            "expectedRoi": row["expected_roi"],
            "expectedRoiPct": (
                float(row["expected_roi"]) * 100.0
                if row["expected_roi"] is not None
                else None
            ),
            "buyOrderReferenceRoi": (
                row["buy_order_reference_roi"]
                if "buy_order_reference_roi" in keys
                else None
            ),
            "buyOrderReferenceRoiPct": (
                float(row["buy_order_reference_roi"]) * 100.0
                if "buy_order_reference_roi" in keys
                and row["buy_order_reference_roi"] is not None
                else None
            ),
            "buyOrderReferenceProfit": (
                row["buy_order_reference_profit"]
                if "buy_order_reference_profit" in keys
                else None
            ),
            "buyOrderReferenceStatus": (
                row["buy_order_reference_status"]
                if "buy_order_reference_status" in keys
                else None
            ),
            "competitorBuyPrice": (
                raw_payload.get("competitorBuyPrice")
                if isinstance(raw_payload, dict)
                else None
            ),
            "competitorBuyRoi": (
                raw_payload.get("competitorBuyRoi")
                if isinstance(raw_payload, dict)
                else None
            ),
            "competitorBuyProfit": (
                raw_payload.get("competitorBuyProfit")
                if isinstance(raw_payload, dict)
                else None
            ),
            "competitorBuyStatus": (
                raw_payload.get("competitorBuyStatus")
                if isinstance(raw_payload, dict)
                else None
            ),
            "excludedOwnBuyPrices": (
                list(raw_payload.get("excludedOwnBuyPrices") or [])
                if isinstance(raw_payload, dict)
                else []
            ),
            "longBuyOrder": (
                dict(raw_payload.get("longBuyOrder") or {})
                if isinstance(raw_payload, dict)
                and isinstance(raw_payload.get("longBuyOrder"), dict)
                else None
            ),
            "longBuyProposal": (
                dict(raw_payload.get("longBuyProposal") or {})
                if isinstance(raw_payload, dict)
                and isinstance(raw_payload.get("longBuyProposal"), dict)
                else None
            ),
            "minRoi": row["min_roi"],
            "manualReviewRoi": row["manual_review_roi"],
            "inventoryCount": row["inventory_count"],
            "tradableCount": row["tradable_count"],
            "manualExecutableQuantity": manual_executable_quantity,
            "c5RecentSoldNetPrice": (
                row["c5_recent_sold_net_price"] if "c5_recent_sold_net_price" in keys else None
            ),
            "c5RecentSoldCount": (
                row["c5_recent_sold_count"] if "c5_recent_sold_count" in keys else None
            ),
            "c5CurrentSellPrice": (
                row_or_raw_value("c5_current_sell_price", "c5CurrentSellPrice")
            ),
            "c5OnSaleCount": row_or_raw_value("c5_on_sale_count", "c5OnSaleCount"),
            "c5PurchaseMaxPrice": (
                row_or_raw_value("c5_purchase_max_price", "c5PurchaseMaxPrice")
            ),
            "c5PurchaseCount": row_or_raw_value("c5_purchase_count", "c5PurchaseCount"),
            "c5PurchaseSellRatio": (
                raw_payload.get("c5PurchaseSellRatio")
                if isinstance(raw_payload, dict)
                else None
            ),
            "c5MinPurchaseSellRatio": (
                raw_payload.get("c5MinPurchaseSellRatio")
                if isinstance(raw_payload, dict)
                else None
            ),
            "riskStatus": row["risk_status"],
            "riskReason": row["risk_reason"],
            "executionStatus": execution_status,
            "executionStatusCode": execution_status_code,
            "executionReason": row["execution_reason"],
            "firstSeenAt": row["first_seen_at"] if "first_seen_at" in keys else None,
            "lastObservedAt": row["last_observed_at"] if "last_observed_at" in keys else None,
            "observedAt": row["observed_at"] if "observed_at" in keys else None,
            "updatedAt": row["updated_at"] if "updated_at" in keys else None,
            "exitedAt": row["exited_at"] if "exited_at" in keys else None,
            "exitReason": row["exit_reason"] if "exit_reason" in keys else None,
        }

    def get_profit_trade_roi_watch(self, market_hash_name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM profit_trade_roi_watch WHERE market_hash_name = ?",
            (str(market_hash_name or "").strip(),),
        ).fetchone()

    def record_profit_trade_roi_scan(
        self,
        observations: list[dict[str, Any]],
        *,
        scan_id: str,
        observed_at: str | None = None,
        exit_reasons: dict[str, str] | None = None,
        exit_observations: dict[str, dict[str, Any]] | None = None,
        preserve_market_hash_names: Iterable[str] | None = None,
        preserve_refresh_failures: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """Persist one successfully completed scan and retire stale watch rows.

        Callers must not call this method for a globally failed scan.  That
        boundary is intentional: retiring rows and writing the matching exit
        observations happen in the same transaction as all current upserts.
        """

        timestamp = observed_at or utc_now_iso()
        normalized: list[dict[str, Any]] = []
        active_names: set[str] = set()
        for raw in observations:
            market_hash_name = str(raw.get("market_hash_name") or "").strip()
            try:
                expected_roi = float(raw.get("expected_roi"))
            except (TypeError, ValueError):
                continue
            raw_payload = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
            keep_active = bool(raw.get("keep_active")) or bool(
                raw_payload.get("longBuyOrder")
                or raw_payload.get("longBuyProposal")
            )
            if not market_hash_name or (expected_roi <= 0 and not keep_active):
                continue
            orderbook = (
                raw_payload.get("steamOrderbook")
                if isinstance(raw_payload.get("steamOrderbook"), dict)
                else {}
            )
            row = {
                "market_hash_name": market_hash_name,
                "name_cn": raw.get("name_cn"),
                "steam_buy_price": raw.get("steam_buy_price"),
                "steam_seller_floor_count": orderbook.get("sellerFloorCount"),
                "steam_buyer_max_price": orderbook.get("buyerMaxPrice"),
                "steam_buyer_max_count": orderbook.get("buyerMaxCount"),
                "steam_spread_amount": orderbook.get("spreadAmount"),
                "steam_spread_pct": orderbook.get("spreadPct"),
                "steam_orderbook_crossed": (
                    1 if orderbook.get("crossed") is True
                    else 0 if orderbook.get("crossed") is False
                    else None
                ),
                "steam_currency_id": orderbook.get("currencyId"),
                "steam_orderbook_observed_at": orderbook.get("observedAt"),
                "steam_price_source": raw.get("steam_price_source"),
                "c5_listing_price": raw.get("c5_listing_price"),
                "c5_price_source": raw.get("c5_price_source"),
                "c5_expected_net_price": raw.get("c5_expected_net_price"),
                "balance_discount": raw.get("balance_discount"),
                "expected_profit": raw.get("expected_profit"),
                "expected_roi": expected_roi,
                "buy_order_reference_roi": raw.get("buy_order_reference_roi"),
                "buy_order_reference_profit": raw.get("buy_order_reference_profit"),
                "buy_order_reference_status": raw.get("buy_order_reference_status"),
                "min_roi": raw.get("min_roi"),
                "manual_review_roi": raw.get("manual_review_roi"),
                "inventory_count": raw.get("inventory_count"),
                "tradable_count": raw.get("tradable_count"),
                "c5_recent_sold_net_price": raw.get("c5_recent_sold_net_price"),
                "c5_recent_sold_count": raw.get("c5_recent_sold_count"),
                "c5_current_sell_price": raw.get("c5_current_sell_price"),
                "c5_on_sale_count": raw.get("c5_on_sale_count"),
                "c5_purchase_max_price": raw.get("c5_purchase_max_price"),
                "c5_purchase_count": raw.get("c5_purchase_count"),
                "risk_status": str(raw.get("risk_status") or "unknown"),
                "risk_reason": raw.get("risk_reason"),
                "execution_status": str(raw.get("execution_status") or "watch_only"),
                "execution_reason": raw.get("execution_reason"),
                "raw_json": json.dumps(raw_payload, ensure_ascii=False),
            }
            normalized.append(row)
            active_names.add(market_hash_name)

        exit_reasons = exit_reasons or {}
        exit_observations = exit_observations or {}
        preserve_names = {
            str(value or "").strip()
            for value in (preserve_market_hash_names or ())
            if str(value or "").strip()
        }
        refresh_failures = {
            str(name or "").strip(): dict(failure)
            for name, failure in (preserve_refresh_failures or {}).items()
            if str(name or "").strip() and isinstance(failure, dict)
        }
        inserted = 0
        updated = 0
        exited = 0
        with self.conn:
            for row in normalized:
                existing = self.conn.execute(
                    "SELECT active FROM profit_trade_roi_watch WHERE market_hash_name = ?",
                    (row["market_hash_name"],),
                ).fetchone()
                event_type = "entered" if existing is None or not bool(existing["active"]) else "observed"
                self.conn.execute(
                    """
                    INSERT INTO profit_trade_roi_watch (
                        market_hash_name,
                        name_cn,
                        scan_id,
                        active,
                        steam_buy_price,
                        steam_seller_floor_count,
                        steam_buyer_max_price,
                        steam_buyer_max_count,
                        steam_spread_amount,
                        steam_spread_pct,
                        steam_orderbook_crossed,
                        steam_currency_id,
                        steam_orderbook_observed_at,
                        steam_price_source,
                        c5_listing_price,
                        c5_price_source,
                        c5_expected_net_price,
                        balance_discount,
                        expected_profit,
                        expected_roi,
                        buy_order_reference_roi,
                        buy_order_reference_profit,
                        buy_order_reference_status,
                        min_roi,
                        manual_review_roi,
                        inventory_count,
                        tradable_count,
                        c5_recent_sold_net_price,
                        c5_recent_sold_count,
                        c5_current_sell_price,
                        c5_on_sale_count,
                        c5_purchase_max_price,
                        c5_purchase_count,
                        risk_status,
                        risk_reason,
                        execution_status,
                        execution_reason,
                        first_seen_at,
                        last_observed_at,
                        updated_at,
                        exited_at,
                        exit_reason,
                        raw_json
                    ) VALUES (
                        :market_hash_name,
                        :name_cn,
                        :scan_id,
                        1,
                        :steam_buy_price,
                        :steam_seller_floor_count,
                        :steam_buyer_max_price,
                        :steam_buyer_max_count,
                        :steam_spread_amount,
                        :steam_spread_pct,
                        :steam_orderbook_crossed,
                        :steam_currency_id,
                        :steam_orderbook_observed_at,
                        :steam_price_source,
                        :c5_listing_price,
                        :c5_price_source,
                        :c5_expected_net_price,
                        :balance_discount,
                        :expected_profit,
                        :expected_roi,
                        :buy_order_reference_roi,
                        :buy_order_reference_profit,
                        :buy_order_reference_status,
                        :min_roi,
                        :manual_review_roi,
                        :inventory_count,
                        :tradable_count,
                        :c5_recent_sold_net_price,
                        :c5_recent_sold_count,
                        :c5_current_sell_price,
                        :c5_on_sale_count,
                        :c5_purchase_max_price,
                        :c5_purchase_count,
                        :risk_status,
                        :risk_reason,
                        :execution_status,
                        :execution_reason,
                        :timestamp,
                        :timestamp,
                        :timestamp,
                        NULL,
                        NULL,
                        :raw_json
                    )
                    ON CONFLICT(market_hash_name) DO UPDATE SET
                        name_cn = excluded.name_cn,
                        scan_id = excluded.scan_id,
                        active = 1,
                        steam_buy_price = excluded.steam_buy_price,
                        steam_seller_floor_count = excluded.steam_seller_floor_count,
                        steam_buyer_max_price = excluded.steam_buyer_max_price,
                        steam_buyer_max_count = excluded.steam_buyer_max_count,
                        steam_spread_amount = excluded.steam_spread_amount,
                        steam_spread_pct = excluded.steam_spread_pct,
                        steam_orderbook_crossed = excluded.steam_orderbook_crossed,
                        steam_currency_id = excluded.steam_currency_id,
                        steam_orderbook_observed_at = excluded.steam_orderbook_observed_at,
                        steam_price_source = excluded.steam_price_source,
                        c5_listing_price = excluded.c5_listing_price,
                        c5_price_source = excluded.c5_price_source,
                        c5_expected_net_price = excluded.c5_expected_net_price,
                        balance_discount = excluded.balance_discount,
                        expected_profit = excluded.expected_profit,
                        expected_roi = excluded.expected_roi,
                        buy_order_reference_roi = excluded.buy_order_reference_roi,
                        buy_order_reference_profit = excluded.buy_order_reference_profit,
                        buy_order_reference_status = excluded.buy_order_reference_status,
                        min_roi = excluded.min_roi,
                        manual_review_roi = excluded.manual_review_roi,
                        inventory_count = excluded.inventory_count,
                        tradable_count = excluded.tradable_count,
                        c5_recent_sold_net_price = excluded.c5_recent_sold_net_price,
                        c5_recent_sold_count = excluded.c5_recent_sold_count,
                        c5_current_sell_price = excluded.c5_current_sell_price,
                        c5_on_sale_count = excluded.c5_on_sale_count,
                        c5_purchase_max_price = excluded.c5_purchase_max_price,
                        c5_purchase_count = excluded.c5_purchase_count,
                        risk_status = excluded.risk_status,
                        risk_reason = excluded.risk_reason,
                        execution_status = excluded.execution_status,
                        execution_reason = excluded.execution_reason,
                        last_observed_at = excluded.last_observed_at,
                        updated_at = excluded.updated_at,
                        exited_at = NULL,
                        exit_reason = NULL,
                        raw_json = excluded.raw_json
                    """,
                    {**row, "scan_id": scan_id, "timestamp": timestamp},
                )
                self.conn.execute(
                    """
                    INSERT INTO profit_trade_roi_observations (
                        scan_id,
                        market_hash_name,
                        event_type,
                        observed_at,
                        steam_buy_price,
                        steam_seller_floor_count,
                        steam_buyer_max_price,
                        steam_buyer_max_count,
                        steam_spread_amount,
                        steam_spread_pct,
                        steam_orderbook_crossed,
                        steam_currency_id,
                        steam_orderbook_observed_at,
                        c5_listing_price,
                        c5_expected_net_price,
                        balance_discount,
                        expected_profit,
                        expected_roi,
                        buy_order_reference_roi,
                        buy_order_reference_profit,
                        buy_order_reference_status,
                        min_roi,
                        manual_review_roi,
                        inventory_count,
                        tradable_count,
                        risk_status,
                        risk_reason,
                        execution_status,
                        execution_reason,
                        raw_json
                    ) VALUES (
                        :scan_id,
                        :market_hash_name,
                        :event_type,
                        :timestamp,
                        :steam_buy_price,
                        :steam_seller_floor_count,
                        :steam_buyer_max_price,
                        :steam_buyer_max_count,
                        :steam_spread_amount,
                        :steam_spread_pct,
                        :steam_orderbook_crossed,
                        :steam_currency_id,
                        :steam_orderbook_observed_at,
                        :c5_listing_price,
                        :c5_expected_net_price,
                        :balance_discount,
                        :expected_profit,
                        :expected_roi,
                        :buy_order_reference_roi,
                        :buy_order_reference_profit,
                        :buy_order_reference_status,
                        :min_roi,
                        :manual_review_roi,
                        :inventory_count,
                        :tradable_count,
                        :risk_status,
                        :risk_reason,
                        :execution_status,
                        :execution_reason,
                        :raw_json
                    )
                    """,
                    {
                        **row,
                        "scan_id": scan_id,
                        "event_type": event_type,
                        "timestamp": timestamp,
                    },
                )
                if existing is None:
                    inserted += 1
                else:
                    updated += 1

            active_rows = self.conn.execute(
                "SELECT * FROM profit_trade_roi_watch WHERE active = 1"
            ).fetchall()
            for watch_row in active_rows:
                market_hash_name = str(watch_row["market_hash_name"])
                if market_hash_name in active_names:
                    continue
                if market_hash_name in preserve_names:
                    failure = refresh_failures.get(market_hash_name)
                    if failure is not None:
                        try:
                            preserved_raw = json.loads(
                                str(watch_row["raw_json"] or "{}")
                            )
                        except (TypeError, ValueError):
                            preserved_raw = {}
                        if not isinstance(preserved_raw, dict):
                            preserved_raw = {}
                        preserved_raw["latestRefresh"] = {
                            "status": "failed",
                            "scanId": scan_id,
                            "attemptedAt": timestamp,
                            "reason": str(
                                failure.get("reason") or "行情读取失败"
                            )[:1000],
                            "errorType": str(
                                failure.get("errorType") or "price_unavailable"
                            )[:100],
                            "targetBalanceDiscount": failure.get(
                                "targetBalanceDiscount"
                            ),
                        }
                        self.conn.execute(
                            """
                            UPDATE profit_trade_roi_watch
                            SET updated_at = ?, raw_json = ?
                            WHERE market_hash_name = ?
                            """,
                            (
                                timestamp,
                                json.dumps(preserved_raw, ensure_ascii=False),
                                market_hash_name,
                            ),
                        )
                    continue
                reason = str(
                    exit_reasons.get(market_hash_name)
                    or "not profitable or unavailable in the latest completed scan"
                )
                snapshot = exit_observations.get(market_hash_name) or {}
                snapshot_raw = snapshot.get("raw") if isinstance(snapshot.get("raw"), dict) else {}
                snapshot_orderbook = (
                    snapshot_raw.get("steamOrderbook")
                    if isinstance(snapshot_raw.get("steamOrderbook"), dict)
                    else {}
                )

                def exit_value(key: str, column: str) -> Any:
                    value = snapshot.get(key)
                    return watch_row[column] if value is None else value

                def exit_orderbook_value(key: str, column: str) -> Any:
                    value = snapshot_orderbook.get(key)
                    return watch_row[column] if value is None else value

                exit_values = {
                    "steam_buy_price": exit_value("steam_buy_price", "steam_buy_price"),
                    "steam_seller_floor_count": exit_orderbook_value(
                        "sellerFloorCount", "steam_seller_floor_count"
                    ),
                    "steam_buyer_max_price": exit_orderbook_value(
                        "buyerMaxPrice", "steam_buyer_max_price"
                    ),
                    "steam_buyer_max_count": exit_orderbook_value(
                        "buyerMaxCount", "steam_buyer_max_count"
                    ),
                    "steam_spread_amount": exit_orderbook_value(
                        "spreadAmount", "steam_spread_amount"
                    ),
                    "steam_spread_pct": exit_orderbook_value(
                        "spreadPct", "steam_spread_pct"
                    ),
                    "steam_orderbook_crossed": (
                        1 if snapshot_orderbook.get("crossed") is True
                        else 0 if snapshot_orderbook.get("crossed") is False
                        else watch_row["steam_orderbook_crossed"]
                    ),
                    "steam_currency_id": exit_orderbook_value(
                        "currencyId", "steam_currency_id"
                    ),
                    "steam_orderbook_observed_at": exit_orderbook_value(
                        "observedAt", "steam_orderbook_observed_at"
                    ),
                    "c5_listing_price": exit_value("c5_listing_price", "c5_listing_price"),
                    "c5_expected_net_price": exit_value(
                        "c5_expected_net_price", "c5_expected_net_price"
                    ),
                    "balance_discount": exit_value("balance_discount", "balance_discount"),
                    "expected_profit": exit_value("expected_profit", "expected_profit"),
                    "expected_roi": exit_value("expected_roi", "expected_roi"),
                    "buy_order_reference_roi": exit_value(
                        "buy_order_reference_roi", "buy_order_reference_roi"
                    ),
                    "buy_order_reference_profit": exit_value(
                        "buy_order_reference_profit", "buy_order_reference_profit"
                    ),
                    "buy_order_reference_status": exit_value(
                        "buy_order_reference_status", "buy_order_reference_status"
                    ),
                    "min_roi": exit_value("min_roi", "min_roi"),
                    "manual_review_roi": exit_value("manual_review_roi", "manual_review_roi"),
                    "inventory_count": exit_value("inventory_count", "inventory_count"),
                    "tradable_count": exit_value("tradable_count", "tradable_count"),
                    "risk_status": exit_value("risk_status", "risk_status"),
                    "risk_reason": exit_value("risk_reason", "risk_reason"),
                    "execution_status": exit_value("execution_status", "execution_status"),
                    "execution_reason": exit_value("execution_reason", "execution_reason"),
                    "raw_json": (
                        json.dumps(snapshot_raw, ensure_ascii=False)
                        if snapshot
                        else watch_row["raw_json"]
                    ),
                }
                self.conn.execute(
                    """
                    UPDATE profit_trade_roi_watch
                    SET active = 0,
                        steam_buy_price = ?,
                        steam_seller_floor_count = ?,
                        steam_buyer_max_price = ?,
                        steam_buyer_max_count = ?,
                        steam_spread_amount = ?,
                        steam_spread_pct = ?,
                        steam_orderbook_crossed = ?,
                        steam_currency_id = ?,
                        steam_orderbook_observed_at = ?,
                        c5_listing_price = ?,
                        c5_expected_net_price = ?,
                        balance_discount = ?,
                        expected_profit = ?,
                        expected_roi = ?,
                        buy_order_reference_roi = ?,
                        buy_order_reference_profit = ?,
                        buy_order_reference_status = ?,
                        min_roi = ?,
                        manual_review_roi = ?,
                        inventory_count = ?,
                        tradable_count = ?,
                        risk_status = ?,
                        risk_reason = ?,
                        execution_status = ?,
                        execution_reason = ?,
                        updated_at = ?,
                        exited_at = ?,
                        exit_reason = ?,
                        raw_json = ?
                    WHERE market_hash_name = ?
                    """,
                    (
                        exit_values["steam_buy_price"],
                        exit_values["steam_seller_floor_count"],
                        exit_values["steam_buyer_max_price"],
                        exit_values["steam_buyer_max_count"],
                        exit_values["steam_spread_amount"],
                        exit_values["steam_spread_pct"],
                        exit_values["steam_orderbook_crossed"],
                        exit_values["steam_currency_id"],
                        exit_values["steam_orderbook_observed_at"],
                        exit_values["c5_listing_price"],
                        exit_values["c5_expected_net_price"],
                        exit_values["balance_discount"],
                        exit_values["expected_profit"],
                        exit_values["expected_roi"],
                        exit_values["buy_order_reference_roi"],
                        exit_values["buy_order_reference_profit"],
                        exit_values["buy_order_reference_status"],
                        exit_values["min_roi"],
                        exit_values["manual_review_roi"],
                        exit_values["inventory_count"],
                        exit_values["tradable_count"],
                        exit_values["risk_status"],
                        exit_values["risk_reason"],
                        exit_values["execution_status"],
                        exit_values["execution_reason"],
                        timestamp,
                        timestamp,
                        reason,
                        exit_values["raw_json"],
                        market_hash_name,
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO profit_trade_roi_observations (
                        scan_id,
                        market_hash_name,
                        event_type,
                        observed_at,
                        steam_buy_price,
                        steam_seller_floor_count,
                        steam_buyer_max_price,
                        steam_buyer_max_count,
                        steam_spread_amount,
                        steam_spread_pct,
                        steam_orderbook_crossed,
                        steam_currency_id,
                        steam_orderbook_observed_at,
                        c5_listing_price,
                        c5_expected_net_price,
                        balance_discount,
                        expected_profit,
                        expected_roi,
                        buy_order_reference_roi,
                        buy_order_reference_profit,
                        buy_order_reference_status,
                        min_roi,
                        manual_review_roi,
                        inventory_count,
                        tradable_count,
                        risk_status,
                        risk_reason,
                        execution_status,
                        execution_reason,
                        exit_reason,
                        raw_json
                    ) VALUES (
                        :scan_id,
                        :market_hash_name,
                        'exited',
                        :timestamp,
                        :steam_buy_price,
                        :steam_seller_floor_count,
                        :steam_buyer_max_price,
                        :steam_buyer_max_count,
                        :steam_spread_amount,
                        :steam_spread_pct,
                        :steam_orderbook_crossed,
                        :steam_currency_id,
                        :steam_orderbook_observed_at,
                        :c5_listing_price,
                        :c5_expected_net_price,
                        :balance_discount,
                        :expected_profit,
                        :expected_roi,
                        :buy_order_reference_roi,
                        :buy_order_reference_profit,
                        :buy_order_reference_status,
                        :min_roi,
                        :manual_review_roi,
                        :inventory_count,
                        :tradable_count,
                        :risk_status,
                        :risk_reason,
                        :execution_status,
                        :execution_reason,
                        :exit_reason,
                        :raw_json
                    )
                    """,
                    {
                        **exit_values,
                        "scan_id": scan_id,
                        "market_hash_name": market_hash_name,
                        "timestamp": timestamp,
                        "exit_reason": reason,
                    },
                )
                exited += 1
        return {"inserted": inserted, "updated": updated, "exited": exited}

    def create_profit_trade_long_buy_order(
        self,
        *,
        market_hash_name: str,
        steam_account_id: str,
        steam_id: str | None,
        create_request_id: str,
        bid_price_cents: int,
        quantity: int,
        c5_price_batch: float | None,
        c5_expected_net_price: float | None,
        balance_discount: float | None,
        standard_roi: float | None,
        aggressive_roi: float | None,
        standard_safe_price_cents: int | None,
        aggressive_safe_price_cents: int | None,
        competitor_buy_price_cents: int | None,
        competitor_buy_status: str | None,
        worst_case_roi: float | None,
        source_scan_id: str | None,
        wallet_before: float | None,
        previous_bid_price_cents: int | None = None,
        previous_price_expires_at: str | None = None,
        replaces_order_id: int | None = None,
        note: dict[str, Any] | None = None,
    ) -> int:
        name = str(market_hash_name or "").strip()
        account_id = str(steam_account_id or "").strip()
        request_id = str(create_request_id or "").strip()
        price_cents = int(bid_price_cents)
        requested_quantity = int(quantity)
        if not name or not account_id or not request_id:
            raise ValueError(
                "market_hash_name, steam_account_id and create_request_id are required"
            )
        if price_cents <= 0 or requested_quantity <= 0:
            raise ValueError("bid_price_cents and quantity must be positive")
        now = utc_now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO profit_trade_long_buy_orders (
                    market_hash_name,
                    steam_account_id,
                    steam_id,
                    create_request_id,
                    state,
                    bid_price_cents,
                    quantity,
                    remaining_quantity,
                    c5_price_batch,
                    c5_expected_net_price,
                    balance_discount,
                    standard_roi,
                    aggressive_roi,
                    standard_safe_price_cents,
                    aggressive_safe_price_cents,
                    competitor_buy_price_cents,
                    competitor_buy_status,
                    worst_case_roi,
                    source_scan_id,
                    previous_bid_price_cents,
                    previous_price_expires_at,
                    wallet_before,
                    replaces_order_id,
                    note_json,
                    created_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, 'creating', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    name,
                    account_id,
                    str(steam_id or "").strip() or None,
                    request_id,
                    price_cents,
                    requested_quantity,
                    requested_quantity,
                    c5_price_batch,
                    c5_expected_net_price,
                    balance_discount,
                    standard_roi,
                    aggressive_roi,
                    standard_safe_price_cents,
                    aggressive_safe_price_cents,
                    competitor_buy_price_cents,
                    competitor_buy_status,
                    worst_case_roi,
                    str(source_scan_id or "").strip() or None,
                    previous_bid_price_cents,
                    previous_price_expires_at,
                    wallet_before,
                    replaces_order_id,
                    _json_object(note),
                    now,
                    now,
                ),
            )
            order_id = int(cursor.lastrowid)
            self.conn.execute(
                """
                INSERT INTO profit_trade_long_buy_events (
                    long_buy_order_id, event_type, state_from, state_to,
                    reason, context_json, created_at
                ) VALUES (?, 'create_intent_recorded', NULL, 'creating', ?, ?, ?)
                """,
                (
                    order_id,
                    "local create intent recorded before Steam request",
                    _json_object(note),
                    now,
                ),
            )
            if replaces_order_id is not None:
                self.conn.execute(
                    """
                    UPDATE profit_trade_long_buy_orders
                    SET replaced_by_order_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (order_id, now, int(replaces_order_id)),
                )
        return order_id

    def get_profit_trade_long_buy_order(
        self,
        order_id: int,
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM profit_trade_long_buy_orders WHERE id = ?",
            (int(order_id),),
        ).fetchone()

    def get_profit_trade_long_buy_order_by_remote_id(
        self,
        *,
        steam_account_id: str,
        buy_order_id: str,
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM profit_trade_long_buy_orders
            WHERE steam_account_id = ? AND buy_order_id = ?
            LIMIT 1
            """,
            (
                str(steam_account_id or "").strip(),
                str(buy_order_id or "").strip(),
            ),
        ).fetchone()

    def get_live_profit_trade_long_buy_order_for_market(
        self,
        market_hash_name: str,
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM profit_trade_long_buy_orders
            WHERE market_hash_name = ?
              AND state IN (
                'creating', 'active', 'partial', 'cancel_pending',
                'terminal_uncertain'
              )
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(market_hash_name or "").strip(),),
        ).fetchone()

    def list_profit_trade_long_buy_orders(
        self,
        *,
        states: Iterable[str] | None = None,
        steam_account_id: str | None = None,
        market_hash_name: str | None = None,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[Any] = []
        normalized_states = [
            str(value or "").strip()
            for value in list(states or [])
            if str(value or "").strip()
        ]
        if normalized_states:
            placeholders = ",".join("?" for _ in normalized_states)
            where.append(f"state IN ({placeholders})")
            params.extend(normalized_states)
        if steam_account_id is not None:
            where.append("steam_account_id = ?")
            params.append(str(steam_account_id or "").strip())
        if market_hash_name is not None:
            where.append("market_hash_name = ?")
            params.append(str(market_hash_name or "").strip())
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        return self.conn.execute(
            f"""
            SELECT *
            FROM profit_trade_long_buy_orders
            {where_sql}
            ORDER BY id ASC
            LIMIT ?
            """,
            (*params, max(1, int(limit))),
        ).fetchall()

    def count_live_profit_trade_long_buy_orders(self) -> int:
        return int(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM profit_trade_long_buy_orders
                WHERE state IN (
                    'creating', 'active', 'partial', 'cancel_pending',
                    'terminal_uncertain'
                )
                """
            ).fetchone()[0]
        )

    def update_profit_trade_long_buy_order(
        self,
        order_id: int,
        *,
        event_type: str,
        reason: str | None = None,
        context: dict[str, Any] | None = None,
        **fields: Any,
    ) -> sqlite3.Row:
        allowed = {
            "buy_order_id",
            "state",
            "bid_price_cents",
            "quantity",
            "filled_quantity",
            "remaining_quantity",
            "c5_price_batch",
            "c5_expected_net_price",
            "balance_discount",
            "standard_roi",
            "aggressive_roi",
            "standard_safe_price_cents",
            "aggressive_safe_price_cents",
            "competitor_buy_price_cents",
            "competitor_buy_status",
            "worst_case_roi",
            "source_scan_id",
            "previous_bid_price_cents",
            "previous_price_expires_at",
            "wallet_before",
            "replaces_order_id",
            "replaced_by_order_id",
            "terminal_reason",
            "note_json",
            "last_checked_at",
            "last_filled_at",
            "completed_at",
        }
        unsupported = sorted(set(fields) - allowed)
        if unsupported:
            raise ValueError(
                f"unsupported long-buy field(s): {', '.join(unsupported)}"
            )
        current = self.get_profit_trade_long_buy_order(int(order_id))
        if current is None:
            raise LookupError(f"profit trade long buy order not found: {order_id}")
        old_state = str(current["state"] or "")
        new_state = str(fields.get("state") or old_state)
        if "note_json" in fields and isinstance(fields["note_json"], dict):
            fields["note_json"] = _json_object(fields["note_json"])
        now = utc_now_iso()
        parts = [f"{name} = ?" for name in fields]
        params = list(fields.values())
        parts.append("updated_at = ?")
        params.append(now)
        params.append(int(order_id))
        with self.conn:
            self.conn.execute(
                f"""
                UPDATE profit_trade_long_buy_orders
                SET {', '.join(parts)}
                WHERE id = ?
                """,
                tuple(params),
            )
            self.conn.execute(
                """
                INSERT INTO profit_trade_long_buy_events (
                    long_buy_order_id, event_type, state_from, state_to,
                    reason, context_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(order_id),
                    str(event_type or "updated"),
                    old_state,
                    new_state,
                    reason,
                    _json_object(context),
                    now,
                ),
            )
        updated = self.get_profit_trade_long_buy_order(int(order_id))
        assert updated is not None
        return updated

    def list_profit_trade_long_buy_events(
        self,
        order_id: int,
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT *
            FROM profit_trade_long_buy_events
            WHERE long_buy_order_id = ?
            ORDER BY created_at, id
            """,
            (int(order_id),),
        ).fetchall()

    def record_profit_trade_long_buy_fill(
        self,
        *,
        long_buy_order_id: int,
        steam_account_id: str,
        purchase_id: str,
        listing_id: str | None,
        market_hash_name: str,
        paid_total_cents: int,
        asset_id: str | None,
        new_asset_id: str | None,
        purchased_at: str | None,
        evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized_purchase_id = str(purchase_id or "").strip()
        if not normalized_purchase_id:
            raise ValueError("purchase_id is required")
        if int(paid_total_cents) <= 0:
            raise ValueError("paid_total_cents must be positive")
        now = utc_now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO profit_trade_long_buy_fills (
                    long_buy_order_id,
                    steam_account_id,
                    purchase_id,
                    listing_id,
                    market_hash_name,
                    paid_total_cents,
                    asset_id,
                    new_asset_id,
                    purchased_at,
                    state,
                    evidence_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    int(long_buy_order_id),
                    str(steam_account_id or "").strip(),
                    normalized_purchase_id,
                    str(listing_id or "").strip() or None,
                    str(market_hash_name or "").strip(),
                    int(paid_total_cents),
                    str(asset_id or "").strip() or None,
                    str(new_asset_id or "").strip() or None,
                    purchased_at,
                    _json_object(evidence),
                    now,
                    now,
                ),
            )
            inserted = cursor.rowcount > 0
            row = self.conn.execute(
                """
                SELECT *
                FROM profit_trade_long_buy_fills
                WHERE steam_account_id = ? AND purchase_id = ?
                """,
                (
                    str(steam_account_id or "").strip(),
                    normalized_purchase_id,
                ),
            ).fetchone()
        assert row is not None
        return {"id": int(row["id"]), "inserted": inserted, "row": row}

    def list_pending_profit_trade_long_buy_fills(
        self,
        *,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT *
            FROM profit_trade_long_buy_fills
            WHERE state = 'pending'
            ORDER BY created_at, id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()

    def get_profit_trade_long_buy_fill(
        self,
        fill_id: int,
    ) -> sqlite3.Row | None:
        """Return one long-buy fill for an idempotent, receipt-led handoff."""

        return self.conn.execute(
            "SELECT * FROM profit_trade_long_buy_fills WHERE id = ?",
            (int(fill_id),),
        ).fetchone()

    def list_profit_trade_long_buy_fills(
        self,
        *,
        long_buy_order_id: int | None = None,
        states: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[Any] = []
        if long_buy_order_id is not None:
            where.append("long_buy_order_id = ?")
            params.append(int(long_buy_order_id))
        normalized_states = [
            str(value or "").strip()
            for value in list(states or [])
            if str(value or "").strip()
        ]
        if normalized_states:
            placeholders = ",".join("?" for _ in normalized_states)
            where.append(f"state IN ({placeholders})")
            params.extend(normalized_states)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        return self.conn.execute(
            f"""
            SELECT *
            FROM profit_trade_long_buy_fills
            {where_sql}
            ORDER BY purchased_at, created_at, id
            LIMIT ?
            """,
            (*params, max(1, int(limit))),
        ).fetchall()

    def count_profit_trade_long_buy_fills(
        self,
        long_buy_order_id: int,
    ) -> int:
        return int(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM profit_trade_long_buy_fills
                WHERE long_buy_order_id = ?
                """,
                (int(long_buy_order_id),),
            ).fetchone()[0]
        )

    def update_profit_trade_long_buy_fill(
        self,
        fill_id: int,
        *,
        state: str,
        profit_trade_id: int | None = None,
    ) -> sqlite3.Row:
        normalized_state = str(state or "").strip()
        if normalized_state not in {"pending", "processed", "manual_required"}:
            raise ValueError("invalid long-buy fill state")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE profit_trade_long_buy_fills
                SET state = ?,
                    profit_trade_id = COALESCE(?, profit_trade_id),
                    processed_at = CASE
                        WHEN ? IN ('processed', 'manual_required') THEN ?
                        ELSE processed_at
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized_state,
                    profit_trade_id,
                    normalized_state,
                    now,
                    now,
                    int(fill_id),
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM profit_trade_long_buy_fills WHERE id = ?",
            (int(fill_id),),
        ).fetchone()
        if row is None:
            raise LookupError(f"profit trade long buy fill not found: {fill_id}")
        return row

    def list_profit_trade_roi_watch(
        self,
        *,
        active: bool | None = True,
        keyword: str | None = None,
        execution_status: str | None = None,
        roi_sign: str | None = None,
        sort: str = "roi_desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        # The headline totals intentionally ignore the current search, status
        # filter and page.  They describe the complete active inventory pool,
        # not just whatever happens to be visible in one paginated response.
        summary_row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS active_item_count,
                COALESCE(SUM(CASE
                    WHEN COALESCE(tradable_count, 0) > 0 THEN tradable_count
                    ELSE 0
                END), 0) AS tradable_quantity,
                COALESCE(SUM(CASE
                    WHEN COALESCE(tradable_count, 0) > 0
                    THEN COALESCE(expected_profit, 0) * tradable_count
                    ELSE 0
                END), 0) AS current_expected_profit_total,
                COALESCE(SUM(CASE
                    WHEN buy_order_reference_status = 'valid'
                         AND COALESCE(tradable_count, 0) > 0
                    THEN COALESCE(buy_order_reference_profit, 0) * tradable_count
                    ELSE 0
                END), 0) AS buy_order_reference_profit_total,
                COALESCE(SUM(CASE
                    WHEN buy_order_reference_roi IS NOT NULL
                         AND buy_order_reference_profit IS NOT NULL
                    THEN 1
                    ELSE 0
                END), 0) AS buy_order_reference_covered_items,
                COALESCE(SUM(CASE
                    WHEN buy_order_reference_status = 'valid' THEN 1
                    ELSE 0
                END), 0) AS buy_order_reference_eligible_items
            FROM profit_trade_roi_watch
            WHERE active = 1
            """
        ).fetchone()
        summary = {
            "activeItemCount": int(summary_row["active_item_count"] or 0),
            "tradableQuantity": int(summary_row["tradable_quantity"] or 0),
            "currentExpectedProfitTotal": float(summary_row["current_expected_profit_total"] or 0),
            "buyOrderReferenceProfitTotal": float(
                summary_row["buy_order_reference_profit_total"] or 0
            ),
            "buyOrderReferenceCoveredItems": int(
                summary_row["buy_order_reference_covered_items"] or 0
            ),
            "buyOrderReferenceEligibleItems": int(
                summary_row["buy_order_reference_eligible_items"] or 0
            ),
        }
        where: list[str] = []
        params: list[Any] = []
        if active is not None:
            where.append("active = ?")
            params.append(1 if active else 0)
        if keyword:
            where.append("(market_hash_name LIKE ? OR COALESCE(name_cn, '') LIKE ?)")
            pattern = f"%{keyword.strip()}%"
            params.extend([pattern, pattern])
        if execution_status:
            where.append("execution_status = ?")
            params.append(execution_status)
        if roi_sign == "positive":
            where.append("expected_roi > 0")
        elif roi_sign == "negative":
            where.append("expected_roi < 0")
        elif roi_sign not in (None, "", "all"):
            raise ValueError("roiSign must be positive, negative, or all")
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM profit_trade_roi_watch{where_sql}",
                tuple(params),
            ).fetchone()[0]
        )
        order_by = {
            "roi_asc": "expected_roi ASC, last_observed_at DESC",
            "updated_desc": "last_observed_at DESC, market_hash_name ASC",
            "price_desc": "c5_listing_price DESC, expected_roi DESC",
        }.get(sort, "expected_roi DESC, last_observed_at DESC")
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM profit_trade_roi_watch
            {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return {
            "items": [self._profit_trade_roi_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "summary": summary,
        }

    def list_profit_trade_roi_history(
        self,
        market_hash_name: str,
        *,
        from_time: str | None = None,
        to_time: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = min(500, max(1, int(page_size)))
        where = ["market_hash_name = ?"]
        params: list[Any] = [market_hash_name]
        if from_time:
            where.append("julianday(observed_at) >= julianday(?)")
            params.append(from_time)
        if to_time:
            where.append("julianday(observed_at) <= julianday(?)")
            params.append(to_time)
        where_sql = " AND ".join(where)
        stats_row = self.conn.execute(
            f"""
            SELECT
                COUNT(expected_roi) AS valid_observation_count,
                MAX(expected_roi) AS highest_roi,
                AVG(expected_roi) AS average_roi,
                MIN(balance_discount) AS roi_basis_min,
                MAX(balance_discount) AS roi_basis_max
            FROM profit_trade_roi_observations
            WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()
        roi_basis_min = stats_row["roi_basis_min"]
        roi_basis_max = stats_row["roi_basis_max"]
        stats = {
            "highestRoi": stats_row["highest_roi"],
            "averageRoi": stats_row["average_roi"],
            "roiBasis": (
                roi_basis_min
                if roi_basis_min is not None and roi_basis_min == roi_basis_max
                else None
            ),
            "roiBasisMin": roi_basis_min,
            "roiBasisMax": roi_basis_max,
            "validObservationCount": int(stats_row["valid_observation_count"] or 0),
        }
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM profit_trade_roi_observations WHERE {where_sql}",
                tuple(params),
            ).fetchone()[0]
        )
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM profit_trade_roi_observations
            WHERE {where_sql}
            ORDER BY observed_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        trend_rows = self.conn.execute(
            f"""
            SELECT observed_at, expected_roi, buy_order_reference_roi, balance_discount
            FROM profit_trade_roi_observations
            WHERE {where_sql} AND expected_roi IS NOT NULL
            ORDER BY observed_at ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
        return {
            "items": [self._profit_trade_roi_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "stats": stats,
            "trend": _profit_trade_history_trend(trend_rows),
        }

    def list_profit_trade_state_events(self, trade_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM profit_trade_state_events
            WHERE trade_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (trade_id,),
        ).fetchall()
        if not rows:
            trade = self.get_profit_trade(trade_id)
            if trade is None:
                return []
            return [
                {
                    "id": None,
                    "tradeId": trade_id,
                    "eventType": "historical_snapshot",
                    "statusFrom": None,
                    "statusTo": trade["status"],
                    "stepKeyFrom": None,
                    "stepKeyTo": trade["step_key"],
                    "stepIndexFrom": None,
                    "stepIndexTo": int(trade["step_index"] or 0),
                    "reason": trade["error"],
                    "logEventId": None,
                    "context": {},
                    "createdAt": trade["updated_at"],
                    "isSnapshot": True,
                }
            ]
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                context = json.loads(str(row["context_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                context = {}
            result.append(
                {
                    "id": int(row["id"]),
                    "tradeId": int(row["trade_id"]),
                    "eventType": row["event_type"],
                    "statusFrom": row["status_from"],
                    "statusTo": row["status_to"],
                    "stepKeyFrom": row["step_key_from"],
                    "stepKeyTo": row["step_key_to"],
                    "stepIndexFrom": row["step_index_from"],
                    "stepIndexTo": int(row["step_index_to"]),
                    "reason": row["reason"],
                    "logEventId": row["log_event_id"],
                    "context": context if isinstance(context, dict) else {},
                    "createdAt": row["created_at"],
                    "isSnapshot": False,
                }
            )
        return result

    def list_profit_trade_interruptions(
        self,
        *,
        statuses: tuple[str, ...] = ("cancelled", "failed", "manual_required"),
        step_key: str | None = None,
        acknowledged: str = "exclude",
        keyword: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        clean_statuses = tuple(str(value).strip() for value in statuses if str(value).strip())
        if not clean_statuses:
            return {"items": [], "total": 0, "page": page, "pageSize": page_size}
        where = [f"t.status IN ({', '.join('?' for _ in clean_statuses)})"]
        params: list[Any] = list(clean_statuses)
        if step_key:
            where.append("t.step_key = ?")
            params.append(step_key)
        if acknowledged == "only":
            where.append("COALESCE(a.acknowledged, 0) = 1")
        elif acknowledged == "exclude":
            where.append("COALESCE(a.acknowledged, 0) = 0")
        if keyword:
            pattern = f"%{keyword.strip()}%"
            where.append(
                "(t.trade_no LIKE ? OR t.market_hash_name LIKE ? "
                "OR COALESCE(i.name_cn, '') LIKE ? OR COALESCE(t.error, '') LIKE ? "
                "OR COALESCE(t.note, '') LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern, pattern])
        interrupted_time = "COALESCE(t.completed_at, t.updated_at)"
        if from_time:
            where.append(f"julianday({interrupted_time}) >= julianday(?)")
            params.append(from_time)
        if to_time:
            where.append(f"julianday({interrupted_time}) <= julianday(?)")
            params.append(to_time)
        where_sql = " AND ".join(where)
        join_sql = (
            "LEFT JOIN profit_trade_acknowledgements a ON a.trade_id = t.id "
            "LEFT JOIN items i ON i.market_hash_name = t.market_hash_name"
        )
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM profit_trades t {join_sql} WHERE {where_sql}",
                tuple(params),
            ).fetchone()[0]
        )
        rows = self.conn.execute(
            f"""
            SELECT
                t.*,
                COALESCE(a.acknowledged, 0) AS acknowledged,
                a.reason AS acknowledgement_reason,
                a.acknowledged_at,
                a.restored_at,
                a.updated_at AS acknowledgement_updated_at
            FROM profit_trades t
            {join_sql}
            WHERE {where_sql}
            ORDER BY {interrupted_time} DESC, t.id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }

    def get_profit_trade_interruption_summary(
        self,
        *,
        statuses: tuple[str, ...] = ("cancelled", "failed", "manual_required"),
        acknowledged: str = "exclude",
        keyword: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> dict[str, Any]:
        clean_statuses = tuple(str(value).strip() for value in statuses if str(value).strip())
        if not clean_statuses:
            return {"total": 0, "stepCounts": []}
        where = [f"t.status IN ({', '.join('?' for _ in clean_statuses)})"]
        params: list[Any] = list(clean_statuses)
        if acknowledged == "only":
            where.append("COALESCE(a.acknowledged, 0) = 1")
        elif acknowledged == "exclude":
            where.append("COALESCE(a.acknowledged, 0) = 0")
        if keyword:
            pattern = f"%{keyword.strip()}%"
            where.append(
                "(t.trade_no LIKE ? OR t.market_hash_name LIKE ? "
                "OR COALESCE(i.name_cn, '') LIKE ? OR COALESCE(t.error, '') LIKE ? "
                "OR COALESCE(t.note, '') LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern, pattern])
        interrupted_time = "COALESCE(t.completed_at, t.updated_at)"
        if from_time:
            where.append(f"julianday({interrupted_time}) >= julianday(?)")
            params.append(from_time)
        if to_time:
            where.append(f"julianday({interrupted_time}) <= julianday(?)")
            params.append(to_time)
        rows = self.conn.execute(
            f"""
            SELECT t.step_key, t.step_index, COUNT(*) AS count
            FROM profit_trades t
            LEFT JOIN profit_trade_acknowledgements a ON a.trade_id = t.id
            LEFT JOIN items i ON i.market_hash_name = t.market_hash_name
            WHERE {' AND '.join(where)}
            GROUP BY t.step_key, t.step_index
            ORDER BY t.step_index ASC, t.step_key ASC
            """,
            tuple(params),
        ).fetchall()
        step_counts = [
            {
                "stepKey": row["step_key"],
                "stepIndex": int(row["step_index"] or 0),
                "count": int(row["count"]),
            }
            for row in rows
        ]
        return {"total": sum(item["count"] for item in step_counts), "stepCounts": step_counts}

    def set_profit_trade_acknowledgement(
        self,
        trade_id: int,
        *,
        acknowledged: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if self.get_profit_trade(trade_id) is None:
            raise ValueError(f"profit trade not found: {trade_id}")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO profit_trade_acknowledgements (
                    trade_id,
                    acknowledged,
                    reason,
                    acknowledged_at,
                    restored_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    acknowledged = excluded.acknowledged,
                    reason = excluded.reason,
                    acknowledged_at = excluded.acknowledged_at,
                    restored_at = excluded.restored_at,
                    updated_at = excluded.updated_at
                """,
                (
                    trade_id,
                    1 if acknowledged else 0,
                    reason,
                    now if acknowledged else None,
                    None if acknowledged else now,
                    now,
                ),
            )
        trade = self.get_profit_trade(trade_id)
        _emit_profit_trade_local_event(
            component="profit_trade_interruptions",
            operation="acknowledged" if acknowledged else "acknowledgement_restored",
            message=(
                "Profit Trade interruption acknowledged"
                if acknowledged
                else "Profit Trade interruption acknowledgement restored"
            ),
            trade_id=trade_id,
            trade_no=str(trade["trade_no"] or "") if trade is not None else None,
            market_hash_name=str(trade["market_hash_name"] or "") if trade is not None else None,
            safe_context={"acknowledged": bool(acknowledged), "reason": reason},
        )
        return {
            "tradeId": trade_id,
            "acknowledged": bool(acknowledged),
            "reason": reason,
            "acknowledgedAt": now if acknowledged else None,
            "restoredAt": None if acknowledged else now,
            "updatedAt": now,
        }

    # ------------------------------------------------------------------
    # Strategy evaluations
    # ------------------------------------------------------------------

    def save_strategy_evaluation(
        self,
        *,
        market_hash_name: str,
        rebuy_price: float | None,
        steam_sell_price: float | None,
        steam_after_tax_price: float | None,
        listing_ratio: float | None,
        transfer_real_ratio: float | None,
        recommended_strategy: str | None,
        inventory_count: int | None,
        tradable_count: int | None,
        config_json: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO strategy_evaluations (
                market_hash_name, evaluated_at,
                rebuy_price, steam_sell_price, steam_after_tax_price,
                listing_ratio, transfer_real_ratio, recommended_strategy,
                inventory_count, tradable_count, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_hash_name,
                utc_now_iso(),
                rebuy_price,
                steam_sell_price,
                steam_after_tax_price,
                listing_ratio,
                transfer_real_ratio,
                recommended_strategy,
                inventory_count,
                tradable_count,
                config_json,
            ),
        )
        self.conn.commit()

    def list_latest_evaluations(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM strategy_evaluations
            ORDER BY evaluated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    # ------------------------------------------------------------------
    # Guadao case ratio monitor snapshots
    # ------------------------------------------------------------------

    def save_guadao_case_ratio_snapshots(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO guadao_case_ratio_snapshots (
                market_hash_name,
                observed_at,
                name_cn,
                c5_sell_price,
                c5_sell_count,
                steam_list_price,
                steam_wall_price,
                steam_after_tax_price,
                listing_ratio,
                c5_price_source,
                steam_price_source,
                status,
                error,
                raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["market_hash_name"],
                    row["observed_at"],
                    row.get("name_cn"),
                    row.get("c5_sell_price"),
                    row.get("c5_sell_count"),
                    row.get("steam_list_price"),
                    row.get("steam_wall_price"),
                    row.get("steam_after_tax_price"),
                    row.get("listing_ratio"),
                    row.get("c5_price_source"),
                    row.get("steam_price_source"),
                    row.get("status") or "ok",
                    row.get("error"),
                    json.dumps(row.get("raw_json") or {}, ensure_ascii=False),
                )
                for row in rows
            ],
        )
        self.conn.commit()
        return len(rows)

    def list_guadao_case_ratio_snapshots(
        self,
        *,
        start_utc: str,
        end_utc: str,
        market_hash_name: str | None = None,
    ) -> list[sqlite3.Row]:
        params: list[Any] = [start_utc, end_utc]
        market_filter = ""
        if market_hash_name:
            market_filter = " AND market_hash_name = ?"
            params.append(market_hash_name)
        return self.conn.execute(
            f"""
            SELECT *
            FROM guadao_case_ratio_snapshots
            WHERE observed_at >= ?
              AND observed_at <= ?
              {market_filter}
            ORDER BY market_hash_name ASC, observed_at ASC, id ASC
            """,
            tuple(params),
        ).fetchall()

    def latest_guadao_case_ratio_snapshot_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM guadao_case_ratio_snapshots").fetchone()
        return int(row["count"] if row is not None else 0)

    # ------------------------------------------------------------------
    # Guadao case ratio monitor runtime
    # ------------------------------------------------------------------

    def get_case_monitor_runtime_state(self, runtime_key: str = "case_monitor") -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM case_monitor_runtime_state WHERE runtime_key = ?",
            (str(runtime_key or "case_monitor").strip(),),
        ).fetchone()

    def upsert_case_monitor_runtime_state(
        self,
        *,
        runtime_key: str = "case_monitor",
        enabled: bool,
        interval_minutes: float,
        runtime_status: str,
        current_job_id: str | None = None,
        next_run_at: str | datetime | None = None,
        last_collection_at: str | datetime | None = None,
        last_report_at: str | datetime | None = None,
        last_error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        key = str(runtime_key or "case_monitor").strip()
        status = str(runtime_status or "").strip()
        interval = float(interval_minutes)
        if not key:
            raise ValueError("runtime_key is required")
        if not status:
            raise ValueError("runtime_status is required")
        if interval <= 0:
            raise ValueError("interval_minutes must be positive")
        now = utc_now_iso()

        def optional_utc(value: str | datetime | None) -> str | None:
            return _utc_iso(value) if value is not None else None

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO case_monitor_runtime_state (
                    runtime_key, enabled, interval_minutes, runtime_status,
                    current_job_id, next_run_at, last_collection_at,
                    last_report_at, last_error, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runtime_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    interval_minutes = excluded.interval_minutes,
                    runtime_status = excluded.runtime_status,
                    current_job_id = excluded.current_job_id,
                    next_run_at = excluded.next_run_at,
                    last_collection_at = excluded.last_collection_at,
                    last_report_at = excluded.last_report_at,
                    last_error = excluded.last_error,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    1 if enabled else 0,
                    interval,
                    status,
                    str(current_job_id).strip() if current_job_id else None,
                    optional_utc(next_run_at),
                    optional_utc(last_collection_at),
                    optional_utc(last_report_at),
                    str(last_error) if last_error else None,
                    _json_object(payload),
                    now,
                    now,
                ),
            )
        row = self.get_case_monitor_runtime_state(key)
        if row is None:
            raise RuntimeError("case monitor runtime state was not persisted")
        return row

    def interrupt_case_monitor_jobs(self, reason: str) -> int:
        now = utc_now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE case_monitor_jobs
                SET status = 'interrupted',
                    error = ?,
                    message = ?,
                    finished_at = ?,
                    updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (str(reason), str(reason), now, now),
            )
        return int(cursor.rowcount or 0)

    def create_case_monitor_job_if_idle(
        self,
        *,
        job_type: str,
        trigger_source: str,
        parameters: dict[str, Any] | None = None,
        runtime_key: str = "case_monitor",
        start_immediately: bool = False,
    ) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        normalized_type = str(job_type or "").strip()
        normalized_trigger = str(trigger_source or "").strip()
        if normalized_type not in {"collect", "report"}:
            raise ValueError("job_type must be collect or report")
        if not normalized_trigger:
            raise ValueError("trigger_source is required")
        now = utc_now_iso()
        job_id = f"case-{normalized_type}-{uuid.uuid4().hex[:16]}"
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            busy = self.conn.execute(
                """
                SELECT *
                FROM case_monitor_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY requested_at ASC, job_id ASC
                LIMIT 1
                """
            ).fetchone()
            if busy is not None:
                self.conn.rollback()
                return None, busy
            self.conn.execute(
                """
                INSERT INTO case_monitor_jobs (
                    job_id, job_type, trigger_source, status,
                    progress_current, progress_total, message,
                    parameters_json, result_json, requested_at,
                    started_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, ?, ?, '{}', ?, ?, ?)
                """,
                (
                    job_id,
                    normalized_type,
                    normalized_trigger,
                    "running" if start_immediately else "queued",
                    "任务执行中" if start_immediately else "等待后台执行",
                    _json_object(parameters),
                    now,
                    now if start_immediately else None,
                    now,
                ),
            )
            self.conn.execute(
                """
                UPDATE case_monitor_runtime_state
                SET runtime_status = ?,
                    current_job_id = ?,
                    last_error = NULL,
                    updated_at = ?
                WHERE runtime_key = ?
                """,
                (
                    "collecting" if normalized_type == "collect" else "reporting",
                    job_id,
                    now,
                    str(runtime_key or "case_monitor").strip(),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_case_monitor_job(job_id), None

    def get_case_monitor_job(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM case_monitor_jobs WHERE job_id = ?",
            (str(job_id or "").strip(),),
        ).fetchone()

    def latest_case_monitor_job(self, job_type: str | None = None) -> sqlite3.Row | None:
        params: tuple[Any, ...] = ()
        where = ""
        if job_type:
            where = "WHERE job_type = ?"
            params = (str(job_type).strip(),)
        return self.conn.execute(
            f"""
            SELECT *
            FROM case_monitor_jobs
            {where}
            ORDER BY requested_at DESC, job_id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()

    def claim_next_case_monitor_job(self) -> sqlite3.Row | None:
        now = utc_now_iso()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                """
                SELECT *
                FROM case_monitor_jobs
                WHERE status = 'queued'
                ORDER BY requested_at ASC, job_id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.conn.rollback()
                return None
            self.conn.execute(
                """
                UPDATE case_monitor_jobs
                SET status = 'running',
                    started_at = COALESCE(started_at, ?),
                    message = ?,
                    updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (now, "任务执行中", now, row["job_id"]),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_case_monitor_job(str(row["job_id"]))

    def update_case_monitor_job_progress(
        self,
        job_id: str,
        *,
        current: int,
        total: int,
        message: str | None = None,
        partial_result: dict[str, Any] | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE case_monitor_jobs
                SET progress_current = ?,
                    progress_total = ?,
                    message = COALESCE(?, message),
                    result_json = COALESCE(?, result_json),
                    updated_at = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (
                    max(0, int(current)),
                    max(0, int(total)),
                    str(message) if message else None,
                    _json_object(partial_result) if partial_result is not None else None,
                    utc_now_iso(),
                    str(job_id or "").strip(),
                ),
            )

    def finish_case_monitor_job(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        status: str | None = None,
    ) -> sqlite3.Row | None:
        final_status = str(status or ("failed" if error else "completed")).strip()
        if final_status not in {"completed", "failed", "interrupted"}:
            raise ValueError("invalid final case monitor job status")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE case_monitor_jobs
                SET status = ?,
                    result_json = ?,
                    error = ?,
                    message = ?,
                    finished_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    final_status,
                    _json_object(result),
                    str(error) if error else None,
                    (
                        str(error)
                        if error
                        else "任务已完成"
                        if final_status == "completed"
                        else "任务已中断"
                    ),
                    now,
                    now,
                    str(job_id or "").strip(),
                ),
            )
        return self.get_case_monitor_job(job_id)

    # ------------------------------------------------------------------
    # Persistent executor runtime state
    # ------------------------------------------------------------------

    def get_executor_runtime_state(self, executor_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM executor_runtime_state WHERE executor_key = ?",
            (str(executor_key).strip(),),
        ).fetchone()

    def list_executor_runtime_states(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM executor_runtime_state ORDER BY executor_key"
        ).fetchall()

    def upsert_executor_runtime_state(
        self,
        executor_key: str,
        *,
        enabled: bool,
        runtime_status: str,
        migration_hold: bool,
        gate_reason: str | None = None,
        heartbeat_at: str | datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        key = str(executor_key or "").strip()
        status = str(runtime_status or "").strip()
        if not key:
            raise ValueError("executor_key is required")
        if not status:
            raise ValueError("runtime_status is required")
        now = utc_now_iso()
        heartbeat = _utc_iso(heartbeat_at) if heartbeat_at is not None else None
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO executor_runtime_state (
                    executor_key, enabled, runtime_status, migration_hold,
                    gate_reason, heartbeat_at, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(executor_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    runtime_status = excluded.runtime_status,
                    migration_hold = excluded.migration_hold,
                    gate_reason = excluded.gate_reason,
                    heartbeat_at = excluded.heartbeat_at,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    1 if enabled else 0,
                    status,
                    1 if migration_hold else 0,
                    gate_reason,
                    heartbeat,
                    _json_object(payload),
                    now,
                    now,
                ),
            )
        row = self.get_executor_runtime_state(key)
        if row is None:  # pragma: no cover - guarded by the successful insert
            raise RuntimeError(f"executor runtime state was not persisted: {key}")
        return row

    # ------------------------------------------------------------------
    # Profit Trade selection watch (research-only; never a trade queue)
    # ------------------------------------------------------------------

    @staticmethod
    def _profit_trade_selection_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Public projection for the separate all-market research watch.

        Keep this mapping independent from the inventory ROI mapper above: an
        all-market item intentionally has no usable C5 asset and must never be
        projected as executable merely because its price/ROI happens to look
        attractive.
        """

        keys = set(row.keys())
        try:
            raw_payload = json.loads(str(row["raw_json"] or "{}")) if "raw_json" in keys else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_payload = {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        try:
            catalog_payload = (
                json.loads(str(row["catalog_raw_json"] or "{}"))
                if "catalog_raw_json" in keys
                else {}
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            catalog_payload = {}
        if not isinstance(catalog_payload, dict):
            catalog_payload = {}
        raw_orderbook = (
            raw_payload.get("steamOrderbook")
            if isinstance(raw_payload.get("steamOrderbook"), dict)
            else {}
        )
        crossed_listing_probe = (
            dict(raw_payload.get("crossedListingProbe") or {})
            if isinstance(raw_payload.get("crossedListingProbe"), dict)
            else None
        )

        def value(column: str, raw_key: str | None = None) -> Any:
            if column in keys and row[column] is not None:
                return row[column]
            return raw_orderbook.get(raw_key) if raw_key else None

        crossed_value = value("steam_orderbook_crossed", "crossed")
        currency_id = value("steam_currency_id", "currencyId")
        try:
            normalized_currency_id = int(currency_id) if currency_id is not None else None
        except (TypeError, ValueError):
            normalized_currency_id = None
        currency_valid = (
            raw_orderbook.get("currencyValid") is not False
            and normalized_currency_id == 23
        )
        expected_roi = value("expected_roi")
        try:
            expected_roi_pct = float(expected_roi) * 100.0 if expected_roi is not None else None
        except (TypeError, ValueError):
            expected_roi_pct = None
        rarity = (
            catalog_payload.get("rarity")
            if isinstance(catalog_payload.get("rarity"), dict)
            else {}
        )
        category = (
            catalog_payload.get("category")
            if isinstance(catalog_payload.get("category"), dict)
            else {}
        )
        weapon = (
            catalog_payload.get("weapon")
            if isinstance(catalog_payload.get("weapon"), dict)
            else {}
        )
        wear = (
            catalog_payload.get("wear")
            if isinstance(catalog_payload.get("wear"), dict)
            else {}
        )
        valid_observation_count_7d = int(value("valid_observation_count_7d") or 0)
        positive_observation_count_7d = int(value("positive_observation_count_7d") or 0)
        return {
            "id": int(row["id"]) if "id" in keys and row["id"] is not None else None,
            "scanId": row["scan_id"] if "scan_id" in keys else None,
            "marketHashName": str(row["market_hash_name"] or ""),
            "name": row["name_cn"] if "name_cn" in keys else None,
            "active": bool(row["active"]) if "active" in keys else None,
            "selectionStatus": row["status"] if "status" in keys else None,
            "eventType": row["event_type"] if "event_type" in keys else None,
            "researchOnly": True,
            "canExecute": False,
            "steamBuyPrice": value("steam_buy_price"),
            "steamOrderbook": {
                "observedAt": value("steam_orderbook_observed_at", "observedAt"),
                "currencyId": normalized_currency_id if normalized_currency_id is not None else currency_id,
                "currencyValid": currency_valid,
                "sellerFloorPrice": value("steam_buy_price", "sellerFloorPrice") if currency_valid else None,
                "sellerFloorCount": value("steam_seller_floor_count", "sellerFloorCount") if currency_valid else None,
                "buyerMaxPrice": value("steam_buyer_max_price", "buyerMaxPrice") if currency_valid else None,
                "buyerMaxCount": value("steam_buyer_max_count", "buyerMaxCount") if currency_valid else None,
                "spreadAmount": value("steam_spread_amount", "spreadAmount") if currency_valid else None,
                "spreadPct": value("steam_spread_pct", "spreadPct") if currency_valid else None,
                "crossed": bool(crossed_value) if crossed_value is not None else None,
                "sellOrderCountTotal": raw_orderbook.get("sellOrderCountTotal") if currency_valid else None,
                "buyOrderCountTotal": raw_orderbook.get("buyOrderCountTotal") if currency_valid else None,
                "sellLevels": list(raw_orderbook.get("sellLevels") or []) if currency_valid else [],
                "buyLevels": list(raw_orderbook.get("buyLevels") or []) if currency_valid else [],
            },
            "crossedListingProbe": crossed_listing_probe,
            "steamPriceSource": value("steam_price_source"),
            "c5ListingPrice": value("c5_listing_price"),
            "c5PriceSource": value("c5_price_source"),
            "c5ExpectedNetPrice": value("c5_expected_net_price"),
            "balanceDiscount": value("balance_discount"),
            "roiBasis": value("balance_discount"),
            "expectedProfit": value("expected_profit"),
            "expectedRoi": expected_roi,
            "expectedRoiPct": expected_roi_pct,
            "buyOrderReferenceRoi": value("buy_order_reference_roi"),
            "buyOrderReferenceProfit": value("buy_order_reference_profit"),
            "buyOrderReferenceStatus": value("buy_order_reference_status"),
            "inventoryCount": value("inventory_count") or 0,
            "tradableCount": value("tradable_count") or 0,
            "riskStatus": value("risk_status"),
            "riskReason": value("risk_reason"),
            "executionStatus": "selection_only",
            "executionStatusCode": "selection_only",
            "executionReason": value("execution_reason") or "research-only selection watch",
            "selectedAt": value("selected_at"),
            "firstSeenAt": value("first_seen_at"),
            "lastObservedAt": value("last_observed_at"),
            "nextScanAt": value("next_scan_at"),
            "lastError": value("last_error") if "last_error" in keys else value("error"),
            "removedAt": value("removed_at"),
            "observedAt": value("observed_at"),
            "itemType": str(category.get("name") or "").strip() or None,
            "weaponName": str(weapon.get("name") or "").strip() or None,
            "rarityName": str(rarity.get("name") or "").strip() or None,
            "rarityColor": str(rarity.get("color") or "").strip() or None,
            "wearName": str(wear.get("name") or "").strip() or None,
            "minFloat": catalog_payload.get("min_float"),
            "maxFloat": catalog_payload.get("max_float"),
            "imageUrl": str(catalog_payload.get("image") or "").strip() or None,
            "averageRoi7d": value("avg_expected_roi_7d"),
            "highestRoi7d": value("max_expected_roi_7d"),
            "lowestRoi7d": value("min_expected_roi_7d"),
            "validObservationCount7d": valid_observation_count_7d,
            "positiveRoiShare7d": (
                positive_observation_count_7d / valid_observation_count_7d
                if valid_observation_count_7d > 0
                else None
            ),
        }

    @staticmethod
    def _selection_orderbook_columns(raw: dict[str, Any]) -> dict[str, Any]:
        payload = raw.get("raw") if isinstance(raw.get("raw"), dict) else raw.get("raw_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        orderbook = payload.get("steamOrderbook") if isinstance(payload.get("steamOrderbook"), dict) else {}
        return {
            "steam_seller_floor_count": orderbook.get("sellerFloorCount"),
            "steam_buyer_max_price": orderbook.get("buyerMaxPrice"),
            "steam_buyer_max_count": orderbook.get("buyerMaxCount"),
            "steam_spread_amount": orderbook.get("spreadAmount"),
            "steam_spread_pct": orderbook.get("spreadPct"),
            "steam_orderbook_crossed": (
                1 if orderbook.get("crossed") is True else 0 if orderbook.get("crossed") is False else None
            ),
            "steam_currency_id": orderbook.get("currencyId"),
            "steam_orderbook_observed_at": orderbook.get("observedAt"),
            "raw_json": json.dumps(payload, ensure_ascii=False),
        }

    def _insert_profit_trade_selection_observation(
        self,
        row: dict[str, Any],
        *,
        scan_id: str,
        event_type: str,
        observed_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO profit_trade_selection_observations (
                scan_id, market_hash_name, event_type, status, observed_at, error,
                steam_buy_price, steam_seller_floor_count, steam_buyer_max_price,
                steam_buyer_max_count, steam_spread_amount, steam_spread_pct,
                steam_orderbook_crossed, steam_currency_id, steam_orderbook_observed_at,
                steam_price_source, c5_listing_price, c5_price_source,
                c5_expected_net_price, balance_discount, expected_profit, expected_roi,
                buy_order_reference_roi, buy_order_reference_profit, buy_order_reference_status,
                inventory_count, tradable_count, risk_status, risk_reason,
                execution_status, execution_reason, raw_json
            ) VALUES (
                :scan_id, :market_hash_name, :event_type, :status, :observed_at, :last_error,
                :steam_buy_price, :steam_seller_floor_count, :steam_buyer_max_price,
                :steam_buyer_max_count, :steam_spread_amount, :steam_spread_pct,
                :steam_orderbook_crossed, :steam_currency_id, :steam_orderbook_observed_at,
                :steam_price_source, :c5_listing_price, :c5_price_source,
                :c5_expected_net_price, :balance_discount, :expected_profit, :expected_roi,
                :buy_order_reference_roi, :buy_order_reference_profit, :buy_order_reference_status,
                :inventory_count, :tradable_count, :risk_status, :risk_reason,
                :execution_status, :execution_reason, :raw_json
            )
            """,
            {
                **row,
                "scan_id": scan_id,
                "event_type": event_type,
                "observed_at": observed_at,
            },
        )

    def _selection_watch_row_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        """Copy a watch row into an insert-ready, JSON-safe history payload."""

        keys = set(row.keys())
        result = {
            key: row[key]
            for key in (
                "market_hash_name",
                "status",
                "last_error",
                "steam_buy_price",
                "steam_seller_floor_count",
                "steam_buyer_max_price",
                "steam_buyer_max_count",
                "steam_spread_amount",
                "steam_spread_pct",
                "steam_orderbook_crossed",
                "steam_currency_id",
                "steam_orderbook_observed_at",
                "steam_price_source",
                "c5_listing_price",
                "c5_price_source",
                "c5_expected_net_price",
                "balance_discount",
                "expected_profit",
                "expected_roi",
                "buy_order_reference_roi",
                "buy_order_reference_profit",
                "buy_order_reference_status",
                "inventory_count",
                "tradable_count",
                "risk_status",
                "risk_reason",
                "execution_status",
                "execution_reason",
                "raw_json",
            )
            if key in keys
        }
        result.setdefault("status", "pending_first_scan")
        result.setdefault("last_error", None)
        result.setdefault("inventory_count", 0)
        result.setdefault("tradable_count", 0)
        result.setdefault("execution_status", "selection_only")
        result.setdefault("execution_reason", "research-only selection watch")
        result.setdefault("raw_json", "{}")
        return result

    def add_profit_trade_selection_watch(
        self,
        market_hash_name: str,
        *,
        name_cn: str,
        selected_at: str | None = None,
    ) -> tuple[sqlite3.Row, str]:
        """Add a local-catalog item to the research-only selection watch.

        The service layer validates the catalog membership first.  This DB API
        is nevertheless intentionally incapable of creating a profit-trade
        record or reserving an asset.
        """

        market_hash_name = str(market_hash_name or "").strip()
        display_name = str(name_cn or "").strip() or market_hash_name
        if not market_hash_name:
            raise ValueError("market_hash_name is required")
        timestamp = selected_at or utc_now_iso()
        with self.conn:
            existing = self.conn.execute(
                "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                (market_hash_name,),
            ).fetchone()
            if existing is None:
                active_count = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM profit_trade_selection_watch WHERE active = 1"
                    ).fetchone()[0]
                )
                if active_count >= 200:
                    raise ValueError("selection watch limit reached: at most 200 active items")
                self.conn.execute(
                    """
                    INSERT INTO profit_trade_selection_watch (
                        market_hash_name, name_cn, active, status, selected_at,
                        next_scan_at, execution_status, execution_reason, updated_at
                    ) VALUES (?, ?, 1, 'pending_first_scan', ?, ?, 'selection_only',
                              'research-only selection watch', ?)
                    """,
                    (market_hash_name, display_name, timestamp, timestamp, timestamp),
                )
                current = self.conn.execute(
                    "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                    (market_hash_name,),
                ).fetchone()
                if current is None:  # pragma: no cover - guarded by INSERT
                    raise RuntimeError("selection watch was not persisted")
                self._insert_profit_trade_selection_observation(
                    self._selection_watch_row_payload(current),
                    scan_id=f"PTSEL-{uuid.uuid4().hex}",
                    event_type="added",
                    observed_at=timestamp,
                )
                return current, "added"
            if bool(existing["active"]):
                return existing, "already_active"
        return self.reactivate_profit_trade_selection_watch(
            market_hash_name,
            name_cn=display_name,
            reactivated_at=timestamp,
        ), "reactivated"

    def reactivate_profit_trade_selection_watch(
        self,
        market_hash_name: str,
        *,
        name_cn: str | None = None,
        reactivated_at: str | None = None,
    ) -> sqlite3.Row:
        market_hash_name = str(market_hash_name or "").strip()
        if not market_hash_name:
            raise ValueError("market_hash_name is required")
        timestamp = reactivated_at or utc_now_iso()
        with self.conn:
            existing = self.conn.execute(
                "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                (market_hash_name,),
            ).fetchone()
            if existing is None:
                raise ValueError("selection watch item does not exist")
            if bool(existing["active"]):
                return existing
            active_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM profit_trade_selection_watch WHERE active = 1"
                ).fetchone()[0]
            )
            if active_count >= 200:
                raise ValueError("selection watch limit reached: at most 200 active items")
            self.conn.execute(
                """
                UPDATE profit_trade_selection_watch
                SET name_cn = COALESCE(?, name_cn),
                    active = 1,
                    status = 'pending_first_scan',
                    next_scan_at = ?,
                    last_error = NULL,
                    removed_at = NULL,
                    execution_status = 'selection_only',
                    execution_reason = 'research-only selection watch',
                    updated_at = ?
                WHERE market_hash_name = ?
                """,
                (str(name_cn or "").strip() or None, timestamp, timestamp, market_hash_name),
            )
            current = self.conn.execute(
                "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                (market_hash_name,),
            ).fetchone()
            if current is None:  # pragma: no cover
                raise RuntimeError("selection watch was not reactivated")
            self._insert_profit_trade_selection_observation(
                self._selection_watch_row_payload(current),
                scan_id=f"PTSEL-{uuid.uuid4().hex}",
                event_type="reentered",
                observed_at=timestamp,
            )
            return current

    def remove_profit_trade_selection_watch(
        self,
        market_hash_name: str,
        *,
        removed_at: str | None = None,
    ) -> sqlite3.Row:
        market_hash_name = str(market_hash_name or "").strip()
        if not market_hash_name:
            raise ValueError("market_hash_name is required")
        timestamp = removed_at or utc_now_iso()
        with self.conn:
            existing = self.conn.execute(
                "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                (market_hash_name,),
            ).fetchone()
            if existing is None:
                raise ValueError("selection watch item does not exist")
            if not bool(existing["active"]):
                return existing
            self.conn.execute(
                """
                UPDATE profit_trade_selection_watch
                SET active = 0,
                    status = 'removed',
                    next_scan_at = NULL,
                    removed_at = ?,
                    execution_status = 'selection_only',
                    execution_reason = 'research-only selection watch was removed',
                    updated_at = ?
                WHERE market_hash_name = ?
                """,
                (timestamp, timestamp, market_hash_name),
            )
            current = self.conn.execute(
                "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                (market_hash_name,),
            ).fetchone()
            if current is None:  # pragma: no cover
                raise RuntimeError("selection watch was not removed")
            self._insert_profit_trade_selection_observation(
                self._selection_watch_row_payload(current),
                scan_id=f"PTSEL-{uuid.uuid4().hex}",
                event_type="removed",
                observed_at=timestamp,
            )
            return current

    def count_active_profit_trade_selection_watch(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM profit_trade_selection_watch WHERE active = 1"
            ).fetchone()[0]
        )

    def list_due_profit_trade_selection_watch(
        self,
        *,
        now: str | None = None,
        include_not_due: bool = False,
    ) -> list[sqlite3.Row]:
        timestamp = str(now or utc_now_iso())
        due_clause = (
            ""
            if include_not_due
            else "AND (next_scan_at IS NULL OR next_scan_at <= ?)"
        )
        params: tuple[Any, ...] = () if include_not_due else (timestamp,)
        return self.conn.execute(
            f"""
            SELECT *
            FROM profit_trade_selection_watch
            WHERE active = 1
              {due_clause}
            ORDER BY COALESCE(next_scan_at, selected_at) ASC, market_hash_name ASC
            """,
            params,
        ).fetchall()

    def next_profit_trade_selection_watch_due_at(self) -> str | None:
        row = self.conn.execute(
            """
            SELECT MIN(COALESCE(next_scan_at, selected_at)) AS next_due_at
            FROM profit_trade_selection_watch
            WHERE active = 1
            """
        ).fetchone()
        if row is None:
            return None
        return str(row["next_due_at"] or "").strip() or None

    def list_profit_trade_selection_watch(
        self,
        *,
        active: bool | None = True,
        keyword: str | None = None,
        status: str | None = None,
        sort: str = "roi_desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        where: list[str] = []
        params: list[Any] = []
        if active is not None:
            where.append("w.active = ?")
            params.append(1 if active else 0)
        if keyword:
            pattern = f"%{str(keyword).strip()}%"
            where.append("(w.market_hash_name LIKE ? OR COALESCE(w.name_cn, '') LIKE ?)")
            params.extend([pattern, pattern])
        if status:
            where.append("w.status = ?")
            params.append(str(status).strip())
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM profit_trade_selection_watch w{where_sql}",
                tuple(params),
            ).fetchone()[0]
        )
        order_by = {
            "roi_asc": "w.expected_roi ASC, w.last_observed_at DESC, w.market_hash_name ASC",
            "updated_desc": "w.updated_at DESC, w.market_hash_name ASC",
            "selected_desc": "w.selected_at DESC, w.market_hash_name ASC",
            "next_scan": "COALESCE(w.next_scan_at, w.selected_at) ASC, w.market_hash_name ASC",
        }.get(sort, "w.expected_roi DESC, w.last_observed_at DESC, w.market_hash_name ASC")
        rows = self.conn.execute(
            f"""
            SELECT
                w.*,
                i.raw_json AS catalog_raw_json,
                h.avg_expected_roi_7d,
                h.max_expected_roi_7d,
                h.min_expected_roi_7d,
                h.valid_observation_count_7d,
                h.positive_observation_count_7d
            FROM profit_trade_selection_watch w
            LEFT JOIN items i
              ON i.market_hash_name = w.market_hash_name
            LEFT JOIN (
                SELECT
                    market_hash_name,
                    AVG(expected_roi) AS avg_expected_roi_7d,
                    MAX(expected_roi) AS max_expected_roi_7d,
                    MIN(expected_roi) AS min_expected_roi_7d,
                    COUNT(expected_roi) AS valid_observation_count_7d,
                    SUM(CASE WHEN expected_roi > 0 THEN 1 ELSE 0 END)
                        AS positive_observation_count_7d
                FROM profit_trade_selection_observations
                WHERE julianday(observed_at) >= julianday('now', '-7 days')
                GROUP BY market_hash_name
            ) h
              ON h.market_hash_name = w.market_hash_name
            {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return {
            "items": [self._profit_trade_selection_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }

    def list_profit_trade_selection_history(
        self,
        market_hash_name: str,
        *,
        from_time: str | None = None,
        to_time: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        market_hash_name = str(market_hash_name or "").strip()
        if not market_hash_name:
            raise ValueError("marketHashName is required")
        page = max(1, int(page))
        page_size = min(500, max(1, int(page_size)))
        where = ["market_hash_name = ?"]
        params: list[Any] = [market_hash_name]
        if from_time:
            where.append("julianday(observed_at) >= julianday(?)")
            params.append(str(from_time))
        if to_time:
            where.append("julianday(observed_at) <= julianday(?)")
            params.append(str(to_time))
        where_sql = " AND ".join(where)
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM profit_trade_selection_observations WHERE {where_sql}",
                tuple(params),
            ).fetchone()[0]
        )
        rows = self.conn.execute(
            f"""
            SELECT * FROM profit_trade_selection_observations
            WHERE {where_sql}
            ORDER BY observed_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        trend_rows = self.conn.execute(
            f"""
            SELECT observed_at, expected_roi, buy_order_reference_roi, balance_discount
            FROM profit_trade_selection_observations
            WHERE {where_sql} AND expected_roi IS NOT NULL
            ORDER BY observed_at ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
        return {
            "items": [self._profit_trade_selection_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "trend": _profit_trade_history_trend(trend_rows),
        }

    def profit_trade_selection_history_statistics(
        self,
        market_hash_name: str,
        *,
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> dict[str, Any]:
        """Return history-wide ROI aggregates, independent of UI pagination."""

        market_hash_name = str(market_hash_name or "").strip()
        if not market_hash_name:
            raise ValueError("marketHashName is required")
        where = ["market_hash_name = ?"]
        params: list[Any] = [market_hash_name]
        if from_time:
            where.append("julianday(observed_at) >= julianday(?)")
            params.append(str(from_time))
        if to_time:
            where.append("julianday(observed_at) <= julianday(?)")
            params.append(str(to_time))
        row = self.conn.execute(
            f"""
            SELECT
                COUNT(expected_roi) AS observed_count,
                MAX(expected_roi) AS max_expected_roi,
                MIN(expected_roi) AS min_expected_roi,
                AVG(expected_roi) AS avg_expected_roi,
                SUM(CASE WHEN expected_roi > 0 THEN 1 ELSE 0 END)
                    AS positive_observation_count,
                MAX(buy_order_reference_roi) AS max_buy_order_reference_roi,
                AVG(buy_order_reference_roi) AS avg_buy_order_reference_roi,
                SUM(expected_profit) AS expected_profit_total,
                SUM(buy_order_reference_profit) AS buy_order_reference_profit_total,
                MIN(balance_discount) AS roi_basis_min,
                MAX(balance_discount) AS roi_basis_max
            FROM profit_trade_selection_observations
            WHERE {' AND '.join(where)}
            """,
            tuple(params),
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite aggregate always yields one row
            return {}
        roi_basis_min = row["roi_basis_min"]
        roi_basis_max = row["roi_basis_max"]
        timeline = self.conn.execute(
            f"""
            SELECT observed_at, expected_roi
            FROM profit_trade_selection_observations
            WHERE {' AND '.join(where)}
              AND expected_roi IS NOT NULL
            ORDER BY observed_at ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
        parsed_timeline: list[tuple[datetime, float]] = []
        for sample in timeline:
            try:
                timestamp = datetime.fromisoformat(
                    str(sample["observed_at"] or "").replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                parsed_timeline.append(
                    (timestamp.astimezone(timezone.utc), float(sample["expected_roi"]))
                )
            except (TypeError, ValueError, OverflowError):
                continue
        deltas = [
            max(
                0.0,
                (parsed_timeline[index + 1][0] - parsed_timeline[index][0]).total_seconds(),
            )
            for index in range(max(0, len(parsed_timeline) - 1))
        ]
        normal_deltas = sorted(delta for delta in deltas if 0 < delta <= 3600)
        cadence_seconds = (
            normal_deltas[len(normal_deltas) // 2]
            if normal_deltas
            else 600.0
        )
        cadence_seconds = min(1800.0, max(60.0, cadence_seconds))
        duration_by_key = {"high": 0.0, "good": 0.0, "low": 0.0, "negative": 0.0}
        for index, (_, roi) in enumerate(parsed_timeline):
            duration = (
                min(deltas[index], cadence_seconds * 2.0)
                if index < len(deltas) and deltas[index] > 0
                else cadence_seconds
            )
            key = (
                "high"
                if roi >= 0.02
                else "good"
                if roi >= 0.01
                else "low"
                if roi >= 0
                else "negative"
            )
            duration_by_key[key] += duration
        duration_seconds = sum(duration_by_key.values())
        duration_labels = {
            "high": "≥ 2.00%",
            "good": "1%~2%",
            "low": "0%~1%",
            "negative": "< 0%",
        }
        duration_buckets = [
            {
                "key": key,
                "label": duration_labels[key],
                "seconds": round(duration_by_key[key]),
                "share": (
                    duration_by_key[key] / duration_seconds
                    if duration_seconds > 0
                    else 0.0
                ),
            }
            for key in ("high", "good", "low", "negative")
        ]
        observed_count = int(row["observed_count"] or 0)
        positive_observation_count = int(row["positive_observation_count"] or 0)
        return {
            "observedCount": observed_count,
            "maxExpectedRoi": row["max_expected_roi"],
            "minExpectedRoi": row["min_expected_roi"],
            "avgExpectedRoi": row["avg_expected_roi"],
            "maxBuyOrderReferenceRoi": row["max_buy_order_reference_roi"],
            "avgBuyOrderReferenceRoi": row["avg_buy_order_reference_roi"],
            "expectedProfitTotal": row["expected_profit_total"] or 0.0,
            "buyOrderReferenceProfitTotal": row["buy_order_reference_profit_total"] or 0.0,
            # Keep the selection history wire-compatible with the existing
            # inventory ROI history drawer.  The selection-specific summary
            # names above remain for callers that need the richer aggregates.
            "highestRoi": row["max_expected_roi"],
            "lowestRoi": row["min_expected_roi"],
            "averageRoi": row["avg_expected_roi"],
            "positiveRoiShare": (
                positive_observation_count / observed_count
                if observed_count > 0
                else None
            ),
            "durationSeconds": round(duration_seconds),
            "roiDurationBuckets": duration_buckets,
            "roiBasis": (
                roi_basis_min
                if roi_basis_min is not None and roi_basis_min == roi_basis_max
                else None
            ),
            "roiBasisMin": roi_basis_min,
            "roiBasisMax": roi_basis_max,
            "validObservationCount": observed_count,
        }

    def record_profit_trade_selection_watch_scan(
        self,
        observations: list[dict[str, Any]],
        *,
        scan_id: str,
        observed_at: str | None = None,
        interval_seconds: float = 30.0 * 60.0,
    ) -> dict[str, int]:
        """Persist selected market observations without ROI or inventory gating."""

        timestamp = str(observed_at or utc_now_iso())
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            next_scan_at = (
                parsed.astimezone(timezone.utc)
                + timedelta(seconds=max(1.0, float(interval_seconds)))
            ).isoformat()
        except (TypeError, ValueError):
            next_scan_at = timestamp
        inserted = 0
        updated = 0
        with self.conn:
            for raw in observations:
                market_hash_name = str(raw.get("market_hash_name") or "").strip()
                if not market_hash_name:
                    continue
                existing = self.conn.execute(
                    "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                    (market_hash_name,),
                ).fetchone()
                if existing is None or not bool(existing["active"]):
                    continue
                orderbook_columns = self._selection_orderbook_columns(raw)
                status = str(raw.get("status") or "observed").strip() or "observed"
                event_type = str(raw.get("event_type") or "observed").strip() or "observed"
                row = {
                    "market_hash_name": market_hash_name,
                    "name_cn": str(raw.get("name_cn") or existing["name_cn"] or market_hash_name),
                    "status": status,
                    "last_error": raw.get("last_error"),
                    "steam_buy_price": raw.get("steam_buy_price"),
                    "steam_price_source": raw.get("steam_price_source"),
                    "c5_listing_price": raw.get("c5_listing_price"),
                    "c5_price_source": raw.get("c5_price_source"),
                    "c5_expected_net_price": raw.get("c5_expected_net_price"),
                    "balance_discount": raw.get("balance_discount"),
                    "expected_profit": raw.get("expected_profit"),
                    "expected_roi": raw.get("expected_roi"),
                    "buy_order_reference_roi": raw.get("buy_order_reference_roi"),
                    "buy_order_reference_profit": raw.get("buy_order_reference_profit"),
                    "buy_order_reference_status": raw.get("buy_order_reference_status"),
                    "inventory_count": 0,
                    "tradable_count": 0,
                    "risk_status": raw.get("risk_status") or "selection_only",
                    "risk_reason": raw.get("risk_reason") or "research-only selection watch",
                    "execution_status": "selection_only",
                    "execution_reason": "research-only selection watch",
                    **orderbook_columns,
                }
                self.conn.execute(
                    """
                    UPDATE profit_trade_selection_watch
                    SET name_cn = :name_cn,
                        status = :status,
                        last_observed_at = :timestamp,
                        next_scan_at = :next_scan_at,
                        last_error = :last_error,
                        steam_buy_price = :steam_buy_price,
                        steam_seller_floor_count = :steam_seller_floor_count,
                        steam_buyer_max_price = :steam_buyer_max_price,
                        steam_buyer_max_count = :steam_buyer_max_count,
                        steam_spread_amount = :steam_spread_amount,
                        steam_spread_pct = :steam_spread_pct,
                        steam_orderbook_crossed = :steam_orderbook_crossed,
                        steam_currency_id = :steam_currency_id,
                        steam_orderbook_observed_at = :steam_orderbook_observed_at,
                        steam_price_source = :steam_price_source,
                        c5_listing_price = :c5_listing_price,
                        c5_price_source = :c5_price_source,
                        c5_expected_net_price = :c5_expected_net_price,
                        balance_discount = :balance_discount,
                        expected_profit = :expected_profit,
                        expected_roi = :expected_roi,
                        buy_order_reference_roi = :buy_order_reference_roi,
                        buy_order_reference_profit = :buy_order_reference_profit,
                        buy_order_reference_status = :buy_order_reference_status,
                        inventory_count = 0,
                        tradable_count = 0,
                        risk_status = :risk_status,
                        risk_reason = :risk_reason,
                        execution_status = 'selection_only',
                        execution_reason = :execution_reason,
                        first_seen_at = COALESCE(first_seen_at, :timestamp),
                        updated_at = :timestamp,
                        raw_json = :raw_json
                    WHERE market_hash_name = :market_hash_name AND active = 1
                    """,
                    {**row, "timestamp": timestamp, "next_scan_at": next_scan_at},
                )
                current = self.conn.execute(
                    "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                    (market_hash_name,),
                ).fetchone()
                if current is None:  # pragma: no cover - active row was selected above
                    continue
                history_row = self._selection_watch_row_payload(current)
                self._insert_profit_trade_selection_observation(
                    history_row,
                    scan_id=scan_id,
                    event_type=event_type,
                    observed_at=timestamp,
                )
                if existing["last_observed_at"] is None:
                    inserted += 1
                else:
                    updated += 1
        return {"inserted": inserted, "updated": updated}

    def defer_profit_trade_selection_watch_scan(
        self,
        market_hash_names: Iterable[str],
        *,
        scan_id: str,
        reason: str,
        next_scan_at: str,
        observed_at: str | None = None,
    ) -> int:
        """Record a bounded batch stop, e.g. after a Steam 429, without retrying."""

        timestamp = str(observed_at or utc_now_iso())
        changed = 0
        with self.conn:
            for value in market_hash_names:
                market_hash_name = str(value or "").strip()
                if not market_hash_name:
                    continue
                existing = self.conn.execute(
                    "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ? AND active = 1",
                    (market_hash_name,),
                ).fetchone()
                if existing is None:
                    continue
                self.conn.execute(
                    """
                    UPDATE profit_trade_selection_watch
                    SET status = 'scan_deferred', last_error = ?, next_scan_at = ?, updated_at = ?
                    WHERE market_hash_name = ? AND active = 1
                    """,
                    (str(reason)[:1000], str(next_scan_at), timestamp, market_hash_name),
                )
                current = self.conn.execute(
                    "SELECT * FROM profit_trade_selection_watch WHERE market_hash_name = ?",
                    (market_hash_name,),
                ).fetchone()
                if current is None:  # pragma: no cover
                    continue
                self._insert_profit_trade_selection_observation(
                    self._selection_watch_row_payload(current),
                    scan_id=scan_id,
                    event_type="scan_deferred",
                    observed_at=timestamp,
                )
                changed += 1
        return changed

    # ------------------------------------------------------------------
    # Scheduled task queue
    # ------------------------------------------------------------------

    def upsert_scheduled_task(
        self,
        task_key: str,
        *,
        source: str,
        task_type: str,
        next_attempt_at: str | datetime,
        account_id: str | None = None,
        operation_id: str | int | None = None,
        payload: dict[str, Any] | None = None,
        status: str = "pending",
        priority: int = 2,
        last_error: str | None = None,
    ) -> sqlite3.Row:
        key = str(task_key or "").strip()
        normalized_source = str(source or "").strip()
        normalized_type = str(task_type or "").strip()
        normalized_status = str(status or "").strip()
        if not key or not normalized_source or not normalized_type:
            raise ValueError("task_key, source and task_type are required")
        if not normalized_status:
            raise ValueError("status is required")
        now = utc_now_iso()
        completed_at = now if normalized_status in {"completed", "failed", "cancelled"} else None
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    task_key, source, task_type, account_id, operation_id,
                    payload_json, status, priority, next_attempt_at,
                    last_error, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    source = excluded.source,
                    task_type = excluded.task_type,
                    account_id = excluded.account_id,
                    operation_id = excluded.operation_id,
                    payload_json = excluded.payload_json,
                    status = CASE
                        WHEN scheduled_tasks.status = 'running' THEN scheduled_tasks.status
                        ELSE excluded.status
                    END,
                    priority = excluded.priority,
                    next_attempt_at = CASE
                        WHEN scheduled_tasks.status = 'running' THEN scheduled_tasks.next_attempt_at
                        ELSE excluded.next_attempt_at
                    END,
                    lease_owner = CASE
                        WHEN scheduled_tasks.status = 'running' THEN scheduled_tasks.lease_owner
                        ELSE NULL
                    END,
                    lease_expires_at = CASE
                        WHEN scheduled_tasks.status = 'running' THEN scheduled_tasks.lease_expires_at
                        ELSE NULL
                    END,
                    last_error = excluded.last_error,
                    completed_at = CASE
                        WHEN scheduled_tasks.status = 'running' THEN scheduled_tasks.completed_at
                        ELSE excluded.completed_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    normalized_source,
                    normalized_type,
                    str(account_id) if account_id is not None else None,
                    str(operation_id) if operation_id is not None else None,
                    _json_object(payload),
                    normalized_status,
                    int(priority),
                    _utc_iso(next_attempt_at),
                    last_error,
                    now,
                    now,
                    completed_at,
                ),
            )
        row = self.get_scheduled_task(key)
        if row is None:  # pragma: no cover
            raise RuntimeError(f"scheduled task was not persisted: {key}")
        return row

    def ensure_scheduled_task(
        self,
        task_key: str,
        *,
        source: str,
        task_type: str,
        next_attempt_at: str | datetime,
        account_id: str | None = None,
        operation_id: str | int | None = None,
        payload: dict[str, Any] | None = None,
        status: str = "pending",
        priority: int = 2,
        last_error: str | None = None,
    ) -> sqlite3.Row:
        """Create a task once without changing an existing lease or terminal state."""

        key = str(task_key or "").strip()
        normalized_source = str(source or "").strip()
        normalized_type = str(task_type or "").strip()
        normalized_status = str(status or "").strip()
        if not key or not normalized_source or not normalized_type:
            raise ValueError("task_key, source and task_type are required")
        if not normalized_status:
            raise ValueError("status is required")
        now = utc_now_iso()
        completed_at = now if normalized_status in {"completed", "failed", "cancelled"} else None
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO scheduled_tasks (
                    task_key, source, task_type, account_id, operation_id,
                    payload_json, status, priority, next_attempt_at,
                    last_error, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    normalized_source,
                    normalized_type,
                    str(account_id) if account_id is not None else None,
                    str(operation_id) if operation_id is not None else None,
                    _json_object(payload),
                    normalized_status,
                    int(priority),
                    _utc_iso(next_attempt_at),
                    last_error,
                    now,
                    now,
                    completed_at,
                ),
            )
        row = self.get_scheduled_task(key)
        if row is None:  # pragma: no cover
            raise RuntimeError(f"scheduled task was not persisted: {key}")
        return row

    def get_scheduled_task(self, task_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scheduled_tasks WHERE task_key = ?",
            (str(task_key),),
        ).fetchone()

    def list_scheduled_tasks(
        self,
        *,
        source: str | None = None,
        task_type: str | None = None,
        status: str | None = None,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("source", source),
            ("task_type", task_type),
            ("status", status),
            ("account_id", account_id),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(str(value))
        sql = "SELECT * FROM scheduled_tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY next_attempt_at ASC, priority ASC, id ASC LIMIT ?"
        params.append(max(1, int(limit)))
        return self.conn.execute(sql, tuple(params)).fetchall()

    def claim_due_scheduled_tasks(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        lease_seconds: float = 60,
        source: str | None = None,
        now: str | datetime | None = None,
        starvation_guard_task_key: str | None = None,
        starvation_guard_after_seconds: float | None = None,
        deadline_guard_task_type: str | None = None,
        deadline_guard_after_seconds: float | None = None,
    ) -> list[sqlite3.Row]:
        owner = str(worker_id or "").strip()
        if not owner:
            raise ValueError("worker_id is required")
        now_iso = _utc_iso(now)
        expires_at = _lease_expiry(now_iso, lease_seconds)
        max_rows = max(1, int(limit))
        source_clause = " AND source = ?" if source is not None else ""
        order_clause = "priority ASC, next_attempt_at ASC, id ASC"
        params: list[Any] = [now_iso, now_iso]
        if source is not None:
            params.append(str(source))

        deadline_type = str(deadline_guard_task_type or "").strip()
        deadline_after = deadline_guard_after_seconds
        if bool(deadline_type) != (deadline_after is not None):
            raise ValueError(
                "deadline_guard_task_type and deadline_guard_after_seconds "
                "must be provided together"
            )
        if deadline_type:
            deadline_seconds = float(deadline_after)
            if deadline_seconds <= 0:
                raise ValueError("deadline_guard_after_seconds must be positive")
            deadline_cutoff = _utc_iso(
                datetime.fromisoformat(now_iso) - timedelta(seconds=deadline_seconds)
            )
        else:
            deadline_cutoff = None

        guard_key = str(starvation_guard_task_key or "").strip()
        guard_after = starvation_guard_after_seconds
        if bool(guard_key) != (guard_after is not None):
            raise ValueError(
                "starvation_guard_task_key and starvation_guard_after_seconds "
                "must be provided together"
            )
        if guard_key:
            guard_seconds = float(guard_after)
            if guard_seconds <= 0:
                raise ValueError("starvation_guard_after_seconds must be positive")
            guard_cutoff = _utc_iso(
                datetime.fromisoformat(now_iso) - timedelta(seconds=guard_seconds)
            )
        else:
            guard_cutoff = None

        if deadline_type or guard_key:
            # P0 is the absolute safety lane.  A task type with an exceeded
            # start-lag budget receives the next fair slot before P1, while a
            # separately guarded background scan remains behind it.  Both
            # persisted priorities stay unchanged.
            rank = 1
            rank_lines = ["WHEN priority <= 0 THEN 0"]
            if deadline_type:
                rank_lines.append(
                    f"WHEN task_type = ? AND next_attempt_at <= ? THEN {rank}"
                )
                params.extend((deadline_type, deadline_cutoff))
                rank += 1
            if guard_key:
                rank_lines.append(
                    f"WHEN task_key = ? AND next_attempt_at <= ? THEN {rank}"
                )
                params.extend((guard_key, guard_cutoff))
                rank += 1
            order_clause = (
                "CASE\n                    "
                + "\n                    ".join(rank_lines)
                + f"\n                    ELSE priority + {rank}\n                END ASC,\n"
                "                next_attempt_at ASC,\n"
                "                id ASC"
            )
        params.append(max_rows)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            rows = self.conn.execute(
                f"""
                SELECT id
                FROM scheduled_tasks
                WHERE next_attempt_at <= ?
                  AND (
                    status IN ('pending', 'retry')
                    OR (status = 'running' AND lease_expires_at <= ?)
                  )
                  {source_clause}
                ORDER BY {order_clause}
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"""
                    UPDATE scheduled_tasks
                    SET status = 'running',
                        lease_owner = ?,
                        lease_expires_at = ?,
                        attempt_count = attempt_count + 1,
                        updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (owner, expires_at, now_iso, *ids),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self.conn.execute(
            f"SELECT * FROM scheduled_tasks WHERE id IN ({placeholders}) ORDER BY priority, next_attempt_at, id",
            tuple(ids),
        ).fetchall()

    def renew_scheduled_task_lease(
        self,
        task_key: str,
        worker_id: str,
        *,
        lease_seconds: float = 60,
        now: str | datetime | None = None,
    ) -> bool:
        now_iso = _utc_iso(now)
        cursor = self.conn.execute(
            """
            UPDATE scheduled_tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE task_key = ? AND status = 'running' AND lease_owner = ?
            """,
            (_lease_expiry(now_iso, lease_seconds), now_iso, str(task_key), str(worker_id)),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def complete_scheduled_task(
        self,
        task_key: str,
        worker_id: str,
        *,
        status: str = "completed",
        error: str | None = None,
        now: str | datetime | None = None,
    ) -> bool:
        terminal_status = str(status)
        if terminal_status not in {"completed", "failed", "cancelled"}:
            raise ValueError("scheduled task terminal status is invalid")
        now_iso = _utc_iso(now)
        cursor = self.conn.execute(
            """
            UPDATE scheduled_tasks
            SET status = ?, last_error = ?, lease_owner = NULL,
                lease_expires_at = NULL, completed_at = ?, updated_at = ?
            WHERE task_key = ? AND status = 'running' AND lease_owner = ?
            """,
            (terminal_status, error, now_iso, now_iso, str(task_key), str(worker_id)),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def reschedule_scheduled_task(
        self,
        task_key: str,
        *,
        next_attempt_at: str | datetime,
        worker_id: str | None = None,
        error: str | None = None,
        status: str = "pending",
    ) -> bool:
        where = "task_key = ?"
        params: list[Any] = [str(status), _utc_iso(next_attempt_at), error, utc_now_iso(), str(task_key)]
        if worker_id is not None:
            where += " AND status = 'running' AND lease_owner = ?"
            params.append(str(worker_id))
        cursor = self.conn.execute(
            f"""
            UPDATE scheduled_tasks
            SET status = ?, next_attempt_at = ?, last_error = ?,
                lease_owner = NULL, lease_expires_at = NULL,
                completed_at = NULL, updated_at = ?
            WHERE {where}
            """,
            tuple(params),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def delete_scheduled_task(self, task_key: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM scheduled_tasks WHERE task_key = ? AND status != 'running'",
            (str(task_key),),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Steam Cookie health
    # ------------------------------------------------------------------

    def upsert_steam_cookie_health(
        self,
        account_id: str,
        *,
        status: str,
        account_name: str | None = None,
        steam_id: str | None = None,
        batch_id: str | None = None,
        failure_count: int = 0,
        last_error: str | None = None,
        last_validated_at: str | datetime | None = None,
        next_retry_at: str | datetime | None = None,
        retry_after_seconds: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        key = str(account_id or "").strip()
        normalized_status = str(status or "").strip()
        if not key or not normalized_status:
            raise ValueError("account_id and status are required")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO steam_cookie_health (
                    account_id, account_name, steam_id, status, batch_id,
                    failure_count, last_error, last_validated_at, next_retry_at,
                    retry_after_seconds, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    account_name = COALESCE(excluded.account_name, steam_cookie_health.account_name),
                    steam_id = COALESCE(excluded.steam_id, steam_cookie_health.steam_id),
                    status = excluded.status,
                    batch_id = excluded.batch_id,
                    failure_count = excluded.failure_count,
                    last_error = excluded.last_error,
                    last_validated_at = COALESCE(
                        excluded.last_validated_at,
                        steam_cookie_health.last_validated_at
                    ),
                    next_retry_at = excluded.next_retry_at,
                    retry_after_seconds = excluded.retry_after_seconds,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    account_name,
                    steam_id,
                    normalized_status,
                    batch_id,
                    max(0, int(failure_count)),
                    last_error,
                    _utc_iso(last_validated_at) if last_validated_at is not None else None,
                    _utc_iso(next_retry_at) if next_retry_at is not None else None,
                    float(retry_after_seconds) if retry_after_seconds is not None else None,
                    _json_object(payload),
                    now,
                    now,
                ),
            )
        row = self.get_steam_cookie_health(key)
        if row is None:  # pragma: no cover
            raise RuntimeError(f"Steam Cookie health was not persisted: {key}")
        return row

    def get_steam_cookie_health(self, account_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM steam_cookie_health WHERE account_id = ?",
            (str(account_id),),
        ).fetchone()

    def list_steam_cookie_health(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM steam_cookie_health ORDER BY account_name, account_id"
        ).fetchall()

    def list_due_steam_cookie_retries(
        self,
        *,
        now: str | datetime | None = None,
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM steam_cookie_health
            WHERE status != 'valid'
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY COALESCE(next_retry_at, created_at), account_id
            """,
            (_utc_iso(now),),
        ).fetchall()

    # ------------------------------------------------------------------
    # Cross-process Steam request queue
    # ------------------------------------------------------------------

    def enqueue_steam_request(
        self,
        request_id: str,
        *,
        source: str,
        route: str,
        priority: int,
        account_id: str | None = None,
        method: str | None = None,
        operation_id: str | int | None = None,
        payload: dict[str, Any] | None = None,
        available_at: str | datetime | None = None,
    ) -> sqlite3.Row:
        key = str(request_id or "").strip()
        normalized_source = str(source or "").strip()
        normalized_route = str(route or "").strip()
        if not key or not normalized_source or not normalized_route:
            raise ValueError("request_id, source and route are required")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO steam_request_queue (
                    request_id, source, route, method, account_id, operation_id,
                    priority, payload_json, status, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    key,
                    normalized_source,
                    normalized_route,
                    method,
                    str(account_id) if account_id is not None else None,
                    str(operation_id) if operation_id is not None else None,
                    int(priority),
                    _json_object(payload),
                    _utc_iso(available_at) if available_at is not None else now,
                    now,
                    now,
                ),
            )
        row = self.get_steam_request(key)
        if row is None:  # pragma: no cover
            raise RuntimeError(f"Steam request was not persisted: {key}")
        return row

    def get_steam_request(self, request_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM steam_request_queue WHERE request_id = ?",
            (str(request_id),),
        ).fetchone()

    def _claim_steam_request_locked(
        self,
        request_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
        now_iso: str,
        parallel_group: str | None = None,
        parallel_limit: int = 1,
        account_exclusive: bool = False,
    ) -> sqlite3.Row | None:
        active_rows = self.conn.execute(
            """
            SELECT * FROM steam_request_queue
            WHERE status = 'running' AND lease_expires_at > ?
            ORDER BY id
            """,
            (now_iso,),
        ).fetchall()
        for active in active_rows:
            if active["request_id"] == request_id and active["lease_owner"] == worker_id:
                return active

        candidate = self.conn.execute(
            """
            SELECT * FROM steam_request_queue
            WHERE request_id = ?
              AND available_at <= ?
              AND (
                status = 'pending'
                OR (status = 'running' AND lease_expires_at <= ?)
              )
            """,
            (request_id, now_iso, now_iso),
        ).fetchone()
        if candidate is None:
            return None

        safe_group = str(parallel_group or "").strip()
        safe_limit = min(8, max(1, int(parallel_limit)))

        def row_policy(row: sqlite3.Row) -> tuple[str, int, bool]:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            group = str(payload.get("schedulerParallelGroup") or "").strip()
            try:
                limit = min(
                    8,
                    max(1, int(payload.get("schedulerParallelLimit") or 1)),
                )
            except (TypeError, ValueError, OverflowError):
                limit = 1
            return (
                group,
                limit,
                bool(payload.get("schedulerAccountExclusive")),
            )

        active_accounts = {
            str(row["account_id"] or "").strip()
            for row in active_rows
            if str(row["account_id"] or "").strip()
        }
        if active_rows:
            if not safe_group or safe_limit <= 1 or len(active_rows) >= safe_limit:
                return None
            candidate_account = str(candidate["account_id"] or "").strip()
            if account_exclusive and (
                not candidate_account or candidate_account in active_accounts
            ):
                return None
            for active in active_rows:
                active_group, active_limit, active_account_exclusive = row_policy(active)
                if (
                    active_group != safe_group
                    or active_limit <= len(active_rows)
                    or active_account_exclusive != account_exclusive
                ):
                    return None

        pending_rows = self.conn.execute(
            """
            SELECT * FROM steam_request_queue
            WHERE available_at <= ?
              AND (
                status = 'pending'
                OR (status = 'running' AND lease_expires_at <= ?)
              )
            ORDER BY priority ASC, available_at ASC, id ASC
            """,
            (now_iso, now_iso),
        ).fetchall()
        claimable_head: sqlite3.Row | None = None
        for pending in pending_rows:
            if active_rows:
                pending_group, _, pending_account_exclusive = row_policy(pending)
                pending_account = str(pending["account_id"] or "").strip()
                if int(pending["priority"]) < int(candidate["priority"]):
                    return None
                if (
                    pending_group == safe_group
                    and pending_account_exclusive == account_exclusive
                    and account_exclusive
                    and pending_account in active_accounts
                ):
                    # This older ticket cannot use its busy account lane yet;
                    # let the next distinct-account ticket fill lane two.
                    continue
            claimable_head = pending
            break
        if claimable_head is None or str(claimable_head["request_id"]) != request_id:
            return None
        expires_at = _lease_expiry(now_iso, lease_seconds)
        self.conn.execute(
            """
            UPDATE steam_request_queue
            SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                attempt_count = attempt_count + 1, updated_at = ?
            WHERE request_id = ?
            """,
            (worker_id, expires_at, now_iso, request_id),
        )
        return self.get_steam_request(request_id)

    def claim_steam_request(
        self,
        request_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 30,
        now: str | datetime | None = None,
        parallel_group: str | None = None,
        parallel_limit: int = 1,
        account_exclusive: bool = False,
    ) -> sqlite3.Row | None:
        key = str(request_id or "").strip()
        owner = str(worker_id or "").strip()
        if not key or not owner:
            raise ValueError("request_id and worker_id are required")
        now_iso = _utc_iso(now)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self._claim_steam_request_locked(
                key,
                owner,
                lease_seconds=lease_seconds,
                now_iso=now_iso,
                parallel_group=parallel_group,
                parallel_limit=parallel_limit,
                account_exclusive=account_exclusive,
            )
            self.conn.commit()
            return row
        except Exception:
            self.conn.rollback()
            raise

    def claim_next_steam_request(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 30,
        now: str | datetime | None = None,
    ) -> sqlite3.Row | None:
        owner = str(worker_id or "").strip()
        if not owner:
            raise ValueError("worker_id is required")
        now_iso = _utc_iso(now)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            active = self.conn.execute(
                """
                SELECT * FROM steam_request_queue
                WHERE status = 'running' AND lease_expires_at > ?
                ORDER BY id LIMIT 1
                """,
                (now_iso,),
            ).fetchone()
            if active is not None:
                self.conn.commit()
                return active if active["lease_owner"] == owner else None
            head = self.conn.execute(
                """
                SELECT request_id FROM steam_request_queue
                WHERE available_at <= ?
                  AND (status = 'pending' OR (status = 'running' AND lease_expires_at <= ?))
                ORDER BY priority ASC, available_at ASC, id ASC LIMIT 1
                """,
                (now_iso, now_iso),
            ).fetchone()
            row = None
            if head is not None:
                row = self._claim_steam_request_locked(
                    str(head["request_id"]),
                    owner,
                    lease_seconds=lease_seconds,
                    now_iso=now_iso,
                )
            self.conn.commit()
            return row
        except Exception:
            self.conn.rollback()
            raise

    def renew_steam_request_lease(
        self,
        request_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 30,
        now: str | datetime | None = None,
    ) -> bool:
        now_iso = _utc_iso(now)
        cursor = self.conn.execute(
            """
            UPDATE steam_request_queue
            SET lease_expires_at = ?, updated_at = ?
            WHERE request_id = ? AND status = 'running' AND lease_owner = ?
            """,
            (
                _lease_expiry(now_iso, lease_seconds),
                now_iso,
                str(request_id),
                str(worker_id),
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def complete_steam_request(
        self,
        request_id: str,
        worker_id: str,
        *,
        status: str = "completed",
        http_status: int | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        now: str | datetime | None = None,
    ) -> sqlite3.Row | None:
        terminal_status = str(status)
        if terminal_status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Steam request terminal status is invalid")
        now_iso = _utc_iso(now)
        cursor = self.conn.execute(
            """
            UPDATE steam_request_queue
            SET status = ?, http_status = ?, result_json = ?, last_error = ?,
                lease_owner = NULL, lease_expires_at = NULL,
                completed_at = ?, updated_at = ?
            WHERE request_id = ? AND status = 'running' AND lease_owner = ?
            """,
            (
                terminal_status,
                int(http_status) if http_status is not None else None,
                _json_object(result) if result is not None else None,
                error,
                now_iso,
                now_iso,
                str(request_id),
                str(worker_id),
            ),
        )
        self.conn.commit()
        return self.get_steam_request(request_id) if cursor.rowcount == 1 else None

    def cancel_steam_request(self, request_id: str, *, reason: str | None = None) -> bool:
        now = utc_now_iso()
        cursor = self.conn.execute(
            """
            UPDATE steam_request_queue
            SET status = 'cancelled', last_error = ?, completed_at = ?, updated_at = ?
            WHERE request_id = ? AND status = 'pending'
            """,
            (reason, now, now, str(request_id)),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def cancel_orphaned_steam_requests(
        self,
        *,
        now: str | datetime | None = None,
        pending_stale_seconds: float = 300.0,
        running_grace_seconds: float = 5.0,
    ) -> dict[str, int]:
        """Cancel persisted tickets whose in-process callback can no longer be trusted.

        Steam callbacks are intentionally never serialized into SQLite. A
        process crash can therefore leave a pending ticket, or an expired
        running lease, that no future process is able to execute. Keeping such
        a row eligible at the queue head would block every later request.

        A grace window avoids treating a briefly delayed heartbeat as an
        orphan. Pending tickets receive a larger stale window because they may
        legitimately wait behind a long-running request.
        """

        now_iso = _utc_iso(now)
        now_dt = datetime.fromisoformat(now_iso)
        pending_cutoff = _utc_iso(
            now_dt - timedelta(seconds=max(0.0, float(pending_stale_seconds)))
        )
        running_cutoff = _utc_iso(
            now_dt - timedelta(seconds=max(0.0, float(running_grace_seconds)))
        )
        with self.conn:
            pending = self.conn.execute(
                """
                UPDATE steam_request_queue
                SET status = 'cancelled', last_error = 'orphaned_pending_request',
                    lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = ?, updated_at = ?
                WHERE status = 'pending' AND updated_at <= ?
                """,
                (now_iso, now_iso, pending_cutoff),
            )
            running = self.conn.execute(
                """
                UPDATE steam_request_queue
                SET status = 'cancelled', last_error = 'orphaned_expired_lease',
                    lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = ?, updated_at = ?
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                """,
                (now_iso, now_iso, running_cutoff),
            )
        return {
            "pending": max(0, int(pending.rowcount)),
            "running": max(0, int(running.rowcount)),
        }

    def list_steam_requests(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        if status is None:
            return self.conn.execute(
                "SELECT * FROM steam_request_queue ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return self.conn.execute(
            """
            SELECT * FROM steam_request_queue
            WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (str(status), max(1, int(limit))),
        ).fetchall()

    def get_steam_queue_snapshot(self, *, limit: int = 50) -> dict[str, Any]:
        counts = {
            str(row["status"]): int(row["count"])
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS count FROM steam_request_queue GROUP BY status"
            ).fetchall()
        }
        rows = self.conn.execute(
            """
            SELECT * FROM steam_request_queue
            WHERE status IN ('pending', 'running')
            ORDER BY CASE WHEN status = 'running' THEN 0 ELSE 1 END,
                     priority ASC, available_at ASC, id ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return {"counts": counts, "requests": [dict(row) for row in rows]}

    def list_recent_steam_429_events(
        self,
        since: str | datetime,
        *,
        account_id: str | None = None,
        route: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        where = ["http_status = 429", "completed_at >= ?"]
        params: list[Any] = [_utc_iso(since)]
        if account_id is not None:
            where.append("account_id = ?")
            params.append(str(account_id))
        if route is not None:
            where.append("route = ?")
            params.append(str(route))
        params.append(max(1, int(limit)))
        return self.conn.execute(
            "SELECT * FROM steam_request_queue WHERE "
            + " AND ".join(where)
            + " ORDER BY completed_at DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()

    # ------------------------------------------------------------------
    # Steam route/account/global circuit persistence
    # ------------------------------------------------------------------

    def get_steam_route_circuit(self, circuit_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM steam_route_circuits WHERE circuit_key = ?",
            (str(circuit_key),),
        ).fetchone()

    def upsert_steam_route_circuit(
        self,
        circuit_key: str,
        *,
        scope: str,
        state: str,
        account_id: str | None = None,
        route: str | None = None,
        consecutive_429: int = 0,
        first_429_at: str | datetime | None = None,
        last_429_at: str | datetime | None = None,
        cooldown_until: str | datetime | None = None,
        next_probe_at: str | datetime | None = None,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        key = str(circuit_key or "").strip()
        normalized_scope = str(scope or "").strip()
        normalized_state = str(state or "").strip()
        if not key or not normalized_scope or not normalized_state:
            raise ValueError("circuit_key, scope and state are required")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO steam_route_circuits (
                    circuit_key, scope, account_id, route, state, consecutive_429,
                    first_429_at, last_429_at, cooldown_until, next_probe_at,
                    reason, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(circuit_key) DO UPDATE SET
                    scope = excluded.scope,
                    account_id = excluded.account_id,
                    route = excluded.route,
                    state = excluded.state,
                    consecutive_429 = excluded.consecutive_429,
                    first_429_at = excluded.first_429_at,
                    last_429_at = excluded.last_429_at,
                    cooldown_until = excluded.cooldown_until,
                    next_probe_at = excluded.next_probe_at,
                    reason = excluded.reason,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    normalized_scope,
                    str(account_id) if account_id is not None else None,
                    route,
                    normalized_state,
                    max(0, int(consecutive_429)),
                    _utc_iso(first_429_at) if first_429_at is not None else None,
                    _utc_iso(last_429_at) if last_429_at is not None else None,
                    _utc_iso(cooldown_until) if cooldown_until is not None else None,
                    _utc_iso(next_probe_at) if next_probe_at is not None else None,
                    reason,
                    _json_object(payload),
                    now,
                    now,
                ),
            )
        row = self.get_steam_route_circuit(key)
        if row is None:  # pragma: no cover
            raise RuntimeError(f"Steam circuit was not persisted: {key}")
        return row

    def list_steam_route_circuits(self, *, state: str | None = None) -> list[sqlite3.Row]:
        if state is None:
            return self.conn.execute(
                "SELECT * FROM steam_route_circuits ORDER BY updated_at DESC, circuit_key"
            ).fetchall()
        return self.conn.execute(
            """
            SELECT * FROM steam_route_circuits
            WHERE state = ? ORDER BY updated_at DESC, circuit_key
            """,
            (str(state),),
        ).fetchall()

    def claim_steam_circuit_probe(
        self,
        circuit_key: str,
        worker_id: str,
        *,
        lease_seconds: float = 30,
        now: str | datetime | None = None,
    ) -> sqlite3.Row | None:
        key = str(circuit_key)
        owner = str(worker_id)
        now_iso = _utc_iso(now)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cursor = self.conn.execute(
                """
                UPDATE steam_route_circuits
                SET state = 'half_open', probe_lease_owner = ?,
                    probe_lease_expires_at = ?, updated_at = ?
                WHERE circuit_key = ?
                  AND state IN ('open', 'half_open')
                  AND (next_probe_at IS NULL OR next_probe_at <= ?)
                  AND (probe_lease_expires_at IS NULL OR probe_lease_expires_at <= ?)
                """,
                (
                    owner,
                    _lease_expiry(now_iso, lease_seconds),
                    now_iso,
                    key,
                    now_iso,
                    now_iso,
                ),
            )
            row = self.get_steam_route_circuit(key) if cursor.rowcount == 1 else None
            self.conn.commit()
            return row
        except Exception:
            self.conn.rollback()
            raise

    def release_steam_circuit_probe(
        self,
        circuit_key: str,
        worker_id: str,
        *,
        state: str | None = None,
        cooldown_until: str | datetime | None = None,
        next_probe_at: str | datetime | None = None,
        reason: str | None = None,
    ) -> bool:
        now = utc_now_iso()
        cursor = self.conn.execute(
            """
            UPDATE steam_route_circuits
            SET state = COALESCE(?, state),
                cooldown_until = ?, next_probe_at = ?, reason = COALESCE(?, reason),
                probe_lease_owner = NULL, probe_lease_expires_at = NULL, updated_at = ?
            WHERE circuit_key = ? AND probe_lease_owner = ?
            """,
            (
                state,
                _utc_iso(cooldown_until) if cooldown_until is not None else None,
                _utc_iso(next_probe_at) if next_probe_at is not None else None,
                reason,
                now,
                str(circuit_key),
                str(worker_id),
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Shared C5 API circuit persistence
    # ------------------------------------------------------------------

    def get_c5_api_circuit(self, circuit_key: str = "global_ip_whitelist") -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM c5_api_circuits WHERE circuit_key = ?",
            (str(circuit_key),),
        ).fetchone()

    def trip_c5_api_circuit(
        self,
        *,
        circuit_key: str = "global_ip_whitelist",
        error_code: int,
        request_ip: str | None,
        trigger_source: str | None,
        trigger_operation: str | None,
        next_probe_at: str | datetime,
        payload: dict[str, Any] | None = None,
    ) -> tuple[sqlite3.Row, bool]:
        """Open the shared circuit atomically and report whether this is a new incident."""

        key = str(circuit_key)
        now = utc_now_iso()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            previous = self.get_c5_api_circuit(key)
            newly_opened = previous is None or str(previous["state"]) != "open"
            if previous is None:
                self.conn.execute(
                    """
                    INSERT INTO c5_api_circuits (
                        circuit_key, state, error_code, request_ip,
                        trigger_source, trigger_operation, first_error_at,
                        last_error_at, next_probe_at, payload_json,
                        created_at, updated_at
                    ) VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        int(error_code),
                        request_ip,
                        trigger_source,
                        trigger_operation,
                        now,
                        now,
                        _utc_iso(next_probe_at),
                        _json_object(payload),
                        now,
                        now,
                    ),
                )
            elif newly_opened:
                self.conn.execute(
                    """
                    UPDATE c5_api_circuits
                    SET state = 'open', error_code = ?, request_ip = ?,
                        trigger_source = ?, trigger_operation = ?,
                        first_error_at = ?, last_error_at = ?, next_probe_at = ?,
                        probe_lease_owner = NULL, probe_lease_expires_at = NULL,
                        alert_sent_at = NULL, recovered_at = NULL,
                        recovery_alert_sent_at = NULL, payload_json = ?, updated_at = ?
                    WHERE circuit_key = ?
                    """,
                    (
                        int(error_code),
                        request_ip,
                        trigger_source,
                        trigger_operation,
                        now,
                        now,
                        _utc_iso(next_probe_at),
                        _json_object(payload),
                        now,
                        key,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE c5_api_circuits
                    SET error_code = ?, request_ip = COALESCE(?, request_ip),
                        last_error_at = ?, payload_json = ?, updated_at = ?
                    WHERE circuit_key = ?
                    """,
                    (int(error_code), request_ip, now, _json_object(payload), now, key),
                )
            row = self.get_c5_api_circuit(key)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if row is None:  # pragma: no cover
            raise RuntimeError("C5 API circuit was not persisted")
        return row, newly_opened

    def claim_c5_api_alert(
        self,
        *,
        circuit_key: str = "global_ip_whitelist",
        recovery: bool = False,
    ) -> str | None:
        column = "recovery_alert_sent_at" if recovery else "alert_sent_at"
        expected_state = "closed" if recovery else "open"
        claim = utc_now_iso()
        cursor = self.conn.execute(
            f"""
            UPDATE c5_api_circuits SET {column} = ?, updated_at = ?
            WHERE circuit_key = ? AND state = ? AND {column} IS NULL
            """,
            (claim, claim, str(circuit_key), expected_state),
        )
        self.conn.commit()
        return claim if cursor.rowcount == 1 else None

    def release_c5_api_alert_claim(
        self,
        claim: str,
        *,
        circuit_key: str = "global_ip_whitelist",
        recovery: bool = False,
    ) -> None:
        column = "recovery_alert_sent_at" if recovery else "alert_sent_at"
        with self.conn:
            self.conn.execute(
                f"UPDATE c5_api_circuits SET {column} = NULL, updated_at = ? "
                f"WHERE circuit_key = ? AND {column} = ?",
                (utc_now_iso(), str(circuit_key), str(claim)),
            )

    def claim_c5_api_probe(
        self,
        worker_id: str,
        *,
        circuit_key: str = "global_ip_whitelist",
        lease_seconds: float = 30,
        now: str | datetime | None = None,
    ) -> sqlite3.Row | None:
        now_iso = _utc_iso(now)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cursor = self.conn.execute(
                """
                UPDATE c5_api_circuits
                SET probe_lease_owner = ?, probe_lease_expires_at = ?, updated_at = ?
                WHERE circuit_key = ? AND state = 'open'
                  AND (next_probe_at IS NULL OR next_probe_at <= ?)
                  AND (probe_lease_expires_at IS NULL OR probe_lease_expires_at <= ?)
                """,
                (
                    str(worker_id),
                    _lease_expiry(now_iso, lease_seconds),
                    now_iso,
                    str(circuit_key),
                    now_iso,
                    now_iso,
                ),
            )
            row = self.get_c5_api_circuit(circuit_key) if cursor.rowcount == 1 else None
            self.conn.commit()
            return row
        except Exception:
            self.conn.rollback()
            raise

    def defer_c5_api_probe(
        self,
        worker_id: str,
        *,
        circuit_key: str = "global_ip_whitelist",
        next_probe_at: str | datetime,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE c5_api_circuits
            SET next_probe_at = ?, probe_lease_owner = NULL,
                probe_lease_expires_at = NULL, updated_at = ?
            WHERE circuit_key = ? AND state = 'open' AND probe_lease_owner = ?
            """,
            (_utc_iso(next_probe_at), utc_now_iso(), str(circuit_key), str(worker_id)),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def recover_c5_api_circuit(
        self,
        worker_id: str,
        *,
        circuit_key: str = "global_ip_whitelist",
    ) -> bool:
        now = utc_now_iso()
        cursor = self.conn.execute(
            """
            UPDATE c5_api_circuits
            SET state = 'closed', recovered_at = ?, next_probe_at = NULL,
                probe_lease_owner = NULL, probe_lease_expires_at = NULL, updated_at = ?
            WHERE circuit_key = ? AND state = 'open' AND probe_lease_owner = ?
            """,
            (now, now, str(circuit_key), str(worker_id)),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Guadao issue acknowledgement and configuration audit
    # ------------------------------------------------------------------

    def set_guadao_issue_acknowledgement(
        self,
        issue_key: str,
        *,
        acknowledged: bool,
        reason: str | None = None,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        key = str(issue_key or "").strip()
        if not key:
            raise ValueError("issue_key is required")
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO guadao_issue_acknowledgements (
                    issue_key, acknowledged, reason, actor, acknowledged_at,
                    restored_at, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issue_key) DO UPDATE SET
                    acknowledged = excluded.acknowledged,
                    reason = excluded.reason,
                    actor = excluded.actor,
                    acknowledged_at = excluded.acknowledged_at,
                    restored_at = excluded.restored_at,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    1 if acknowledged else 0,
                    reason,
                    actor,
                    now if acknowledged else None,
                    None if acknowledged else now,
                    _json_object(payload),
                    now,
                    now,
                ),
            )
        row = self.get_guadao_issue_acknowledgement(key)
        if row is None:  # pragma: no cover
            raise RuntimeError(f"guadao issue acknowledgement was not persisted: {key}")
        return row

    def get_guadao_issue_acknowledgement(self, issue_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM guadao_issue_acknowledgements WHERE issue_key = ?",
            (str(issue_key),),
        ).fetchone()

    def list_guadao_issue_acknowledgements(
        self,
        *,
        acknowledged: bool | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        if acknowledged is None:
            return self.conn.execute(
                """
                SELECT * FROM guadao_issue_acknowledgements
                ORDER BY updated_at DESC, issue_key LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return self.conn.execute(
            """
            SELECT * FROM guadao_issue_acknowledgements
            WHERE acknowledged = ? ORDER BY updated_at DESC, issue_key LIMIT ?
            """,
            (1 if acknowledged else 0, max(1, int(limit))),
        ).fetchall()

    def add_strategy_config_audit(
        self,
        *,
        config_scope: str,
        old_value: Any,
        new_value: Any,
        diff: Any = None,
        event_type: str = "update",
        actor: str | None = None,
        reason: str | None = None,
        created_at: str | datetime | None = None,
    ) -> int:
        scope = str(config_scope or "").strip()
        normalized_event = str(event_type or "").strip()
        if not scope or not normalized_event:
            raise ValueError("config_scope and event_type are required")
        cursor = self.conn.execute(
            """
            INSERT INTO strategy_config_audit (
                config_scope, event_type, old_value_json, new_value_json,
                diff_json, actor, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                normalized_event,
                _json_value(old_value),
                _json_value(new_value),
                _json_value(diff if diff is not None else {}),
                actor,
                reason,
                _utc_iso(created_at),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_strategy_config_audit(
        self,
        *,
        config_scope: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        if config_scope is None:
            return self.conn.execute(
                "SELECT * FROM strategy_config_audit ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return self.conn.execute(
            """
            SELECT * FROM strategy_config_audit
            WHERE config_scope = ? ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (str(config_scope), max(1, int(limit))),
        ).fetchall()

    # ------------------------------------------------------------------
    # Pool operations
    # ------------------------------------------------------------------

    def add_pool_operation(
        self,
        *,
        market_hash_name: str,
        strategy: str,
        operation_type: str,
        quantity: int = 1,
        expected_price: float | None = None,
        asset_id: str | None = None,
        note: str | None = None,
    ) -> int:
        now = utc_now_iso()
        cursor = self.conn.execute(
            """
            INSERT INTO pool_operations (
                market_hash_name, strategy, operation_type, status,
                quantity, expected_price, asset_id, note, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (market_hash_name, strategy, operation_type, quantity, expected_price, asset_id, note, now),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def update_pool_operation(
        self,
        op_id: int,
        *,
        status: str | None = None,
        actual_price: float | None = None,
        asset_id: str | None = None,
        note: str | None = None,
    ) -> None:
        parts: list[str] = []
        params: list[Any] = []
        if status is not None:
            parts.append("status = ?")
            params.append(status)
            if status in ("completed", "failed", "skipped", "dry_run", "sold"):
                parts.append("completed_at = ?")
                params.append(utc_now_iso())
        if actual_price is not None:
            parts.append("actual_price = ?")
            params.append(actual_price)
        if asset_id is not None:
            parts.append("asset_id = ?")
            params.append(asset_id)
        if note is not None:
            parts.append("note = ?")
            params.append(note)
        if not parts:
            return
        params.append(op_id)
        self.conn.execute(
            f"UPDATE pool_operations SET {', '.join(parts)} WHERE id = ?",
            tuple(params),
        )
        self.conn.commit()

    def list_pool_operations(self, status: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        sql = "SELECT * FROM pool_operations"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params = (*params, limit)
        return self.conn.execute(sql, params).fetchall()

    def list_pool_operations_by_type(
        self,
        operation_type: str,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM pool_operations WHERE operation_type = ?"
        params: list[Any] = [operation_type]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, tuple(params)).fetchall()

    def list_pool_operations_by_type_and_statuses(
        self,
        operation_type: str,
        *,
        statuses: list[str],
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        params: list[Any] = [operation_type, *statuses, limit]
        sql = (
            "SELECT * FROM pool_operations WHERE operation_type = ? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT ?"
        )
        return self.conn.execute(sql, tuple(params)).fetchall()

    def list_required_market_hash_names(self) -> list[str]:
        cursor = self.conn.execute(
            """
            SELECT market_hash_name FROM watch_items WHERE enabled = 1
            UNION
            SELECT bi.market_hash_name
            FROM basket_items bi
            JOIN baskets b ON b.id = bi.basket_id
            WHERE b.enabled = 1
            ORDER BY market_hash_name ASC
            """
        )
        return [row["market_hash_name"] for row in cursor.fetchall()]

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
