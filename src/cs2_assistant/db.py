from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from cs2_assistant.models import CatalogItem, MarketState
from cs2_assistant.utils import ensure_parent_dir, utc_now_iso


class Database:
    def __init__(self, path: Path):
        self.path = path
        ensure_parent_dir(path)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def initialize(self) -> None:
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
            """
        )
        self.conn.commit()

    def upsert_items(self, items: Iterable[CatalogItem]) -> int:
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
        self.conn.executemany(
            """
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
                c5_item_id = excluded.c5_item_id,
                steam_item_id = excluded.steam_item_id,
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
