from __future__ import annotations

import importlib
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from cs2_assistant.config import Settings
from cs2_assistant.db import Database
from cs2_assistant.services.pricing import build_orderbook_snapshot
from cs2_assistant.services.strategy import load_strategy_config
from cs2_assistant.utils import safe_float, utc_now_iso


_JOB_TABLE = "c5_research_scan_jobs"
_RESULT_TABLE = "c5_research_scan_results"
_TERMINAL_JOB_STATUSES = frozenset(
    {"completed", "completed_with_errors", "failed", "cancelled"}
)
_RUNNABLE_JOB_STATUSES = frozenset({"queued", "retry"})
_DEFAULT_RETRY_SECONDS = 5.0 * 60.0
_MAX_CHUNK_SIZE = 500
_C5_BATCH_SIZE = 100


def initialize_c5_research_schema(settings: Settings) -> None:
    """Create the isolated research tables without initializing trading schema.

    The service intentionally opens :class:`Database` only for its configured
    SQLite connection.  It deliberately avoids the database-wide schema
    initializer because that method owns the application's trading schema.
    """

    db = Database(settings.db_path)
    try:
        db.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {_JOB_TABLE} (
                request_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                control_action TEXT,
                filters_json TEXT NOT NULL DEFAULT '{{}}',
                matched_count INTEGER NOT NULL DEFAULT 0,
                processed_count INTEGER NOT NULL DEFAULT 0,
                observed_count INTEGER NOT NULL DEFAULT 0,
                filtered_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                cursor INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_c5_research_scan_jobs_status
            ON {_JOB_TABLE}(status, next_attempt_at, created_at);

            CREATE TABLE IF NOT EXISTS {_RESULT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                catalog_index INTEGER NOT NULL,
                market_hash_name TEXT NOT NULL,
                name_cn TEXT,
                taxonomy_json TEXT NOT NULL DEFAULT '{{}}',
                status TEXT NOT NULL DEFAULT 'pending',
                c5_listing_price REAL,
                c5_price_source TEXT,
                c5_error TEXT,
                steam_sell_price REAL,
                steam_price_source TEXT,
                steam_error TEXT,
                orderbook_json TEXT NOT NULL DEFAULT '{{}}',
                c5_expected_net_price REAL,
                balance_discount REAL,
                expected_profit REAL,
                expected_roi REAL,
                observed_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(request_id, market_hash_name),
                FOREIGN KEY (request_id) REFERENCES {_JOB_TABLE}(request_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_c5_research_scan_results_cursor
            ON {_RESULT_TABLE}(request_id, status, catalog_index);

            CREATE INDEX IF NOT EXISTS idx_c5_research_scan_results_roi
            ON {_RESULT_TABLE}(request_id, expected_roi DESC, catalog_index);
            """
        )
        db.conn.commit()
    finally:
        db.close()


def create_c5_research_scan(
    settings: Settings,
    filters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Freeze a catalog match set and return HTTP-202-compatible queue data."""

    initialize_c5_research_schema(settings)
    normalized_filters = _normalize_filters(filters)
    catalog_payload = _filter_catalog_items(settings, normalized_filters)
    catalog_items = _normalize_catalog_items(catalog_payload)
    request_id = f"C5RS-{uuid.uuid4().hex}"
    now = utc_now_iso()

    db = Database(settings.db_path)
    try:
        with db.conn:
            db.conn.execute(
                f"""
                INSERT INTO {_JOB_TABLE} (
                    request_id, status, filters_json, matched_count,
                    processed_count, observed_count, filtered_count, error_count,
                    cursor, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, 0, 0, 0, 0, 0, ?, ?)
                """,
                (
                    request_id,
                    _json_dump(normalized_filters),
                    len(catalog_items),
                    now,
                    now,
                ),
            )
            db.conn.executemany(
                f"""
                INSERT INTO {_RESULT_TABLE} (
                    request_id, catalog_index, market_hash_name, name_cn,
                    taxonomy_json, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                [
                    (
                        request_id,
                        index,
                        item["market_hash_name"],
                        item.get("name_cn"),
                        _json_dump(item["taxonomy"]),
                        now,
                    )
                    for index, item in enumerate(catalog_items)
                ],
            )
        row = _get_job_row(db, request_id)
        if row is None:  # pragma: no cover - protected by the successful INSERT
            raise RuntimeError("C5 research scan was not persisted")
        payload = _job_to_public(row)
    finally:
        db.close()

    payload.update(
        {
            "httpStatus": 202,
            "accepted": True,
            "queued": True,
        }
    )
    return payload


def get_c5_research_scan(settings: Settings, request_id: str) -> dict[str, Any]:
    initialize_c5_research_schema(settings)
    normalized_id = _normalize_request_id(request_id)
    db = Database(settings.db_path)
    try:
        row = _get_job_row(db, normalized_id)
        if row is None:
            raise LookupError(f"C5 research scan not found: {normalized_id}")
        return _job_to_public(row)
    finally:
        db.close()


def list_c5_research_results(
    settings: Settings,
    request_id: str,
    page: int = 1,
    page_size: int = 50,
    sort: str = "roi_desc",
) -> dict[str, Any]:
    initialize_c5_research_schema(settings)
    normalized_id = _normalize_request_id(request_id)
    safe_page = max(1, int(page))
    safe_page_size = min(500, max(1, int(page_size)))
    normalized_sort = str(sort or "roi_desc").strip().lower()
    order_by = {
        "catalog": "catalog_index ASC, id ASC",
        "updated_desc": "updated_at DESC, catalog_index ASC",
        "roi_asc": (
            "CASE WHEN expected_roi IS NULL THEN 1 ELSE 0 END ASC, "
            "expected_roi ASC, catalog_index ASC"
        ),
        "c5_price_asc": (
            "CASE WHEN c5_listing_price IS NULL THEN 1 ELSE 0 END ASC, "
            "c5_listing_price ASC, catalog_index ASC"
        ),
        "c5_price_desc": (
            "CASE WHEN c5_listing_price IS NULL THEN 1 ELSE 0 END ASC, "
            "c5_listing_price DESC, catalog_index ASC"
        ),
        "steam_price_asc": (
            "CASE WHEN steam_sell_price IS NULL THEN 1 ELSE 0 END ASC, "
            "steam_sell_price ASC, catalog_index ASC"
        ),
        "steam_price_desc": (
            "CASE WHEN steam_sell_price IS NULL THEN 1 ELSE 0 END ASC, "
            "steam_sell_price DESC, catalog_index ASC"
        ),
        "roi_desc": (
            "CASE WHEN expected_roi IS NULL THEN 1 ELSE 0 END ASC, "
            "expected_roi DESC, catalog_index ASC"
        ),
    }.get(normalized_sort)
    if order_by is None:
        raise ValueError(
            "sort must be catalog, updated_desc, roi_asc, roi_desc, "
            "c5_price_asc, c5_price_desc, steam_price_asc, or steam_price_desc"
        )

    db = Database(settings.db_path)
    try:
        if _get_job_row(db, normalized_id) is None:
            raise LookupError(f"C5 research scan not found: {normalized_id}")
        total = int(
            db.conn.execute(
                f"SELECT COUNT(*) FROM {_RESULT_TABLE} WHERE request_id = ?",
                (normalized_id,),
            ).fetchone()[0]
        )
        rows = db.conn.execute(
            f"""
            SELECT *
            FROM {_RESULT_TABLE}
            WHERE request_id = ?
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (
                normalized_id,
                safe_page_size,
                (safe_page - 1) * safe_page_size,
            ),
        ).fetchall()
    finally:
        db.close()

    return {
        "ok": True,
        "requestId": normalized_id,
        "researchOnly": True,
        "canExecute": False,
        "page": safe_page,
        "pageSize": safe_page_size,
        "total": total,
        "sort": normalized_sort,
        "items": [_result_to_public(row) for row in rows],
    }


def set_c5_research_scan_action(
    settings: Settings,
    request_id: str,
    action: str,
) -> dict[str, Any]:
    initialize_c5_research_schema(settings)
    normalized_id = _normalize_request_id(request_id)
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"pause", "resume", "cancel"}:
        raise ValueError("action must be pause, resume, or cancel")

    db = Database(settings.db_path)
    try:
        now = utc_now_iso()
        with db.conn:
            row = _get_job_row(db, normalized_id)
            if row is None:
                raise LookupError(f"C5 research scan not found: {normalized_id}")
            status = str(row["status"] or "")
            changed = False
            if normalized_action == "pause":
                if status == "running":
                    changed = db.conn.execute(
                        f"""
                        UPDATE {_JOB_TABLE}
                        SET control_action = 'pause', updated_at = ?
                        WHERE request_id = ? AND status = 'running'
                        """,
                        (now, normalized_id),
                    ).rowcount == 1
                elif status not in _TERMINAL_JOB_STATUSES and status != "paused":
                    changed = db.conn.execute(
                        f"""
                        UPDATE {_JOB_TABLE}
                        SET status = 'paused', control_action = NULL,
                            next_attempt_at = NULL, updated_at = ?
                        WHERE request_id = ?
                        """,
                        (now, normalized_id),
                    ).rowcount == 1
            elif normalized_action == "resume":
                if status == "running" and str(row["control_action"] or "") == "pause":
                    changed = db.conn.execute(
                        f"""
                        UPDATE {_JOB_TABLE}
                        SET control_action = NULL, updated_at = ?
                        WHERE request_id = ? AND status = 'running'
                        """,
                        (now, normalized_id),
                    ).rowcount == 1
                elif status in {"paused", "retry", "failed"}:
                    changed = db.conn.execute(
                        f"""
                        UPDATE {_JOB_TABLE}
                        SET status = 'queued', control_action = NULL,
                            last_error = NULL, next_attempt_at = NULL,
                            completed_at = NULL, updated_at = ?
                        WHERE request_id = ?
                        """,
                        (now, normalized_id),
                    ).rowcount == 1
            else:
                if status == "running":
                    changed = db.conn.execute(
                        f"""
                        UPDATE {_JOB_TABLE}
                        SET control_action = 'cancel', updated_at = ?
                        WHERE request_id = ? AND status = 'running'
                        """,
                        (now, normalized_id),
                    ).rowcount == 1
                elif status not in _TERMINAL_JOB_STATUSES:
                    changed = db.conn.execute(
                        f"""
                        UPDATE {_JOB_TABLE}
                        SET status = 'cancelled', control_action = NULL,
                            next_attempt_at = NULL, completed_at = ?, updated_at = ?
                        WHERE request_id = ?
                        """,
                        (now, now, normalized_id),
                    ).rowcount == 1
            current = _get_job_row(db, normalized_id)
        if current is None:  # pragma: no cover
            raise RuntimeError("C5 research scan disappeared during action update")
        payload = _job_to_public(current)
        payload.update({"action": normalized_action, "changed": changed})
        return payload
    finally:
        db.close()


def run_c5_research_scan_chunk(
    settings: Settings,
    request_id: str,
    chunk_size: int = 50,
    market_service: Any | None = None,
    *,
    steam_client: Any | None = None,
) -> dict[str, Any]:
    """Run one bounded research chunk.

    The remote sequence is deliberately one-way and read-only:

    1. The catalog match set was already frozen by ``create``.
    2. Every C5 batch request for this chunk completes before any Steam call.
    3. Only candidates inside the requested C5 price range reach orderbook.

    No execution evaluator, asset reservation, transaction row, or market
    mutation method is reachable from this function.
    """

    initialize_c5_research_schema(settings)
    normalized_id = _normalize_request_id(request_id)
    safe_chunk_size = min(_MAX_CHUNK_SIZE, max(1, int(chunk_size)))
    claimed = _claim_job_chunk(settings, normalized_id)
    if not claimed.get("claimed"):
        return claimed["job"]

    if market_service is None:
        try:
            market_service = _build_default_market_service(settings, normalized_id)
        except Exception as exc:
            return _fail_job(settings, normalized_id, f"market service unavailable: {exc}")

    db = Database(settings.db_path)
    try:
        rows = db.conn.execute(
            f"""
            SELECT *
            FROM {_RESULT_TABLE}
            WHERE request_id = ? AND status IN ('pending', 'retry')
            ORDER BY catalog_index ASC, id ASC
            LIMIT ?
            """,
            (normalized_id, safe_chunk_size),
        ).fetchall()
        job_row = _get_job_row(db, normalized_id)
        if job_row is None:
            raise LookupError(f"C5 research scan not found: {normalized_id}")
        filters = _json_load_object(job_row["filters_json"])
    finally:
        db.close()

    if not rows:
        return _finish_or_requeue(settings, normalized_id)

    c5_client = getattr(market_service, "c5_client", None)
    selected_steam_client = steam_client or _first_steam_client(market_service)
    names = [str(row["market_hash_name"] or "") for row in rows]

    # Finish the complete C5 phase before the first Steam request.  Per-batch
    # failures are remembered and projected onto only the affected rows.
    c5_payloads: dict[str, Any] = {}
    c5_errors: dict[str, str] = {}
    if c5_client is None or not callable(getattr(c5_client, "price_batch", None)):
        c5_errors.update({name: "C5 price_batch client is unavailable" for name in names})
    else:
        for batch in _chunked(names, _C5_BATCH_SIZE):
            control = _apply_requested_control(settings, normalized_id)
            if control is not None:
                return control
            try:
                raw_batch = c5_client.price_batch(batch, app_id=settings.app_id)
                c5_payloads.update(_normalize_c5_batch_payload(raw_batch, expected_names=batch))
            except Exception as exc:
                message = _safe_error(exc)
                if _is_rate_limited(exc):
                    return _set_retry(
                        settings,
                        normalized_id,
                        cursor=int(rows[0]["catalog_index"]),
                        error=f"C5 price_batch rate limited: {message}",
                        retry_after=_exception_retry_after(exc),
                    )
                c5_errors.update({name: message for name in batch})

    config = load_strategy_config(settings)
    price_min = _positive_or_zero(filters.get("priceMin"))
    price_max = _positive_or_zero(filters.get("priceMax"))
    net_factor = float(config.profit_trade_c5_current_sale_net_factor)
    balance_discount = float(config.profit_trade_balance_discount)

    for row in rows:
        control = _apply_requested_control(settings, normalized_id)
        if control is not None:
            return control
        market_hash_name = str(row["market_hash_name"] or "")
        catalog_index = int(row["catalog_index"])
        now = utc_now_iso()
        c5_error = c5_errors.get(market_hash_name)
        if c5_error:
            _write_result(
                settings,
                row,
                status="c5_error",
                c5_error=c5_error,
                observed_at=now,
            )
            _refresh_job_progress(settings, normalized_id)
            continue

        c5_payload = c5_payloads.get(market_hash_name)
        c5_price = _extract_c5_price(c5_payload)
        if c5_price is None:
            _write_result(
                settings,
                row,
                status="c5_price_unavailable",
                c5_error="C5 price_batch returned no usable price",
                observed_at=now,
            )
            _refresh_job_progress(settings, normalized_id)
            continue

        if (
            (price_min is not None and c5_price < price_min)
            or (price_max is not None and c5_price > price_max)
        ):
            _write_result(
                settings,
                row,
                status="c5_filtered_out",
                c5_listing_price=c5_price,
                c5_price_source="c5_price_batch",
                observed_at=now,
            )
            _refresh_job_progress(settings, normalized_id)
            continue

        if selected_steam_client is None or not callable(
            getattr(selected_steam_client, "order_book", None)
        ):
            _write_result(
                settings,
                row,
                status="steam_error",
                c5_listing_price=c5_price,
                c5_price_source="c5_price_batch",
                steam_error="Steam orderbook client is unavailable",
                observed_at=now,
            )
            _refresh_job_progress(settings, normalized_id)
            continue

        try:
            orderbook_payload = selected_steam_client.order_book(
                app_id=settings.app_id,
                market_hash_name=market_hash_name,
            )
            if not isinstance(orderbook_payload, dict):
                raise RuntimeError("Steam orderbook returned a non-object payload")
            snapshot = build_orderbook_snapshot(
                orderbook_payload,
                observed_at=now,
                depth=5,
                expected_currency=23,
            )
        except Exception as exc:
            message = _safe_error(exc)
            if _is_rate_limited(exc):
                _write_result(
                    settings,
                    row,
                    status="retry",
                    c5_listing_price=c5_price,
                    c5_price_source="c5_price_batch",
                    steam_error=message,
                    observed_at=None,
                )
                _refresh_job_progress(settings, normalized_id)
                return _set_retry(
                    settings,
                    normalized_id,
                    cursor=catalog_index,
                    error=f"Steam orderbook rate limited: {message}",
                    retry_after=_exception_retry_after(exc),
                )
            _write_result(
                settings,
                row,
                status="steam_error",
                c5_listing_price=c5_price,
                c5_price_source="c5_price_batch",
                steam_error=message,
                observed_at=now,
            )
            _refresh_job_progress(settings, normalized_id)
            continue

        if snapshot.get("currencyValid") is False:
            _write_result(
                settings,
                row,
                status="currency_invalid",
                c5_listing_price=c5_price,
                c5_price_source="c5_price_batch",
                steam_error=(
                    "Steam orderbook currency must be CNY (23), got "
                    f"{snapshot.get('currencyId')!r}"
                ),
                orderbook=snapshot,
                observed_at=now,
            )
            _refresh_job_progress(settings, normalized_id)
            continue

        steam_price = safe_float(snapshot.get("sellerFloorPrice"))
        if steam_price is None or steam_price <= 0:
            _write_result(
                settings,
                row,
                status="orderbook_empty",
                c5_listing_price=c5_price,
                c5_price_source="c5_price_batch",
                steam_error="Steam orderbook returned no usable sell price",
                orderbook=snapshot,
                observed_at=now,
            )
            _refresh_job_progress(settings, normalized_id)
            continue

        c5_expected_net_price = float(c5_price) * net_factor
        expected_profit = c5_expected_net_price - float(steam_price) * balance_discount
        expected_roi = c5_expected_net_price / float(steam_price) - balance_discount
        _write_result(
            settings,
            row,
            status="observed",
            c5_listing_price=c5_price,
            c5_price_source="c5_price_batch",
            steam_sell_price=float(steam_price),
            steam_price_source="steam_orderbook",
            orderbook=snapshot,
            c5_expected_net_price=c5_expected_net_price,
            balance_discount=balance_discount,
            expected_profit=expected_profit,
            expected_roi=expected_roi,
            observed_at=now,
        )
        _refresh_job_progress(settings, normalized_id)

    return _finish_or_requeue(settings, normalized_id)


def _filter_catalog_items(
    settings: Settings,
    filters: dict[str, Any],
) -> Any:
    """Late-bind the catalog service so this module remains independently testable."""

    module = importlib.import_module("cs2_assistant.services.c5_catalog_taxonomy")
    filter_items = getattr(module, "filter_c5_catalog_items", None)
    if not callable(filter_items):
        raise RuntimeError("c5_catalog_taxonomy.filter_c5_catalog_items is unavailable")
    return filter_items(settings, filters)


def _build_default_market_service(settings: Settings, request_id: str) -> Any:
    # Import lazily to avoid making this isolated service part of Profit Trade's
    # import graph.  The production constructor supplies only C5 price_batch and
    # official Steam market clients; relogin is explicitly disabled.
    from cs2_assistant.services.profit_trade import _build_profit_trade_market_service

    return _build_profit_trade_market_service(
        settings,
        telemetry_context={"run_id": request_id, "component": "c5_research_scan"},
        allow_relogin=False,
    )


def _normalize_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    if filters is None:
        result: dict[str, Any] = {}
    elif isinstance(filters, Mapping):
        result = dict(filters)
    else:
        raise TypeError("filters must be an object")

    for public_key, aliases in (
        ("priceMin", ("priceMin", "price_min")),
        ("priceMax", ("priceMax", "price_max")),
    ):
        value: Any = None
        found = False
        for alias in aliases:
            if alias in result:
                value = result.pop(alias)
                found = True
                break
        if found:
            normalized = _optional_nonnegative_float(value, field=public_key)
            if normalized is not None:
                result[public_key] = normalized
    price_min = safe_float(result.get("priceMin"))
    price_max = safe_float(result.get("priceMax"))
    if price_min is not None and price_max is not None and price_min > price_max:
        raise ValueError("priceMin must not exceed priceMax")

    # JSON round-tripping both validates persistence and detaches mutable input.
    return _json_load_object(_json_dump(result))


def _normalize_catalog_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        raw_items = payload.get("items") or []
    else:
        raw_items = payload
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, Iterable) or isinstance(raw_items, (str, bytes, Mapping)):
        raise TypeError("filter_c5_catalog_items must return a list or an object with items")

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if isinstance(raw, Mapping):
            item = dict(raw)
        else:
            try:
                item = dict(raw)
            except (TypeError, ValueError):
                continue
        market_hash_name = str(
            item.get("market_hash_name")
            or item.get("marketHashName")
            or ""
        ).strip()
        if not market_hash_name or market_hash_name in seen:
            continue
        seen.add(market_hash_name)
        name_cn = str(
            item.get("name_cn")
            or item.get("nameCn")
            or item.get("name")
            or item.get("displayName")
            or market_hash_name
        ).strip()
        taxonomy = _json_load_object(_json_dump(item))
        result.append(
            {
                "market_hash_name": market_hash_name,
                "name_cn": name_cn or market_hash_name,
                "taxonomy": taxonomy,
            }
        )
    return result


def _claim_job_chunk(settings: Settings, request_id: str) -> dict[str, Any]:
    db = Database(settings.db_path)
    try:
        db.conn.execute("BEGIN IMMEDIATE")
        row = _get_job_row(db, request_id)
        if row is None:
            db.conn.rollback()
            raise LookupError(f"C5 research scan not found: {request_id}")
        status = str(row["status"] or "")
        if status == "retry" and not _retry_is_due(row["next_attempt_at"]):
            db.conn.commit()
            return {"claimed": False, "job": _job_to_public(row)}
        if status not in _RUNNABLE_JOB_STATUSES:
            db.conn.commit()
            payload = _job_to_public(row)
            if status == "running":
                payload["alreadyRunning"] = True
            return {"claimed": False, "job": payload}
        now = utc_now_iso()
        changed = db.conn.execute(
            f"""
            UPDATE {_JOB_TABLE}
            SET status = 'running', control_action = NULL,
                started_at = COALESCE(started_at, ?),
                next_attempt_at = NULL, updated_at = ?
            WHERE request_id = ? AND status = ?
            """,
            (now, now, request_id, status),
        ).rowcount
        db.conn.commit()
        if changed != 1:
            current = _get_job_row(db, request_id)
            if current is None:  # pragma: no cover
                raise LookupError(f"C5 research scan not found: {request_id}")
            return {"claimed": False, "job": _job_to_public(current)}
        return {"claimed": True}
    except Exception:
        if db.conn.in_transaction:
            db.conn.rollback()
        raise
    finally:
        db.close()


def _apply_requested_control(
    settings: Settings,
    request_id: str,
) -> dict[str, Any] | None:
    db = Database(settings.db_path)
    try:
        row = _get_job_row(db, request_id)
        if row is None:
            raise LookupError(f"C5 research scan not found: {request_id}")
        action = str(row["control_action"] or "")
        status = str(row["status"] or "")
        if status != "running":
            return _job_to_public(row)
        if action not in {"pause", "cancel"}:
            return None
        now = utc_now_iso()
        target = "paused" if action == "pause" else "cancelled"
        completed_at = now if target == "cancelled" else None
        with db.conn:
            db.conn.execute(
                f"""
                UPDATE {_JOB_TABLE}
                SET status = ?, control_action = NULL,
                    next_attempt_at = NULL, completed_at = ?, updated_at = ?
                WHERE request_id = ? AND status = 'running'
                """,
                (target, completed_at, now, request_id),
            )
        current = _get_job_row(db, request_id)
        return _job_to_public(current) if current is not None else None
    finally:
        db.close()


def _write_result(
    settings: Settings,
    row: Any,
    *,
    status: str,
    c5_listing_price: float | None = None,
    c5_price_source: str | None = None,
    c5_error: str | None = None,
    steam_sell_price: float | None = None,
    steam_price_source: str | None = None,
    steam_error: str | None = None,
    orderbook: Mapping[str, Any] | None = None,
    c5_expected_net_price: float | None = None,
    balance_discount: float | None = None,
    expected_profit: float | None = None,
    expected_roi: float | None = None,
    observed_at: str | None = None,
) -> None:
    db = Database(settings.db_path)
    try:
        now = utc_now_iso()
        with db.conn:
            db.conn.execute(
                f"""
                UPDATE {_RESULT_TABLE}
                SET status = ?,
                    c5_listing_price = ?, c5_price_source = ?, c5_error = ?,
                    steam_sell_price = ?, steam_price_source = ?, steam_error = ?,
                    orderbook_json = ?, c5_expected_net_price = ?,
                    balance_discount = ?, expected_profit = ?, expected_roi = ?,
                    observed_at = ?, updated_at = ?
                WHERE id = ? AND request_id = ?
                """,
                (
                    status,
                    _finite_or_none(c5_listing_price),
                    c5_price_source,
                    c5_error,
                    _finite_or_none(steam_sell_price),
                    steam_price_source,
                    steam_error,
                    _json_dump(dict(orderbook or {})),
                    _finite_or_none(c5_expected_net_price),
                    _finite_or_none(balance_discount),
                    _finite_or_none(expected_profit),
                    _finite_or_none(expected_roi),
                    observed_at,
                    now,
                    int(row["id"]),
                    str(row["request_id"]),
                ),
            )
    finally:
        db.close()


def _refresh_job_progress(settings: Settings, request_id: str) -> None:
    db = Database(settings.db_path)
    try:
        counts = _result_counts(db, request_id)
        with db.conn:
            db.conn.execute(
                f"""
                UPDATE {_JOB_TABLE}
                SET processed_count = ?, observed_count = ?, filtered_count = ?,
                    error_count = ?, cursor = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (
                    counts["processed"],
                    counts["observed"],
                    counts["filtered"],
                    counts["errors"],
                    counts["cursor"],
                    utc_now_iso(),
                    request_id,
                ),
            )
    finally:
        db.close()


def _finish_or_requeue(settings: Settings, request_id: str) -> dict[str, Any]:
    _refresh_job_progress(settings, request_id)
    db = Database(settings.db_path)
    try:
        row = _get_job_row(db, request_id)
        if row is None:
            raise LookupError(f"C5 research scan not found: {request_id}")
        if str(row["status"] or "") != "running":
            return _job_to_public(row)
        counts = _result_counts(db, request_id)
        now = utc_now_iso()
        if counts["remaining"] > 0:
            target_status = "queued"
            completed_at = None
        else:
            target_status = (
                "completed_with_errors" if counts["errors"] > 0 else "completed"
            )
            completed_at = now
        with db.conn:
            db.conn.execute(
                f"""
                UPDATE {_JOB_TABLE}
                SET status = ?, control_action = NULL, cursor = ?,
                    processed_count = ?, observed_count = ?, filtered_count = ?,
                    error_count = ?, next_attempt_at = NULL,
                    completed_at = ?, updated_at = ?
                WHERE request_id = ? AND status = 'running'
                """,
                (
                    target_status,
                    counts["cursor"],
                    counts["processed"],
                    counts["observed"],
                    counts["filtered"],
                    counts["errors"],
                    completed_at,
                    now,
                    request_id,
                ),
            )
        current = _get_job_row(db, request_id)
        if current is None:  # pragma: no cover
            raise RuntimeError("C5 research scan disappeared during completion")
        return _job_to_public(current)
    finally:
        db.close()


def _set_retry(
    settings: Settings,
    request_id: str,
    *,
    cursor: int,
    error: str,
    retry_after: Any,
) -> dict[str, Any]:
    delay = _retry_delay_seconds(retry_after)
    next_attempt_at = (
        datetime.now(timezone.utc) + timedelta(seconds=delay)
    ).isoformat()
    db = Database(settings.db_path)
    try:
        with db.conn:
            db.conn.execute(
                f"""
                UPDATE {_JOB_TABLE}
                SET status = 'retry', control_action = NULL, cursor = ?,
                    last_error = ?, next_attempt_at = ?, updated_at = ?
                WHERE request_id = ? AND status = 'running'
                """,
                (
                    max(0, int(cursor)),
                    str(error)[:1000],
                    next_attempt_at,
                    utc_now_iso(),
                    request_id,
                ),
            )
        current = _get_job_row(db, request_id)
        if current is None:  # pragma: no cover
            raise RuntimeError("C5 research scan disappeared during retry update")
        payload = _job_to_public(current)
        payload["safeToRetry"] = True
        return payload
    finally:
        db.close()


def _fail_job(settings: Settings, request_id: str, error: str) -> dict[str, Any]:
    db = Database(settings.db_path)
    try:
        now = utc_now_iso()
        with db.conn:
            db.conn.execute(
                f"""
                UPDATE {_JOB_TABLE}
                SET status = 'failed', control_action = NULL, last_error = ?,
                    next_attempt_at = NULL, completed_at = ?, updated_at = ?
                WHERE request_id = ? AND status = 'running'
                """,
                (str(error)[:1000], now, now, request_id),
            )
        row = _get_job_row(db, request_id)
        if row is None:
            raise LookupError(f"C5 research scan not found: {request_id}")
        return _job_to_public(row)
    finally:
        db.close()


def _result_counts(db: Database, request_id: str) -> dict[str, int]:
    row = db.conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status IN ('pending', 'retry') THEN 1 ELSE 0 END) AS remaining,
            SUM(CASE WHEN status = 'observed' THEN 1 ELSE 0 END) AS observed,
            SUM(CASE WHEN status = 'c5_filtered_out' THEN 1 ELSE 0 END) AS filtered,
            SUM(CASE WHEN status IN (
                'c5_error', 'c5_price_unavailable', 'steam_error',
                'currency_invalid', 'orderbook_empty'
            ) THEN 1 ELSE 0 END) AS errors,
            MIN(CASE WHEN status IN ('pending', 'retry') THEN catalog_index END)
                AS next_cursor
        FROM {_RESULT_TABLE}
        WHERE request_id = ?
        """,
        (request_id,),
    ).fetchone()
    total = int(row["total"] or 0)
    remaining = int(row["remaining"] or 0)
    return {
        "total": total,
        "remaining": remaining,
        "processed": max(0, total - remaining),
        "observed": int(row["observed"] or 0),
        "filtered": int(row["filtered"] or 0),
        "errors": int(row["errors"] or 0),
        "cursor": int(row["next_cursor"] if row["next_cursor"] is not None else total),
    }


def _get_job_row(db: Database, request_id: str) -> Any | None:
    return db.conn.execute(
        f"SELECT * FROM {_JOB_TABLE} WHERE request_id = ?",
        (request_id,),
    ).fetchone()


def _job_to_public(row: Any) -> dict[str, Any]:
    status = str(row["status"] or "queued")
    matched_count = int(row["matched_count"] or 0)
    processed_count = int(row["processed_count"] or 0)
    progress = (
        min(1.0, max(0.0, processed_count / matched_count))
        if matched_count > 0
        else (1.0 if status in _TERMINAL_JOB_STATUSES else 0.0)
    )
    return {
        "ok": True,
        "requestId": str(row["request_id"] or ""),
        "status": status,
        "terminal": status in _TERMINAL_JOB_STATUSES,
        "queued": status == "queued",
        "retryable": status in {"retry", "failed", "paused"},
        "safeToRetry": status == "retry",
        "requestedAction": str(row["control_action"] or "") or None,
        "researchOnly": True,
        "canExecute": False,
        "filters": _json_load_object(row["filters_json"]),
        "matchedCount": matched_count,
        "processedCount": processed_count,
        "observedCount": int(row["observed_count"] or 0),
        "filteredCount": int(row["filtered_count"] or 0),
        "errorCount": int(row["error_count"] or 0),
        "cursor": int(row["cursor"] or 0),
        "progress": progress,
        "lastError": str(row["last_error"] or "") or None,
        "nextAttemptAt": row["next_attempt_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
    }


def _result_to_public(row: Any) -> dict[str, Any]:
    expected_roi = safe_float(row["expected_roi"])
    return {
        "id": int(row["id"]),
        "requestId": str(row["request_id"] or ""),
        "catalogIndex": int(row["catalog_index"]),
        "marketHashName": str(row["market_hash_name"] or ""),
        "name": str(row["name_cn"] or row["market_hash_name"] or ""),
        "status": str(row["status"] or ""),
        "taxonomy": _json_load_object(row["taxonomy_json"]),
        "c5ListingPrice": safe_float(row["c5_listing_price"]),
        "c5PriceSource": str(row["c5_price_source"] or "") or None,
        "c5Error": str(row["c5_error"] or "") or None,
        "steamSellPrice": safe_float(row["steam_sell_price"]),
        "steamPriceSource": str(row["steam_price_source"] or "") or None,
        "steamError": str(row["steam_error"] or "") or None,
        "steamOrderbook": _json_load_object(row["orderbook_json"]),
        "c5ExpectedNetPrice": safe_float(row["c5_expected_net_price"]),
        "balanceDiscount": safe_float(row["balance_discount"]),
        "expectedProfit": safe_float(row["expected_profit"]),
        "expectedRoi": expected_roi,
        "expectedRoiPct": expected_roi * 100.0 if expected_roi is not None else None,
        "researchOnly": True,
        "canExecute": False,
        "observedAt": row["observed_at"],
        "updatedAt": row["updated_at"],
    }


def _normalize_c5_batch_payload(
    payload: Any,
    *,
    expected_names: Iterable[str],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RuntimeError("C5 price_batch returned a non-object payload")
    data: Any = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if not isinstance(data, Mapping):
        raise RuntimeError("C5 price_batch data is not an object")
    expected = {str(name) for name in expected_names}
    return {
        str(name): value
        for name, value in data.items()
        if str(name) in expected and isinstance(value, Mapping)
    }


def _extract_c5_price(payload: Any) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    value = safe_float(
        payload.get("price")
        or payload.get("lowestPrice")
        or payload.get("sellPrice")
    )
    if value is None or value <= 0 or not math.isfinite(float(value)):
        return None
    return float(value)


def _first_steam_client(market_service: Any) -> Any | None:
    clients = list(getattr(market_service, "steam_market_clients", []) or [])
    if clients:
        return clients[0]
    return getattr(market_service, "steam_market_client", None)


def _retry_is_due(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)


def _retry_delay_seconds(value: Any) -> float:
    if value not in (None, ""):
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_RETRY_SECONDS


def _is_rate_limited(exc: BaseException) -> bool:
    return _exception_status_code(exc) == 429


def _exception_status_code(exc: BaseException) -> int | None:
    for current in _exception_chain(exc):
        raw_status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if raw_status is None and response is not None:
            raw_status = getattr(response, "status_code", None)
        try:
            if raw_status is not None:
                return int(raw_status)
        except (TypeError, ValueError):
            continue
    return None


def _exception_retry_after(exc: BaseException) -> Any:
    for current in _exception_chain(exc):
        retry_after = getattr(current, "retry_after", None)
        if retry_after not in (None, ""):
            return retry_after
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            retry_after = headers.get("Retry-After")
            if retry_after not in (None, ""):
                return retry_after
    return None


def _exception_chain(exc: BaseException) -> Iterable[BaseException]:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _safe_error(exc: BaseException) -> str:
    return str(exc).strip()[:1000] or type(exc).__name__


def _normalize_request_id(value: Any) -> str:
    request_id = str(value or "").strip()
    if not request_id:
        raise ValueError("requestId is required")
    return request_id


def _optional_nonnegative_float(value: Any, *, field: str) -> float | None:
    if value in (None, ""):
        return None
    parsed = safe_float(value)
    if parsed is None or not math.isfinite(float(parsed)) or parsed < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return float(parsed)


def _positive_or_zero(value: Any) -> float | None:
    parsed = safe_float(value)
    if parsed is None or parsed < 0 or not math.isfinite(float(parsed)):
        return None
    return float(parsed)


def _finite_or_none(value: Any) -> float | None:
    parsed = safe_float(value)
    if parsed is None or not math.isfinite(float(parsed)):
        return None
    return float(parsed)


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    safe_size = max(1, int(size))
    for index in range(0, len(values), safe_size):
        yield values[index : index + safe_size]


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not JSON serializable: {exc}") from exc


def _json_load_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


__all__ = [
    "create_c5_research_scan",
    "get_c5_research_scan",
    "initialize_c5_research_schema",
    "list_c5_research_results",
    "run_c5_research_scan_chunk",
    "set_c5_research_scan_action",
]
