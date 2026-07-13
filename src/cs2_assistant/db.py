from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cs2_assistant.models import CatalogItem, MarketState, POOL_STATUS_HOLDING
from cs2_assistant.utils import ensure_parent_dir, utc_now_iso


PROFIT_TRADE_OBSERVABILITY_TABLES = frozenset(
    {
        "profit_trade_roi_watch",
        "profit_trade_roi_observations",
        "profit_trade_state_events",
        "profit_trade_acknowledgements",
    }
)


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
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

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

    def initialize(self) -> None:
        self._backup_before_profit_trade_observability_upgrade()
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
                active INTEGER NOT NULL DEFAULT 1,
                steam_buy_price REAL,
                steam_price_source TEXT,
                c5_listing_price REAL,
                c5_price_source TEXT,
                c5_expected_net_price REAL,
                balance_discount REAL,
                expected_profit REAL,
                expected_roi REAL,
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
                c5_listing_price REAL,
                c5_expected_net_price REAL,
                balance_discount REAL,
                expected_profit REAL,
                expected_roi REAL,
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
            """
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

    def search_items(self, keyword: str, limit: int = 20) -> list[sqlite3.Row]:
        like = f"%{keyword}%"
        cursor = self.conn.execute(
            """
            SELECT market_hash_name, name_cn, c5_item_id
            FROM items
            WHERE name_cn LIKE ? OR market_hash_name LIKE ?
            ORDER BY name_cn ASC
            LIMIT ?
            """,
            (like, like, limit),
        )
        return cursor.fetchall()

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

    def update_profit_trade(
        self,
        trade_id: int,
        **fields: Any,
    ) -> None:
        event_reason = fields.pop("_event_reason", None)
        event_context = fields.pop("_event_context", None)
        log_event_id = fields.pop("_log_event_id", None)
        allowed = {
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
        parts: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            parts.append(f"{key} = ?")
            params.append(value)
        if not parts:
            return
        current = self.get_profit_trade(trade_id)
        if current is None:
            return
        now = utc_now_iso()
        parts.append("updated_at = ?")
        params.append(now)
        status = fields.get("status")
        if status in {"completed", "failed", "manual_required", "cancelled"} and "completed_at" not in fields:
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
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM profit_trades"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------
    # Profit Trade observability
    # ------------------------------------------------------------------

    @staticmethod
    def _profit_trade_roi_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        is_active = bool(row["active"]) if "active" in keys else None
        execution_status_code = str(row["execution_status"] or "watch_only")
        if is_active is False:
            execution_status = "exited"
        elif execution_status_code == "executable":
            execution_status = "executable"
        elif execution_status_code == "manual_review":
            execution_status = "manual_review"
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
            "steamPriceSource": row["steam_price_source"] if "steam_price_source" in keys else None,
            "c5ListingPrice": row["c5_listing_price"],
            "c5PriceSource": row["c5_price_source"] if "c5_price_source" in keys else None,
            "c5ExpectedNetPrice": row["c5_expected_net_price"],
            "balanceDiscount": row["balance_discount"],
            "expectedProfit": row["expected_profit"],
            "expectedRoi": row["expected_roi"],
            "expectedRoiPct": (
                float(row["expected_roi"]) * 100.0
                if row["expected_roi"] is not None
                else None
            ),
            "minRoi": row["min_roi"],
            "manualReviewRoi": row["manual_review_roi"],
            "inventoryCount": row["inventory_count"],
            "tradableCount": row["tradable_count"],
            "c5RecentSoldNetPrice": (
                row["c5_recent_sold_net_price"] if "c5_recent_sold_net_price" in keys else None
            ),
            "c5RecentSoldCount": (
                row["c5_recent_sold_count"] if "c5_recent_sold_count" in keys else None
            ),
            "c5CurrentSellPrice": (
                row["c5_current_sell_price"] if "c5_current_sell_price" in keys else None
            ),
            "c5OnSaleCount": row["c5_on_sale_count"] if "c5_on_sale_count" in keys else None,
            "c5PurchaseMaxPrice": (
                row["c5_purchase_max_price"] if "c5_purchase_max_price" in keys else None
            ),
            "c5PurchaseCount": row["c5_purchase_count"] if "c5_purchase_count" in keys else None,
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

    def record_profit_trade_roi_scan(
        self,
        observations: list[dict[str, Any]],
        *,
        scan_id: str,
        observed_at: str | None = None,
        exit_reasons: dict[str, str] | None = None,
        exit_observations: dict[str, dict[str, Any]] | None = None,
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
            if not market_hash_name or expected_roi <= 0:
                continue
            row = {
                "market_hash_name": market_hash_name,
                "name_cn": raw.get("name_cn"),
                "steam_buy_price": raw.get("steam_buy_price"),
                "steam_price_source": raw.get("steam_price_source"),
                "c5_listing_price": raw.get("c5_listing_price"),
                "c5_price_source": raw.get("c5_price_source"),
                "c5_expected_net_price": raw.get("c5_expected_net_price"),
                "balance_discount": raw.get("balance_discount"),
                "expected_profit": raw.get("expected_profit"),
                "expected_roi": expected_roi,
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
                "raw_json": json.dumps(raw.get("raw") or {}, ensure_ascii=False),
            }
            normalized.append(row)
            active_names.add(market_hash_name)

        exit_reasons = exit_reasons or {}
        exit_observations = exit_observations or {}
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
                        active,
                        steam_buy_price,
                        steam_price_source,
                        c5_listing_price,
                        c5_price_source,
                        c5_expected_net_price,
                        balance_discount,
                        expected_profit,
                        expected_roi,
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
                        1,
                        :steam_buy_price,
                        :steam_price_source,
                        :c5_listing_price,
                        :c5_price_source,
                        :c5_expected_net_price,
                        :balance_discount,
                        :expected_profit,
                        :expected_roi,
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
                        active = 1,
                        steam_buy_price = excluded.steam_buy_price,
                        steam_price_source = excluded.steam_price_source,
                        c5_listing_price = excluded.c5_listing_price,
                        c5_price_source = excluded.c5_price_source,
                        c5_expected_net_price = excluded.c5_expected_net_price,
                        balance_discount = excluded.balance_discount,
                        expected_profit = excluded.expected_profit,
                        expected_roi = excluded.expected_roi,
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
                    {**row, "timestamp": timestamp},
                )
                self.conn.execute(
                    """
                    INSERT INTO profit_trade_roi_observations (
                        scan_id,
                        market_hash_name,
                        event_type,
                        observed_at,
                        steam_buy_price,
                        c5_listing_price,
                        c5_expected_net_price,
                        balance_discount,
                        expected_profit,
                        expected_roi,
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
                        :c5_listing_price,
                        :c5_expected_net_price,
                        :balance_discount,
                        :expected_profit,
                        :expected_roi,
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
                reason = str(
                    exit_reasons.get(market_hash_name)
                    or "not profitable or unavailable in the latest completed scan"
                )
                snapshot = exit_observations.get(market_hash_name) or {}

                def exit_value(key: str, column: str) -> Any:
                    value = snapshot.get(key)
                    return watch_row[column] if value is None else value

                exit_values = {
                    "steam_buy_price": exit_value("steam_buy_price", "steam_buy_price"),
                    "c5_listing_price": exit_value("c5_listing_price", "c5_listing_price"),
                    "c5_expected_net_price": exit_value(
                        "c5_expected_net_price", "c5_expected_net_price"
                    ),
                    "balance_discount": exit_value("balance_discount", "balance_discount"),
                    "expected_profit": exit_value("expected_profit", "expected_profit"),
                    "expected_roi": exit_value("expected_roi", "expected_roi"),
                    "min_roi": exit_value("min_roi", "min_roi"),
                    "manual_review_roi": exit_value("manual_review_roi", "manual_review_roi"),
                    "inventory_count": exit_value("inventory_count", "inventory_count"),
                    "tradable_count": exit_value("tradable_count", "tradable_count"),
                    "risk_status": exit_value("risk_status", "risk_status"),
                    "risk_reason": exit_value("risk_reason", "risk_reason"),
                    "execution_status": exit_value("execution_status", "execution_status"),
                    "execution_reason": exit_value("execution_reason", "execution_reason"),
                    "raw_json": (
                        json.dumps(snapshot.get("raw") or {}, ensure_ascii=False)
                        if snapshot
                        else watch_row["raw_json"]
                    ),
                }
                self.conn.execute(
                    """
                    UPDATE profit_trade_roi_watch
                    SET active = 0,
                        steam_buy_price = ?,
                        c5_listing_price = ?,
                        c5_expected_net_price = ?,
                        balance_discount = ?,
                        expected_profit = ?,
                        expected_roi = ?,
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
                        exit_values["c5_listing_price"],
                        exit_values["c5_expected_net_price"],
                        exit_values["balance_discount"],
                        exit_values["expected_profit"],
                        exit_values["expected_roi"],
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
                        c5_listing_price,
                        c5_expected_net_price,
                        balance_discount,
                        expected_profit,
                        expected_roi,
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
                    ) VALUES (?, ?, 'exited', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id,
                        market_hash_name,
                        timestamp,
                        exit_values["steam_buy_price"],
                        exit_values["c5_listing_price"],
                        exit_values["c5_expected_net_price"],
                        exit_values["balance_discount"],
                        exit_values["expected_profit"],
                        exit_values["expected_roi"],
                        exit_values["min_roi"],
                        exit_values["manual_review_roi"],
                        exit_values["inventory_count"],
                        exit_values["tradable_count"],
                        exit_values["risk_status"],
                        exit_values["risk_reason"],
                        exit_values["execution_status"],
                        exit_values["execution_reason"],
                        reason,
                        exit_values["raw_json"],
                    ),
                )
                exited += 1
        return {"inserted": inserted, "updated": updated, "exited": exited}

    def list_profit_trade_roi_watch(
        self,
        *,
        active: bool | None = True,
        keyword: str | None = None,
        execution_status: str | None = None,
        sort: str = "roi_desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
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
        return {
            "items": [self._profit_trade_roi_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
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
