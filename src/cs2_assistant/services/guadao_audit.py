from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from cs2_assistant.accounts import AccountStore
from cs2_assistant.clients.c5game import C5GameClient
from cs2_assistant.clients.steam_market import SteamMarketClient
from cs2_assistant.config import PROJECT_ROOT, Settings


CN_TZ = timezone(timedelta(hours=8))
CENT = Decimal("0.01")
FOUR_PLACES = Decimal("0.0001")
DEFAULT_START_AT = "2026-07-19T15:20:00+08:00"
DEFAULT_INITIAL_BALANCE = Decimal("2502.92")
DEFAULT_INITIAL_REAL_VALUE = Decimal("1755.474")
DEFAULT_EXPECTED_ACCOUNT_COUNT = 5
SUPPORTED_EXPORT_FORMATS = {"json", "csv", "markdown"}
AUDIT_TABLE_NAMES = (
    "steam_sales",
    "rebuys",
    "item_conservation",
    "wallet_discount",
)
TERMINAL_STATUSES = {"passed", "failed", "inconclusive", "cancelled"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, sqlite3.Row):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return payload


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _money_cents(value: Any) -> int | None:
    amount = _decimal(value)
    if amount is None:
        return None
    quantized = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def _explicit_cents(payload: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if payload.get(key) in (None, ""):
            continue
        try:
            return int(Decimal(str(payload[key])))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return None


def _amount_cents(
    payload: Mapping[str, Any],
    *,
    cents_keys: Iterable[str] = (),
    amount_keys: Iterable[str] = (),
) -> int | None:
    cents = _explicit_cents(payload, *tuple(cents_keys))
    if cents is not None:
        return cents
    for key in amount_keys:
        cents = _money_cents(payload.get(key))
        if cents is not None:
            return cents
    return None


def _money_text(cents: int | None) -> str | None:
    if cents is None:
        return None
    return format((Decimal(int(cents)) / 100).quantize(CENT), ".2f")


def _decimal_text(value: Decimal | None, *, places: Decimal | None = None) -> str | None:
    if value is None:
        return None
    if places is not None:
        value = value.quantize(places, rounding=ROUND_HALF_UP)
    return format(value, "f")


def _ratio_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _safe_int(value: Any, default: int | None = None) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return default
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _normalize_currency_id(value: Any) -> int | None:
    currency_id = _safe_int(value)
    if currency_id is not None and 2000 <= currency_id < 3000:
        return currency_id - 2000
    return currency_id


def _parse_datetime(value: Any, *, assume_cn: bool = True) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ if assume_cn else timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_time(value: Any, *, assume_cn: bool = True) -> str | None:
    parsed = _parse_datetime(value, assume_cn=assume_cn)
    return parsed.isoformat(timespec="seconds") if parsed is not None else None


def _in_window(value: datetime, start_at: datetime, end_at: datetime) -> bool:
    return start_at <= value <= end_at


def _note(value: Any) -> dict[str, Any]:
    payload = _json_loads(value, {})
    return payload if isinstance(payload, dict) else {}


def _hash_payload(*parts: Any) -> str:
    encoded = _json_dumps(parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _connect(settings: Settings, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(settings.db_path).resolve()
    if readonly:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _managed_connection(
    settings: Settings, *, readonly: bool = False
) -> Iterator[sqlite3.Connection]:
    connection = _connect(settings, readonly=readonly)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_guadao_audit_schema(settings: Settings) -> dict[str, Any]:
    """Create only the isolated audit store; never migrate trading tables."""

    with _managed_connection(settings) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guadao_audit_runs (
                request_id TEXT PRIMARY KEY,
                retry_of_request_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                stage TEXT NOT NULL DEFAULT 'pending',
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                initial_balance_cents INTEGER NOT NULL,
                initial_real_value TEXT NOT NULL,
                expected_account_count INTEGER NOT NULL,
                account_ids_json TEXT NOT NULL DEFAULT '[]',
                reported_ratio TEXT,
                balance_tolerance_cents INTEGER NOT NULL DEFAULT 1,
                ratio_tolerance TEXT NOT NULL DEFAULT '0.000001',
                coverage_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (retry_of_request_id)
                    REFERENCES guadao_audit_runs(request_id)
            );

            CREATE INDEX IF NOT EXISTS idx_guadao_audit_runs_status_time
            ON guadao_audit_runs(status, created_at DESC, request_id);

            CREATE TABLE IF NOT EXISTS guadao_audit_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                source TEXT NOT NULL,
                evidence_key TEXT NOT NULL,
                account_id TEXT,
                external_id TEXT,
                occurred_at TEXT,
                amount_cents INTEGER,
                currency_id INTEGER,
                coverage_complete INTEGER,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(request_id, source, evidence_key),
                FOREIGN KEY (request_id)
                    REFERENCES guadao_audit_runs(request_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_guadao_audit_evidence_run_source
            ON guadao_audit_evidence(request_id, source, id);

            CREATE TABLE IF NOT EXISTS guadao_audit_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                match_type TEXT NOT NULL,
                row_key TEXT NOT NULL,
                official_key TEXT,
                local_operation_id INTEGER,
                source_sell_operation_id INTEGER,
                effective_rebuy_operation_id INTEGER,
                verdict TEXT NOT NULL,
                reason TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(request_id, match_type, row_key),
                FOREIGN KEY (request_id)
                    REFERENCES guadao_audit_runs(request_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_guadao_audit_matches_run_type
            ON guadao_audit_matches(request_id, match_type, id);

            CREATE TABLE IF NOT EXISTS guadao_audit_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                row_key TEXT NOT NULL,
                verdict TEXT NOT NULL,
                expected_value TEXT,
                actual_value TEXT,
                difference_value TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(request_id, table_name, row_key),
                FOREIGN KEY (request_id)
                    REFERENCES guadao_audit_runs(request_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_guadao_audit_checks_run_table
            ON guadao_audit_checks(request_id, table_name, id);
            """
        )
    return {"ok": True, "tables": [
        "guadao_audit_runs",
        "guadao_audit_evidence",
        "guadao_audit_matches",
        "guadao_audit_checks",
    ]}


def _run_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    summary = _json_loads(row["summary_json"], {})
    coverage = _json_loads(row["coverage_json"], {})
    account_ids = _json_loads(row["account_ids_json"], [])
    return {
        "requestId": row["request_id"],
        "retryOfRequestId": row["retry_of_request_id"],
        "status": row["status"],
        "stage": row["stage"],
        "startAt": row["start_at"],
        "endAt": row["end_at"],
        "initialBalance": _money_text(row["initial_balance_cents"]),
        "initialRealValue": row["initial_real_value"],
        "expectedAccountCount": int(row["expected_account_count"]),
        "accountIds": account_ids if isinstance(account_ids, list) else [],
        "reportedComprehensiveRatio": row["reported_ratio"],
        "coverage": coverage if isinstance(coverage, dict) else {},
        "summary": summary if isinstance(summary, dict) else {},
        "error": row["error"],
        "cancelRequested": bool(row["cancel_requested"]),
        "createdAt": row["created_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "updatedAt": row["updated_at"],
    }


def create_guadao_audit_run(
    settings: Settings,
    *,
    end_at: str,
    start_at: str = DEFAULT_START_AT,
    initial_balance: Any = DEFAULT_INITIAL_BALANCE,
    initial_real_value: Any = DEFAULT_INITIAL_REAL_VALUE,
    account_ids: Iterable[str] | None = None,
    expected_account_count: int = DEFAULT_EXPECTED_ACCOUNT_COUNT,
    reported_comprehensive_ratio: Any | None = None,
    balance_tolerance_cents: int = 1,
    ratio_tolerance: Any = Decimal("0.000001"),
    request_id: str | None = None,
    retry_of_request_id: str | None = None,
) -> dict[str, Any]:
    initialize_guadao_audit_schema(settings)
    start = _parse_datetime(start_at)
    end = _parse_datetime(end_at)
    if start is None or end is None:
        raise ValueError("start_at and end_at must be valid ISO 8601 timestamps")
    if end < start:
        raise ValueError("end_at must not be earlier than start_at")
    initial_balance_cents = _money_cents(initial_balance)
    initial_real = _decimal(initial_real_value)
    if initial_balance_cents is None or initial_balance_cents < 0:
        raise ValueError("initial_balance must be a non-negative amount")
    if initial_real is None or initial_real < 0:
        raise ValueError("initial_real_value must be a non-negative decimal")
    expected_count = int(expected_account_count)
    if expected_count <= 0:
        raise ValueError("expected_account_count must be positive")
    ratio = _decimal(reported_comprehensive_ratio)
    tolerance = _decimal(ratio_tolerance)
    if reported_comprehensive_ratio not in (None, "") and ratio is None:
        raise ValueError("reported_comprehensive_ratio must be numeric")
    if tolerance is None or tolerance < 0:
        raise ValueError("ratio_tolerance must be non-negative")
    normalized_accounts = list(
        dict.fromkeys(
            str(value).strip()
            for value in (account_ids or [])
            if str(value or "").strip()
        )
    )
    now = _utc_now_iso()
    run_id = str(request_id or f"GDA-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:10]}")
    with _managed_connection(settings) as conn:
        conn.execute(
            """
            INSERT INTO guadao_audit_runs (
                request_id, retry_of_request_id, status, stage,
                start_at, end_at, initial_balance_cents, initial_real_value,
                expected_account_count, account_ids_json, reported_ratio,
                balance_tolerance_cents, ratio_tolerance,
                created_at, updated_at
            ) VALUES (?, ?, 'pending', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                retry_of_request_id,
                start.isoformat(timespec="seconds"),
                end.isoformat(timespec="seconds"),
                initial_balance_cents,
                format(initial_real, "f"),
                expected_count,
                _json_dumps(normalized_accounts),
                _ratio_text(ratio),
                max(0, int(balance_tolerance_cents)),
                format(tolerance, "f"),
                now,
                now,
            ),
        )
    created = get_guadao_audit_run(settings, run_id)
    if created is None:
        raise RuntimeError("guadao audit run was not persisted")
    return created


def get_guadao_audit_run(settings: Settings, request_id: str) -> dict[str, Any] | None:
    initialize_guadao_audit_schema(settings)
    with _managed_connection(settings) as conn:
        row = conn.execute(
            "SELECT * FROM guadao_audit_runs WHERE request_id = ?",
            (str(request_id).strip(),),
        ).fetchone()
    return _run_row(row)


def cancel_guadao_audit_run(settings: Settings, request_id: str) -> dict[str, Any]:
    initialize_guadao_audit_schema(settings)
    now = _utc_now_iso()
    with _managed_connection(settings) as conn:
        row = conn.execute(
            "SELECT status FROM guadao_audit_runs WHERE request_id = ?",
            (str(request_id).strip(),),
        ).fetchone()
        if row is None:
            raise KeyError(f"guadao audit run not found: {request_id}")
        if str(row["status"]) not in TERMINAL_STATUSES:
            conn.execute(
                """
                UPDATE guadao_audit_runs
                SET status = 'cancelled', stage = 'cancelled',
                    cancel_requested = 1, finished_at = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (now, now, str(request_id).strip()),
            )
    result = get_guadao_audit_run(settings, request_id)
    if result is None:
        raise KeyError(f"guadao audit run not found: {request_id}")
    return result


def retry_guadao_audit_run(settings: Settings, request_id: str) -> dict[str, Any]:
    initialize_guadao_audit_schema(settings)
    with _managed_connection(settings) as conn:
        row = conn.execute(
            "SELECT * FROM guadao_audit_runs WHERE request_id = ?",
            (str(request_id).strip(),),
        ).fetchone()
    if row is None:
        raise KeyError(f"guadao audit run not found: {request_id}")
    if str(row["status"]) not in TERMINAL_STATUSES:
        raise ValueError("only a terminal guadao audit run can be retried")
    return create_guadao_audit_run(
        settings,
        start_at=row["start_at"],
        end_at=row["end_at"],
        initial_balance=Decimal(int(row["initial_balance_cents"])) / 100,
        initial_real_value=row["initial_real_value"],
        account_ids=_json_loads(row["account_ids_json"], []),
        expected_account_count=int(row["expected_account_count"]),
        reported_comprehensive_ratio=row["reported_ratio"],
        balance_tolerance_cents=int(row["balance_tolerance_cents"]),
        ratio_tolerance=row["ratio_tolerance"],
        retry_of_request_id=str(request_id),
    )


# Short aliases are intentionally public for callers that model retry/cancel as actions.
retry_guadao_audit = retry_guadao_audit_run
cancel_guadao_audit = cancel_guadao_audit_run


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _read_local_evidence(settings: Settings) -> dict[str, Any]:
    result: dict[str, Any] = {
        "coverageComplete": True,
        "missingTables": [],
        "sales": [],
        "rebuys": [],
        "profitTrades": [],
        "manualAuditEvents": [],
    }
    try:
        conn = _connect(settings, readonly=True)
    except Exception as exc:
        return {
            **result,
            "coverageComplete": False,
            "errors": [f"local database is unreadable: {exc}"],
        }
    try:
        if not _table_exists(conn, "pool_operations"):
            result["coverageComplete"] = False
            result["missingTables"].append("pool_operations")
        else:
            rows = conn.execute(
                """
                SELECT * FROM pool_operations
                WHERE operation_type IN ('sell_on_steam', 'rebuy_on_c5')
                ORDER BY id ASC
                """
            ).fetchall()
            for row in rows:
                payload = dict(row)
                if str(payload.get("strategy") or "guadao") != "guadao":
                    continue
                if payload.get("operation_type") == "sell_on_steam":
                    result["sales"].append(payload)
                else:
                    result["rebuys"].append(payload)

        if _table_exists(conn, "profit_trades"):
            result["profitTrades"] = [
                dict(row)
                for row in conn.execute("SELECT * FROM profit_trades ORDER BY id ASC").fetchall()
            ]
        else:
            result["missingTables"].append("profit_trades")

        if _table_exists(conn, "guadao_operation_audit_events"):
            result["manualAuditEvents"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM guadao_operation_audit_events
                    WHERE event_type = 'manual_external_rebuy_completed'
                    ORDER BY id ASC
                    """
                ).fetchall()
            ]
    except Exception as exc:
        result["coverageComplete"] = False
        result.setdefault("errors", []).append(f"local evidence query failed: {exc}")
    finally:
        conn.close()
    return result


def _call_provider(
    provider: Any,
    *,
    default_provider: Callable[..., dict[str, Any]],
    method_names: tuple[str, ...],
    kwargs: dict[str, Any],
    source_name: str,
) -> dict[str, Any]:
    target = provider if provider is not None else default_provider
    try:
        if callable(target):
            payload = target(**kwargs)
        else:
            method = next(
                (getattr(target, name) for name in method_names if callable(getattr(target, name, None))),
                None,
            )
            if method is None:
                raise TypeError(f"{source_name} evidence provider has no supported collect method")
            payload = method(**kwargs)
    except Exception as exc:
        return {
            "coverageComplete": False,
            "errors": [f"{source_name} evidence provider failed: {exc}"],
        }
    if not isinstance(payload, dict):
        return {
            "coverageComplete": False,
            "errors": [f"{source_name} evidence provider returned a non-object payload"],
        }
    return payload


def _selected_accounts(account_ids: Iterable[str]) -> tuple[list[Any], list[str]]:
    requested = {str(value).strip() for value in account_ids if str(value).strip()}
    accounts = AccountStore(PROJECT_ROOT / "config").list_accounts()
    if not requested:
        return accounts, []
    selected = [
        account
        for account in accounts
        if requested.intersection(
            {
                str(account.id or "").strip(),
                str(account.name or "").strip(),
                str(account.steam_id64 or "").strip(),
            }
        )
    ]
    found = {
        value
        for account in selected
        for value in (str(account.id or ""), str(account.name or ""), str(account.steam_id64 or ""))
        if value
    }
    return selected, sorted(requested - found)


def _steam_asset_row(payload: Mapping[str, Any], purchase: Mapping[str, Any]) -> dict[str, Any]:
    asset = purchase.get("asset") if isinstance(purchase.get("asset"), dict) else {}
    assets = payload.get("assets") if isinstance(payload.get("assets"), dict) else {}
    app_id = str(asset.get("appid") or "730")
    app_rows = assets.get(app_id) or assets.get(_safe_int(app_id)) or {}
    if not isinstance(app_rows, dict):
        return {}
    target_ids = {
        str(value)
        for value in (asset.get("id"), asset.get("new_id"))
        if value not in (None, "")
    }
    for context_rows in app_rows.values():
        if not isinstance(context_rows, dict):
            continue
        for row_id, row in context_rows.items():
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or row_id) in target_ids:
                return row
    return {}


def _steam_purchase_row(payload: Mapping[str, Any], listing_id: str, purchase_id: str) -> dict[str, Any]:
    purchases = payload.get("purchases")
    if not isinstance(purchases, dict):
        return {}
    candidate = purchases.get(f"{listing_id}_{purchase_id}") or purchases.get(listing_id)
    return candidate if isinstance(candidate, dict) else {}


def _raw_history_amount_cents(payload: Mapping[str, Any], key: str) -> int | None:
    return _explicit_cents(payload, key)


def _default_steam_evidence_provider(
    *,
    settings: Settings,
    start_at: str,
    end_at: str,
    account_ids: list[str],
    expected_account_count: int,
    **_: Any,
) -> dict[str, Any]:
    start = _parse_datetime(start_at)
    end = _parse_datetime(end_at)
    if start is None or end is None:
        return {"coverageComplete": False, "accounts": [], "errors": ["invalid audit window"]}
    accounts, missing = _selected_accounts(account_ids)
    results: list[dict[str, Any]] = []
    errors = [f"configured Steam account not found: {value}" for value in missing]
    for account in accounts:
        account_result: dict[str, Any] = {
            "accountId": account.id,
            "accountName": account.name,
            "steamId": account.steam_id64,
            "coverageComplete": False,
            "sales": [],
            "purchases": [],
            "pagesScanned": 0,
            "errors": [],
        }
        if not account.cookies or not account.steam_id64:
            account_result["errors"].append("missing cookies or steam_id64")
            results.append(account_result)
            continue
        try:
            client = SteamMarketClient(
                cookies=account.cookies,
                steam_id64=account.steam_id64,
                identity_secret=account.identity_secret,
                device_id=account.device_id,
                account_id=account.id,
                base_url=settings.steam_market_base_url,
                request_source="guadao_audit",
                allow_account_relogin=False,
            )
            offset = 0
            page_size = 500
            for _page in range(100):
                payload = client.market_history(start=offset, count=page_size)
                account_result["pagesScanned"] += 1
                events = payload.get("events")
                if not isinstance(events, list):
                    raise RuntimeError("Steam market history events payload is invalid")
                readable_times: list[datetime] = []
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    event_type = _safe_int(event.get("event_type"))
                    if event_type not in {3, 4}:
                        continue
                    listing_id = str(event.get("listingid") or "").strip()
                    purchase_id = str(event.get("purchaseid") or "").strip()
                    purchase = _steam_purchase_row(payload, listing_id, purchase_id)
                    event_time = _parse_datetime(
                        purchase.get("time_sold") if event_type == 3 else event.get("time_event"),
                        assume_cn=False,
                    ) or _parse_datetime(event.get("time_event"), assume_cn=False)
                    if event_time is not None:
                        readable_times.append(event_time)
                    if event_time is None or not _in_window(event_time, start, end):
                        continue
                    asset = purchase.get("asset") if isinstance(purchase.get("asset"), dict) else {}
                    asset_row = _steam_asset_row(payload, purchase)
                    market_name = str(
                        asset_row.get("market_hash_name")
                        or purchase.get("market_hash_name")
                        or asset.get("market_hash_name")
                        or ""
                    ).strip()
                    common = {
                        "accountId": account.id,
                        "accountName": account.name,
                        "steamId": account.steam_id64,
                        "listingId": listing_id or None,
                        "purchaseId": purchase_id or None,
                        "assetId": str(
                            event.get("assetid")
                            or asset.get("id")
                            or asset_row.get("id")
                            or ""
                        ) or None,
                        "marketHashName": market_name,
                        "currencyId": _normalize_currency_id(
                            purchase.get("received_currencyid")
                            if event_type == 3
                            else purchase.get("currencyid")
                        ),
                        "quantity": 1,
                    }
                    if event_type == 3:
                        paid = _raw_history_amount_cents(purchase, "paid_amount")
                        fee = _raw_history_amount_cents(purchase, "paid_fee")
                        gross = paid + fee if paid is not None and fee is not None else None
                        account_result["sales"].append(
                            {
                                **common,
                                "soldAt": event_time.isoformat(timespec="seconds"),
                                "grossAmountCents": gross,
                                "netAmountCents": _raw_history_amount_cents(
                                    purchase, "received_amount"
                                ),
                            }
                        )
                    else:
                        paid = _raw_history_amount_cents(purchase, "paid_amount")
                        fee = _raw_history_amount_cents(purchase, "paid_fee")
                        account_result["purchases"].append(
                            {
                                **common,
                                "purchasedAt": event_time.isoformat(timespec="seconds"),
                                "paidAmountCents": (
                                    paid + fee if paid is not None and fee is not None else None
                                ),
                            }
                        )
                total_count = _safe_int(payload.get("total_count"))
                offset += page_size
                reached_start = bool(readable_times and min(readable_times) <= start)
                reached_end = (
                    not events
                    or (total_count is not None and offset >= total_count)
                    or (total_count is None and len(events) < page_size)
                )
                if reached_start or reached_end:
                    account_result["coverageComplete"] = True
                    break
            else:
                account_result["errors"].append("Steam history page budget exhausted")
        except Exception as exc:
            account_result["errors"].append(str(exc))
        results.append(account_result)
    coverage = (
        not missing
        and len(results) == int(expected_account_count)
        and all(bool(row.get("coverageComplete")) for row in results)
    )
    if len(results) != int(expected_account_count):
        errors.append(
            f"expected {expected_account_count} Steam accounts, collected {len(results)}"
        )
    return {"coverageComplete": coverage, "accounts": results, "errors": errors}


def _default_balance_evidence_provider(
    *,
    settings: Settings,
    account_ids: list[str],
    expected_account_count: int,
    **_: Any,
) -> dict[str, Any]:
    accounts, missing = _selected_accounts(account_ids)
    rows: list[dict[str, Any]] = []
    errors = [f"configured Steam account not found: {value}" for value in missing]
    for account in accounts:
        row: dict[str, Any] = {
            "accountId": account.id,
            "accountName": account.name,
            "steamId": account.steam_id64,
            "coverageComplete": False,
        }
        if not account.cookies or not account.steam_id64:
            row["error"] = "missing cookies or steam_id64"
            rows.append(row)
            continue
        try:
            wallet = SteamMarketClient(
                cookies=account.cookies,
                steam_id64=account.steam_id64,
                identity_secret=account.identity_secret,
                device_id=account.device_id,
                account_id=account.id,
                base_url=settings.steam_market_base_url,
                request_source="guadao_audit",
                allow_account_relogin=False,
            ).wallet_balance()
            row.update(
                {
                    "availableBalance": wallet.get("balance"),
                    "pendingBalance": wallet.get("delayed_balance"),
                    "currencyId": wallet.get("currency_id"),
                    "currency": wallet.get("currency"),
                    "coverageComplete": True,
                }
            )
        except Exception as exc:
            row["error"] = str(exc)
        rows.append(row)
    coverage = (
        not missing
        and len(rows) == int(expected_account_count)
        and all(bool(row.get("coverageComplete")) for row in rows)
    )
    if len(rows) != int(expected_account_count):
        errors.append(f"expected {expected_account_count} wallets, collected {len(rows)}")
    return {"coverageComplete": coverage, "accounts": rows, "errors": errors}


def _c5_list_rows(payload: Any) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    for key in ("list", "records", "rows", "orderList"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    data = payload.get("data")
    return _c5_list_rows(data) if isinstance(data, dict) else None


def _c5_pages(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    pages = _safe_int(payload.get("pages"))
    if pages is not None:
        return pages
    data = payload.get("data")
    return _c5_pages(data) if isinstance(data, dict) else None


def _c5_order_ids(payload: Mapping[str, Any]) -> list[str]:
    values = []
    for key in (
        "orderId",
        "orderAssetId",
        "assetOrderId",
        "tradeOrderId",
        "lookupOrderId",
        "id",
    ):
        value = str(payload.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    lookup_ids = payload.get("lookupOrderIds")
    if isinstance(lookup_ids, (list, tuple, set)):
        for raw_value in lookup_ids:
            value = str(raw_value or "").strip()
            if value and value not in values:
                values.append(value)
    return values


def _default_c5_evidence_provider(
    *,
    settings: Settings,
    order_ids: list[str],
    local_rebuys: list[Mapping[str, Any]] | None = None,
    **_: Any,
) -> dict[str, Any]:
    if not settings.c5_api_key:
        return {
            "coverageComplete": False,
            "orders": [],
            "orderList": [],
            "errors": ["C5 API key is not configured"],
        }
    client = C5GameClient(settings.c5_api_key, settings.c5_base_url)
    errors: list[str] = []
    order_list: list[dict[str, Any]] = []
    list_complete = False
    seen_pages: set[str] = set()
    try:
        for page_num in range(1, 101):
            payload = client.buyer_order_status(page_num=page_num, page_size=100, status=None)
            rows = _c5_list_rows(payload)
            if rows is None:
                raise RuntimeError("C5 buyer order response has no order list")
            fingerprint = _hash_payload(rows)
            if fingerprint in seen_pages:
                errors.append("C5 buyer order pagination stopped making progress")
                break
            seen_pages.add(fingerprint)
            order_list.extend(rows)
            pages = _c5_pages(payload)
            if (pages is not None and page_num >= pages) or (pages is None and len(rows) < 100):
                list_complete = True
                break
        else:
            errors.append("C5 buyer order page budget exhausted")
    except Exception as exc:
        errors.append(f"C5 buyer order list failed: {exc}")

    lookup_groups: list[list[str]] = []
    grouped_ids: set[str] = set()
    seen_groups: set[tuple[str, ...]] = set()
    for row in local_rebuys or []:
        aliases = _rebuy_order_ids(row)
        key = tuple(aliases)
        if aliases and key not in seen_groups:
            seen_groups.add(key)
            lookup_groups.append(aliases)
            grouped_ids.update(aliases)
    for raw_order_id in order_ids:
        order_id = str(raw_order_id or "").strip()
        if order_id and order_id not in grouped_ids:
            lookup_groups.append([order_id])
            grouped_ids.add(order_id)

    details: list[dict[str, Any]] = []
    details_complete = True
    for aliases in lookup_groups:
        alias_errors: list[str] = []
        resolved = False
        for order_id in aliases:
            try:
                detail = client.buyer_order_detail(order_id)
                if not isinstance(detail, dict) or not detail:
                    raise RuntimeError("empty buyer_order_detail payload")
                details.append(
                    {
                        **detail,
                        "orderId": detail.get("orderId") or order_id,
                        "lookupOrderId": order_id,
                        "lookupOrderIds": aliases,
                        "source": "buyer_order_detail",
                    }
                )
                resolved = True
                break
            except Exception as exc:
                alias_errors.append(f"{order_id}: {exc}")
        if not resolved:
            details_complete = False
            errors.append(
                "C5 buyer_order_detail aliases failed: " + "; ".join(alias_errors)
            )
    return {
        "coverageComplete": bool(list_complete and details_complete),
        "orders": details,
        "orderList": order_list,
        "errors": errors,
    }


def _provider_coverage(payload: Mapping[str, Any]) -> bool:
    return payload.get("coverageComplete") is True


def _manifest_errors(payload: Mapping[str, Any], source: str) -> list[str]:
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return []
    return [f"{source}: {str(value)}" for value in errors if str(value or "").strip()]


def _normalize_steam_evidence(
    payload: Mapping[str, Any],
    *,
    start_at: datetime,
    end_at: datetime,
    account_ids: list[str],
    expected_account_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    gaps = _manifest_errors(payload, "steam_history")
    if not _provider_coverage(payload):
        gaps.append("Steam market history coverage is incomplete")
    accounts = payload.get("accounts")
    account_rows = accounts if isinstance(accounts, list) else []
    top_level_sales = payload.get("sales") if isinstance(payload.get("sales"), list) else []
    top_level_purchases = (
        payload.get("purchases") if isinstance(payload.get("purchases"), list) else []
    )
    if not account_rows and (top_level_sales or top_level_purchases):
        account_rows = [
            {
                "accountId": None,
                "coverageComplete": payload.get("coverageComplete"),
                "sales": top_level_sales,
                "purchases": top_level_purchases,
            }
        ]
    observed_accounts = {
        str(row.get("accountId") or row.get("id") or "").strip()
        for row in account_rows
        if isinstance(row, dict)
        and str(row.get("accountId") or row.get("id") or "").strip()
    }
    if len(account_rows) != int(expected_account_count):
        gaps.append(
            f"Steam history account coverage {len(account_rows)}/{expected_account_count}"
        )
    for account_id in account_ids:
        if account_id not in observed_accounts:
            gaps.append(f"Steam history is missing account {account_id}")

    sales: list[dict[str, Any]] = []
    purchases: list[dict[str, Any]] = []
    seen_sales: set[str] = set()
    seen_purchases: set[str] = set()
    for account in account_rows:
        if not isinstance(account, dict):
            gaps.append("Steam history account payload is invalid")
            continue
        account_id = str(account.get("accountId") or account.get("id") or "").strip()
        if account.get("coverageComplete") is not True:
            gaps.append(f"Steam history coverage is incomplete for {account_id or 'unknown account'}")
        account_sales = account.get("sales") if isinstance(account.get("sales"), list) else []
        account_purchases = (
            account.get("purchases") if isinstance(account.get("purchases"), list) else []
        )
        for raw in account_sales:
            if not isinstance(raw, dict):
                gaps.append("Steam sale evidence row is invalid")
                continue
            sold_at = _parse_datetime(raw.get("soldAt") or raw.get("timeSold"), assume_cn=False)
            if sold_at is None:
                gaps.append("Steam official sale is missing soldAt")
                continue
            if not _in_window(sold_at, start_at, end_at):
                continue
            row_account = str(raw.get("accountId") or account_id or "").strip()
            gross_cents = _amount_cents(
                raw,
                cents_keys=("grossAmountCents", "steamGrossCents"),
                amount_keys=("grossAmount", "steamGross", "salePrice"),
            )
            net_cents = _amount_cents(
                raw,
                cents_keys=("netAmountCents", "receivedAmountCents", "steamNetCents"),
                amount_keys=("netAmount", "receivedAmount", "steamNet"),
            )
            currency_id = _normalize_currency_id(
                raw.get("currencyId") or raw.get("receivedCurrencyId")
            )
            if gross_cents is None:
                gaps.append("Steam official sale is missing gross amount")
            if net_cents is None:
                gaps.append("Steam official sale is missing seller-net amount")
            if currency_id != 23:
                gaps.append("Steam official sale currency is not confirmed as CNY")
            quantity = max(1, int(_safe_int(raw.get("quantity"), 1) or 1))
            row = {
                "accountId": row_account or None,
                "listingId": str(raw.get("listingId") or "").strip() or None,
                "purchaseId": str(raw.get("purchaseId") or "").strip() or None,
                "assetId": str(raw.get("assetId") or "").strip() or None,
                "marketHashName": str(raw.get("marketHashName") or "").strip(),
                "soldAt": sold_at.isoformat(timespec="seconds"),
                "grossCents": gross_cents,
                "netCents": net_cents,
                "currencyId": currency_id,
                "quantity": quantity,
                "raw": raw,
            }
            row["officialKey"] = _hash_payload(
                row["accountId"],
                row["listingId"],
                row["purchaseId"],
                row["assetId"],
                row["soldAt"],
            )
            if row["officialKey"] not in seen_sales:
                seen_sales.add(row["officialKey"])
                sales.append(row)

        for raw in account_purchases:
            if not isinstance(raw, dict):
                gaps.append("Steam purchase evidence row is invalid")
                continue
            purchased_at = _parse_datetime(
                raw.get("purchasedAt") or raw.get("timePurchased"), assume_cn=False
            )
            if purchased_at is None:
                gaps.append("Steam official purchase is missing purchasedAt")
                continue
            if not _in_window(purchased_at, start_at, end_at):
                continue
            row_account = str(raw.get("accountId") or account_id or "").strip()
            paid_cents = _amount_cents(
                raw,
                cents_keys=("paidAmountCents", "paidTotalCents"),
                amount_keys=("paidAmount", "paidTotal"),
            )
            currency_id = _normalize_currency_id(raw.get("currencyId"))
            if paid_cents is None:
                gaps.append("Steam official purchase is missing paid amount")
            if currency_id != 23:
                gaps.append("Steam official purchase currency is not confirmed as CNY")
            row = {
                "accountId": row_account or None,
                "listingId": str(raw.get("listingId") or "").strip() or None,
                "purchaseId": str(raw.get("purchaseId") or "").strip() or None,
                "assetId": str(raw.get("assetId") or raw.get("newAssetId") or "").strip()
                or None,
                "marketHashName": str(raw.get("marketHashName") or "").strip(),
                "purchasedAt": purchased_at.isoformat(timespec="seconds"),
                "paidCents": paid_cents,
                "currencyId": currency_id,
                "raw": raw,
            }
            row["officialKey"] = _hash_payload(
                row["accountId"], row["listingId"], row["purchaseId"], row["purchasedAt"]
            )
            if row["officialKey"] not in seen_purchases:
                seen_purchases.add(row["officialKey"])
                purchases.append(row)
    return sales, purchases, list(dict.fromkeys(gaps))


def _normalize_local_sale(row: Mapping[str, Any]) -> dict[str, Any]:
    note = _note(row.get("note"))
    gross_cents = _amount_cents(
        note,
        amount_keys=("steamListPrice", "steamSalePrice", "steamGross"),
    )
    if gross_cents is None:
        gross_cents = _money_cents(row.get("actual_price"))
    net_cents = _amount_cents(
        note,
        amount_keys=("steamSellerNetPrice", "steamNetPrice", "steamSoldNetPrice"),
    )
    return {
        "id": int(row["id"]),
        "marketHashName": str(row.get("market_hash_name") or "").strip(),
        "status": str(row.get("status") or ""),
        "quantity": max(1, int(_safe_int(row.get("quantity"), 1) or 1)),
        "accountId": str(note.get("steamAccountId") or "").strip() or None,
        "listingId": str(note.get("listingId") or note.get("sourceListing") or "").strip()
        or None,
        "purchaseId": str(note.get("steamPurchaseId") or note.get("purchaseId") or "").strip()
        or None,
        "assetId": str(row.get("asset_id") or note.get("assetId") or "").strip() or None,
        "soldAt": _normalized_time(note.get("steamSoldAt") or note.get("timeSold")),
        "grossCents": gross_cents,
        "netCents": net_cents,
        "note": note,
        "raw": dict(row),
    }


def _strong_match_score(official: Mapping[str, Any], local: Mapping[str, Any]) -> int:
    official_account = str(official.get("accountId") or "")
    local_account = str(local.get("accountId") or "")
    if official_account and local_account and official_account != local_account:
        return 0
    score = 0
    for key, weight in (("listingId", 100), ("purchaseId", 90), ("assetId", 80)):
        official_value = str(official.get(key) or "")
        local_value = str(local.get(key) or "")
        if official_value and local_value and official_value == local_value:
            score = max(score, weight)
    return score


def _sale_reconciliation(
    official_sales: list[dict[str, Any]],
    local_rows: list[Mapping[str, Any]],
    *,
    start_at: datetime,
    end_at: datetime,
    official_coverage_complete: bool,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str], list[str]]:
    local_sales = [
        _normalize_local_sale(row)
        for row in local_rows
        if str(row.get("status") or "") == "sold"
    ]
    matched_local_ids: set[int] = set()
    official_to_local: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    gaps: list[str] = []
    for official in official_sales:
        scored = [
            (_strong_match_score(official, local), local)
            for local in local_sales
            if local["id"] not in matched_local_ids
        ]
        best_score = max((score for score, _ in scored), default=0)
        candidates = [local for score, local in scored if score == best_score and score > 0]
        row_key = str(official["officialKey"])
        base = {
            "rowKey": row_key,
            "accountId": official.get("accountId"),
            "listingId": official.get("listingId"),
            "purchaseId": official.get("purchaseId"),
            "assetId": official.get("assetId"),
            "marketHashName": official.get("marketHashName"),
            "officialSoldAt": official.get("soldAt"),
            "officialGross": _money_text(official.get("grossCents")),
            "officialNet": _money_text(official.get("netCents")),
            "officialQuantity": official.get("quantity"),
        }
        if not candidates:
            verdict = "failed" if official_coverage_complete else "inconclusive"
            row = {
                **base,
                "localOperationId": None,
                "programGross": None,
                "programNet": None,
                "netDifference": None,
                "verdict": verdict,
                "reason": "official_sale_missing_local_operation",
            }
            rows.append(row)
            (failures if verdict == "failed" else gaps).append(
                f"official Steam sale has no local operation: {row_key}"
            )
            continue
        if len(candidates) > 1:
            row = {
                **base,
                "localOperationId": None,
                "candidateLocalOperationIds": [candidate["id"] for candidate in candidates],
                "programGross": None,
                "programNet": None,
                "netDifference": None,
                "verdict": "failed" if official_coverage_complete else "inconclusive",
                "reason": "duplicate_local_sale_candidates",
            }
            rows.append(row)
            (failures if row["verdict"] == "failed" else gaps).append(
                f"official Steam sale matches multiple local operations: {row_key}"
            )
            continue
        local = candidates[0]
        matched_local_ids.add(int(local["id"]))
        official_to_local[row_key] = int(local["id"])
        difference_reasons: list[str] = []
        evidence_reasons: list[str] = []
        if local["accountId"] is None:
            evidence_reasons.append("program_account_missing")
        if not local["marketHashName"]:
            evidence_reasons.append("program_market_hash_name_missing")
        elif (
            official.get("marketHashName")
            and local["marketHashName"] != official["marketHashName"]
        ):
            difference_reasons.append("market_hash_name_mismatch")
        for key, reason in (
            ("listingId", "listing_id_mismatch"),
            ("purchaseId", "purchase_id_mismatch"),
            ("assetId", "asset_id_mismatch"),
        ):
            official_value = str(official.get(key) or "")
            local_value = str(local.get(key) or "")
            if official_value and local_value and official_value != local_value:
                difference_reasons.append(reason)
        if local["soldAt"] is None:
            evidence_reasons.append("program_official_sale_time_missing")
        elif local["soldAt"] != official["soldAt"]:
            difference_reasons.append("official_sale_time_mismatch")
        if local["grossCents"] is None:
            evidence_reasons.append("program_gross_missing")
        elif official.get("grossCents") is not None and local["grossCents"] != official["grossCents"]:
            difference_reasons.append("gross_amount_mismatch")
        if local["netCents"] is None:
            evidence_reasons.append("program_net_missing")
        elif official.get("netCents") is not None and local["netCents"] != official["netCents"]:
            difference_reasons.append("net_amount_mismatch")
        if int(local["quantity"]) != int(official.get("quantity") or 1):
            difference_reasons.append("quantity_mismatch")
        reasons = list(dict.fromkeys(evidence_reasons + difference_reasons))
        verdict = (
            "inconclusive"
            if evidence_reasons
            else ("failed" if difference_reasons else "passed")
        )
        if difference_reasons:
            failures.append(
                f"Steam sale mismatch for local operation {local['id']}: "
                f"{','.join(difference_reasons)}"
            )
        if evidence_reasons:
            gaps.append(
                f"local Steam sale evidence is incomplete for operation {local['id']}: "
                f"{','.join(evidence_reasons)}"
            )
        rows.append(
            {
                **base,
                "localOperationId": local["id"],
                "programSoldAt": local["soldAt"],
                "programGross": _money_text(local["grossCents"]),
                "programNet": _money_text(local["netCents"]),
                "programQuantity": local["quantity"],
                "netDifference": _money_text(
                    local["netCents"] - official["netCents"]
                    if local["netCents"] is not None and official.get("netCents") is not None
                    else None
                ),
                "verdict": verdict,
                "reason": ",".join(reasons) if reasons else "matched",
            }
        )

    for local in local_sales:
        if int(local["id"]) in matched_local_ids:
            continue
        local_sold_at = _parse_datetime(local.get("soldAt"), assume_cn=False)
        if local_sold_at is not None and not _in_window(local_sold_at, start_at, end_at):
            continue
        if local_sold_at is None:
            verdict = "inconclusive"
            reason = "local_sale_missing_official_time"
            gaps.append(
                f"local sold operation {local['id']} has no steamSoldAt; completed_at was not used"
            )
        else:
            verdict = "failed" if official_coverage_complete else "inconclusive"
            reason = "local_sale_missing_official_history"
            (failures if verdict == "failed" else gaps).append(
                f"local sold operation {local['id']} has no official Steam sale"
            )
        rows.append(
            {
                "rowKey": f"local-{local['id']}",
                "accountId": local.get("accountId"),
                "listingId": local.get("listingId"),
                "purchaseId": local.get("purchaseId"),
                "assetId": local.get("assetId"),
                "marketHashName": local.get("marketHashName"),
                "officialSoldAt": None,
                "officialGross": None,
                "officialNet": None,
                "officialQuantity": None,
                "localOperationId": local["id"],
                "programSoldAt": local.get("soldAt"),
                "programGross": _money_text(local.get("grossCents")),
                "programNet": _money_text(local.get("netCents")),
                "programQuantity": local.get("quantity"),
                "netDifference": None,
                "verdict": verdict,
                "reason": reason,
            }
        )
    return rows, official_to_local, failures, gaps


def _normalize_c5_details(
    payload: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    details: dict[str, dict[str, Any]] = {}
    gaps = _manifest_errors(payload, "c5")
    if not _provider_coverage(payload):
        gaps.append("C5 buyer order/detail coverage is incomplete")
    orders = payload.get("orders") if isinstance(payload.get("orders"), list) else []
    for raw in orders:
        if not isinstance(raw, dict):
            gaps.append("C5 buyer_order_detail row is invalid")
            continue
        source = str(raw.get("source") or "").strip()
        status_raw = raw.get("status")
        status_int = _safe_int(status_raw)
        status_name = str(raw.get("statusName") or status_raw or "").strip().lower()
        if status_int == 10 or status_name in {
            "success",
            "succeeded",
            "completed",
            "complete",
            "finished",
            "done",
            "delivered",
        }:
            final_status = "success"
        elif status_int == 11 or status_name in {"failed", "fail", "failure", "canceled", "cancelled"}:
            final_status = "failed"
        else:
            final_status = "pending"
        amount_cents = _amount_cents(
            raw,
            cents_keys=("actualAmountCents", "actualPayCents", "priceCents"),
            amount_keys=("actualAmount", "actualPay", "price", "orderAmount"),
        )
        open_item = raw.get("openItemInfo")
        open_item_payload = open_item if isinstance(open_item, dict) else {}
        normalized = {
            "orderIds": _c5_order_ids(raw),
            "status": final_status,
            "actualCents": amount_cents,
            "marketHashName": str(
                raw.get("marketHashName")
                or raw.get("name")
                or open_item_payload.get("marketHashName")
                or open_item_payload.get("name")
                or ""
            ).strip(),
            "source": source,
            "raw": raw,
        }
        for order_id in normalized["orderIds"]:
            details[order_id] = normalized
    return details, list(dict.fromkeys(gaps))


def _rebuy_order_ids(row: Mapping[str, Any]) -> list[str]:
    note = _note(row.get("note"))
    values: list[str] = []
    for key in ("c5OrderId", "c5TradeOrderId"):
        value = str(note.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    payload = note.get("c5OrderPayload")
    if isinstance(payload, dict):
        for value in _c5_order_ids(payload):
            if value not in values:
                values.append(value)
    return values


def _rebuy_source_id(row: Mapping[str, Any]) -> int | None:
    return _safe_int(_note(row.get("note")).get("sourceSellOperationId"))


def _is_manual_rebuy(row: Mapping[str, Any]) -> bool:
    note = _note(row.get("note"))
    return bool(
        str(note.get("c5FinalStatus") or "") == "manual_external_completed"
        or note.get("manualExternalRebuyCompletedAt")
        or note.get("manualExternalRebuySource")
    ) and str(row.get("status") or "") == "completed"


def _frozen_rebuy_cents(row: Mapping[str, Any]) -> int | None:
    note = _note(row.get("note"))
    for value in (
        note.get("manualRebuyRefrozenPrice"),
        note.get("rebuyPrice"),
        row.get("expected_price"),
    ):
        cents = _money_cents(value)
        if cents is not None and cents > 0:
            return cents
    return None


def _classify_rebuy_chain(
    *,
    official: Mapping[str, Any],
    local_sell_id: int | None,
    attempts: list[Mapping[str, Any]],
    c5_details: Mapping[str, Mapping[str, Any]],
    c5_coverage_complete: bool,
) -> tuple[dict[str, Any], list[str], list[str]]:
    failures: list[str] = []
    gaps: list[str] = []
    sale_quantity = int(official.get("quantity") or 1)
    base = {
        "rowKey": str(official["officialKey"]),
        "sourceSellOperationId": local_sell_id,
        "marketHashName": official.get("marketHashName"),
        "steamNet": _money_text(official.get("netCents")),
        "steamQuantity": sale_quantity,
        "attemptCount": len(attempts),
        "attemptOperationIds": [int(row["id"]) for row in attempts],
    }
    if local_sell_id is None:
        verdict = "failed" if c5_coverage_complete else "inconclusive"
        row = {
            **base,
            "effectiveRebuyOperationId": None,
            "destination": "exception",
            "destinationQuantity": sale_quantity,
            "effectiveAmount": None,
            "verdict": verdict,
            "reason": "official_sale_has_no_local_sell_operation",
        }
        (failures if verdict == "failed" else gaps).append(row["reason"])
        return row, failures, gaps
    if not attempts:
        row = {
            **base,
            "effectiveRebuyOperationId": None,
            "destination": "exception",
            "destinationQuantity": sale_quantity,
            "effectiveAmount": None,
            "verdict": "failed",
            "reason": "sell_has_no_rebuy_destination",
        }
        failures.append(f"sell operation {local_sell_id} has no rebuy destination")
        return row, failures, gaps

    attempts = sorted(attempts, key=lambda row: int(row["id"]))
    manual_rows = [row for row in attempts if _is_manual_rebuy(row)]
    remote_successes: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for attempt in attempts:
        for order_id in _rebuy_order_ids(attempt):
            detail = c5_details.get(order_id)
            if detail and detail.get("status") == "success":
                remote_successes.append((attempt, detail))
                break
    if manual_rows and remote_successes:
        effective = manual_rows[-1]
        row = {
            **base,
            "effectiveRebuyOperationId": int(effective["id"]),
            "destination": "exception",
            "destinationQuantity": sale_quantity,
            "effectiveAmount": _money_text(_money_cents(effective.get("actual_price"))),
            "verdict": "failed",
            "reason": "manual_and_c5_success_conflict",
        }
        failures.append(f"sell operation {local_sell_id} has both manual and C5 success")
        return row, failures, gaps
    if len(remote_successes) > 1:
        effective, detail = remote_successes[-1]
        row = {
            **base,
            "effectiveRebuyOperationId": int(effective["id"]),
            "destination": "exception",
            "destinationQuantity": sale_quantity,
            "effectiveAmount": _money_text(detail.get("actualCents")),
            "verdict": "failed",
            "reason": "multiple_remote_c5_successes",
        }
        failures.append(f"sell operation {local_sell_id} has multiple successful C5 orders")
        return row, failures, gaps
    if manual_rows:
        effective = manual_rows[-1]
        amount_cents = _money_cents(effective.get("actual_price"))
        quantity = max(1, int(_safe_int(effective.get("quantity"), 1) or 1))
        verdict = "passed" if amount_cents is not None else "inconclusive"
        if amount_cents is None:
            gaps.append(f"manual rebuy operation {effective['id']} is missing actual amount")
        return (
            {
                **base,
                "effectiveRebuyOperationId": int(effective["id"]),
                "destination": "manual_complete",
                "destinationQuantity": quantity,
                "effectiveAmount": _money_text(amount_cents),
                "effectiveAmountCents": amount_cents,
                "verdict": verdict,
                "reason": "manual_external_completed" if verdict == "passed" else "manual_amount_missing",
            },
            failures,
            gaps,
        )
    if remote_successes:
        effective, detail = remote_successes[0]
        amount_cents = detail.get("actualCents")
        quantity = max(1, int(_safe_int(effective.get("quantity"), 1) or 1))
        detail_source_ok = detail.get("source") == "buyer_order_detail"
        local_item_name = str(effective.get("market_hash_name") or "").strip()
        detail_item_name = str(detail.get("marketHashName") or "").strip()
        effective_item_name = (
            detail_item_name
            or local_item_name
            or str(official.get("marketHashName") or "")
        )
        sold_item_name = str(official.get("marketHashName") or "")
        item_mismatch = bool(
            effective_item_name
            and sold_item_name
            and effective_item_name != sold_item_name
        )
        reasons: list[str] = []
        if not detail_source_ok:
            gaps.append(f"C5 success for operation {effective['id']} is not buyer_order_detail evidence")
            reasons.append("c5_success_source_not_detail")
        if amount_cents is None:
            gaps.append(f"C5 success for operation {effective['id']} is missing remote actual amount")
            reasons.append("remote_actual_amount_missing")
        if item_mismatch:
            failures.append(
                f"C5 item for operation {effective['id']} is {effective_item_name}, "
                f"expected {sold_item_name}"
            )
            reasons.append("c5_item_mismatch")
        local_completed = str(effective.get("status") or "") == "completed"
        local_note = _note(effective.get("note"))
        if not local_completed or str(local_note.get("c5FinalStatus") or "") != "c5_success":
            failures.append(f"local rebuy operation {effective['id']} lags remote C5 success")
            reasons.append("local_state_disagrees_with_remote_success")
        verdict = "inconclusive" if (not detail_source_ok or amount_cents is None) else (
            "failed"
            if (
                item_mismatch
                or not local_completed
                or str(local_note.get("c5FinalStatus") or "") != "c5_success"
            )
            else "passed"
        )
        return (
            {
                **base,
                "marketHashName": effective_item_name,
                "soldMarketHashName": sold_item_name,
                "rebuyMarketHashNameSource": (
                    "buyer_order_detail" if detail_item_name else "local_rebuy"
                ),
                "effectiveRebuyOperationId": int(effective["id"]),
                "destination": "c5_success",
                "destinationQuantity": quantity,
                "effectiveAmount": _money_text(amount_cents),
                "effectiveAmountCents": amount_cents,
                "c5OrderIds": _rebuy_order_ids(effective),
                "verdict": verdict,
                "reason": ",".join(reasons) if reasons else "remote_c5_success",
            },
            failures,
            gaps,
        )

    effective = attempts[-1]
    note = _note(effective.get("note"))
    status = str(effective.get("status") or "")
    quantity = max(1, int(_safe_int(effective.get("quantity"), 1) or 1))
    order_ids = _rebuy_order_ids(effective)
    detail = next((c5_details[value] for value in order_ids if value in c5_details), None)
    if status == "delivery_pending":
        if not order_ids:
            failures.append(f"delivery_pending operation {effective['id']} has no real C5 order id")
            return (
                {
                    **base,
                    "effectiveRebuyOperationId": int(effective["id"]),
                    "destination": "exception",
                    "destinationQuantity": sale_quantity,
                    "effectiveAmount": None,
                    "verdict": "failed",
                    "reason": "delivery_pending_without_c5_order_id",
                },
                failures,
                gaps,
            )
        if detail and detail.get("status") == "failed":
            failures.append(f"delivery_pending operation {effective['id']} is remotely failed")
            return (
                {
                    **base,
                    "effectiveRebuyOperationId": int(effective["id"]),
                    "destination": "exception",
                    "destinationQuantity": sale_quantity,
                    "effectiveAmount": None,
                    "verdict": "failed",
                    "reason": "remote_c5_failed_local_pending",
                },
                failures,
                gaps,
            )
        amount_cents = detail.get("actualCents") if detail else None
        verdict = "passed"
        reason = "c5_delivery_pending"
        if detail is None or amount_cents is None:
            verdict = "inconclusive"
            reason = "delivery_pending_detail_or_amount_missing"
            gaps.append(f"delivery_pending operation {effective['id']} lacks complete C5 detail")
        return (
            {
                **base,
                "effectiveRebuyOperationId": int(effective["id"]),
                "destination": "c5_delivery_pending",
                "destinationQuantity": quantity,
                "effectiveAmount": _money_text(amount_cents),
                "effectiveAmountCents": amount_cents,
                "c5OrderIds": order_ids,
                "verdict": verdict,
                "reason": reason,
            },
            failures,
            gaps,
        )
    if status == "pending" and not any(
        note.get(key) not in (None, "", False)
        for key in ("c5OrderId", "c5TradeOrderId", "c5OrderSubmittedAt", "c5OutTradeNo")
    ):
        frozen_cents = _frozen_rebuy_cents(effective)
        verdict = "passed" if frozen_cents is not None else "inconclusive"
        if frozen_cents is None:
            gaps.append(f"pending rebuy operation {effective['id']} is missing frozen price")
        return (
            {
                **base,
                "effectiveRebuyOperationId": int(effective["id"]),
                "destination": "pending_rebuy",
                "destinationQuantity": quantity,
                "effectiveAmount": _money_text(frozen_cents),
                "effectiveAmountCents": frozen_cents,
                "verdict": verdict,
                "reason": "current_frozen_rebuy_price" if verdict == "passed" else "frozen_price_missing",
            },
            failures,
            gaps,
        )
    if status == "c5_submission_unconfirmed" or str(note.get("c5FinalStatus") or "") == "c5_submission_unconfirmed":
        return (
            {
                **base,
                "effectiveRebuyOperationId": int(effective["id"]),
                "destination": "c5_submission_unconfirmed",
                "destinationQuantity": quantity,
                "effectiveAmount": None,
                "effectiveAmountCents": None,
                "verdict": "passed",
                "reason": "c5_submission_result_unconfirmed",
            },
            failures,
            gaps,
        )
    if order_ids and detail is None:
        gaps.append(
            f"rebuy operation {effective['id']} has no buyer_order_detail evidence"
        )
        return (
            {
                **base,
                "effectiveRebuyOperationId": int(effective["id"]),
                "destination": "exception",
                "destinationQuantity": sale_quantity,
                "effectiveAmount": None,
                "effectiveAmountCents": None,
                "c5OrderIds": order_ids,
                "verdict": "inconclusive",
                "reason": "c5_order_detail_missing",
            },
            failures,
            gaps,
        )
    failures.append(f"rebuy operation {effective['id']} is in exceptional state {status}")
    return (
        {
            **base,
            "effectiveRebuyOperationId": int(effective["id"]),
            "destination": "exception",
            "destinationQuantity": sale_quantity,
            "effectiveAmount": None,
            "effectiveAmountCents": None,
            "verdict": "failed" if c5_coverage_complete else "inconclusive",
            "reason": f"exceptional_rebuy_state:{status or 'missing'}",
        },
        failures if c5_coverage_complete else [],
        gaps + ([] if c5_coverage_complete else [f"C5 evidence incomplete for operation {effective['id']}"]),
    )


def _rebuy_reconciliation(
    official_sales: list[dict[str, Any]],
    official_to_local: Mapping[str, int],
    local_rebuys: list[Mapping[str, Any]],
    c5_payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    c5_details, c5_gaps = _normalize_c5_details(c5_payload)
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for row in local_rebuys:
        source_id = _rebuy_source_id(row)
        if source_id is not None:
            grouped.setdefault(source_id, []).append(row)
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    gaps: list[str] = list(c5_gaps)
    for official in official_sales:
        local_sell_id = official_to_local.get(str(official["officialKey"]))
        row, row_failures, row_gaps = _classify_rebuy_chain(
            official=official,
            local_sell_id=local_sell_id,
            attempts=grouped.get(int(local_sell_id), []) if local_sell_id is not None else [],
            c5_details=c5_details,
            c5_coverage_complete=_provider_coverage(c5_payload),
        )
        rows.append(row)
        failures.extend(row_failures)
        gaps.extend(row_gaps)
    return rows, list(dict.fromkeys(failures)), list(dict.fromkeys(gaps))


def _item_conservation_rows(
    official_sales: list[dict[str, Any]],
    rebuy_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    items: dict[str, dict[str, int]] = {}
    destinations = (
        "c5_success",
        "manual_complete",
        "c5_delivery_pending",
        "pending_rebuy",
        "c5_submission_unconfirmed",
        "exception",
    )
    for sale in official_sales:
        item = str(sale.get("marketHashName") or "")
        bucket = items.setdefault(item, {"steam": 0, **{key: 0 for key in destinations}})
        bucket["steam"] += int(sale.get("quantity") or 1)
    for rebuy in rebuy_rows:
        item = str(rebuy.get("marketHashName") or "")
        bucket = items.setdefault(item, {"steam": 0, **{key: 0 for key in destinations}})
        destination = str(rebuy.get("destination") or "exception")
        if destination not in destinations:
            destination = "exception"
        bucket[destination] += int(rebuy.get("destinationQuantity") or 0)
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for item, counts in sorted(items.items()):
        routed = sum(counts[key] for key in destinations)
        difference = counts["steam"] - routed
        physically_restored = (
            counts["c5_success"] + counts["manual_complete"] == counts["steam"]
            and all(counts[key] == 0 for key in destinations[2:])
        )
        verdict = "passed" if difference == 0 else "failed"
        if difference != 0:
            failures.append(f"quantity conservation failed for {item}: difference={difference}")
        rows.append(
            {
                "rowKey": item or "<missing-market-hash-name>",
                "marketHashName": item,
                "steamSold": counts["steam"],
                "c5Success": counts["c5_success"],
                "manualComplete": counts["manual_complete"],
                "c5DeliveryPending": counts["c5_delivery_pending"],
                "pendingRebuy": counts["pending_rebuy"],
                "c5SubmissionUnconfirmed": counts["c5_submission_unconfirmed"],
                "exception": counts["exception"],
                "quantityDifference": difference,
                "physicallyRestored": physically_restored,
                "verdict": verdict,
                "reason": "quantity_conserved" if difference == 0 else "quantity_difference_nonzero",
            }
        )
    return rows, failures


def _profit_trade_purchase_index(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        note = _note(row.get("note"))
        receipt = note.get("steamPurchaseReceipt")
        if not isinstance(receipt, dict):
            receipt = {}
        discount = _decimal(row.get("steam_balance_discount"))
        if discount is None:
            discount = _decimal(note.get("steamCostRatio") or note.get("steamBalanceDiscount"))
        normalized.append(
            {
                "id": int(row["id"]),
                "accountId": str(note.get("steamAccountId") or "").strip() or None,
                "purchaseId": str(receipt.get("purchaseId") or note.get("steamPurchaseId") or "").strip()
                or None,
                "listingId": str(
                    receipt.get("listingId")
                    or note.get("steamListingId")
                    or row.get("steam_listing_id")
                    or ""
                ).strip()
                or None,
                "assetId": str(row.get("b_asset_id") or "").strip() or None,
                "marketHashName": str(row.get("market_hash_name") or "").strip(),
                "balanceDiscount": discount,
            }
        )
    return normalized


def _purchase_discount(
    purchase: Mapping[str, Any], profit_trades: list[dict[str, Any]]
) -> tuple[Decimal | None, int | None]:
    scored = [
        (_strong_match_score(purchase, row), row)
        for row in profit_trades
    ]
    best = max((score for score, _ in scored), default=0)
    candidates = [row for score, row in scored if score == best and score > 0]
    if len(candidates) != 1:
        return None, None
    return candidates[0].get("balanceDiscount"), int(candidates[0]["id"])


def _normalize_balance_evidence(
    payload: Mapping[str, Any],
    *,
    account_ids: list[str],
    expected_account_count: int,
) -> tuple[int | None, list[dict[str, Any]], list[str]]:
    gaps = _manifest_errors(payload, "steam_balance")
    if not _provider_coverage(payload):
        gaps.append("Steam wallet coverage is incomplete")
    accounts = payload.get("accounts") if isinstance(payload.get("accounts"), list) else []
    if len(accounts) != int(expected_account_count):
        gaps.append(f"Steam wallet account coverage {len(accounts)}/{expected_account_count}")
    observed = {
        str(row.get("accountId") or row.get("id") or "").strip()
        for row in accounts
        if isinstance(row, dict)
    }
    for account_id in account_ids:
        if account_id not in observed:
            gaps.append(f"Steam wallet is missing account {account_id}")
    total = 0
    normalized: list[dict[str, Any]] = []
    for raw in accounts:
        if not isinstance(raw, dict):
            gaps.append("Steam wallet account row is invalid")
            continue
        account_id = str(raw.get("accountId") or raw.get("id") or "").strip()
        if raw.get("coverageComplete") is not True:
            gaps.append(f"Steam wallet evidence is incomplete for {account_id or 'unknown account'}")
        currency_id = _normalize_currency_id(raw.get("currencyId"))
        if currency_id != 23:
            gaps.append(f"Steam wallet currency is not CNY for {account_id or 'unknown account'}")
        available = _amount_cents(
            raw,
            cents_keys=("availableBalanceCents", "realBalanceCents", "balanceCents"),
            amount_keys=("availableBalance", "realBalance", "balance"),
        )
        pending = _amount_cents(
            raw,
            cents_keys=("pendingBalanceCents", "delayedBalanceCents"),
            amount_keys=("pendingBalance", "delayedBalance", "delayed_balance"),
        )
        row_total = _amount_cents(
            raw,
            cents_keys=("totalBalanceCents",),
            amount_keys=("totalBalance",),
        )
        if row_total is None and available is not None and pending is not None:
            row_total = available + pending
        if row_total is None:
            gaps.append(f"Steam wallet amount is missing for {account_id or 'unknown account'}")
        else:
            total += row_total
        normalized.append(
            {
                "accountId": account_id or None,
                "availableCents": available,
                "pendingCents": pending,
                "totalCents": row_total,
                "currencyId": currency_id,
                "raw": raw,
            }
        )
    return (total if accounts else None), normalized, list(dict.fromkeys(gaps))


def _wallet_and_ratio_row(
    *,
    run_row: sqlite3.Row,
    official_sales: list[dict[str, Any]],
    official_purchases: list[dict[str, Any]],
    rebuy_rows: list[dict[str, Any]],
    profit_trade_rows: list[Mapping[str, Any]],
    balance_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    failures: list[str] = []
    account_ids = _json_loads(run_row["account_ids_json"], [])
    actual_balance_cents, balance_accounts, balance_gaps = _normalize_balance_evidence(
        balance_payload,
        account_ids=account_ids if isinstance(account_ids, list) else [],
        expected_account_count=int(run_row["expected_account_count"]),
    )
    gaps = list(balance_gaps)
    sale_net_cents = sum(int(row.get("netCents") or 0) for row in official_sales)
    purchase_cents = sum(int(row.get("paidCents") or 0) for row in official_purchases)
    initial_balance_cents = int(run_row["initial_balance_cents"])
    predicted_balance_cents = initial_balance_cents + sale_net_cents - purchase_cents
    balance_difference_cents = (
        actual_balance_cents - predicted_balance_cents
        if actual_balance_cents is not None
        else None
    )
    if (
        balance_difference_cents is not None
        and abs(balance_difference_cents) > int(run_row["balance_tolerance_cents"])
    ):
        failures.append(
            f"Steam wallet difference is {_money_text(balance_difference_cents)}"
        )

    realized_cents = 0
    realized_net_cents = 0
    expected_rebuy_cents = 0
    for row in rebuy_rows:
        amount_cents = _safe_int(row.get("effectiveAmountCents"))
        steam_net_cents = _money_cents(row.get("steamNet"))
        destination = str(row.get("destination") or "")
        if destination in {"c5_success", "manual_complete"}:
            if amount_cents is not None:
                realized_cents += amount_cents
                expected_rebuy_cents += amount_cents
            if steam_net_cents is not None:
                realized_net_cents += steam_net_cents
        elif destination in {"c5_delivery_pending", "pending_rebuy"}:
            if amount_cents is None:
                gaps.append(
                    f"{destination} row {row.get('rowKey')} is missing current effective amount"
                )
            else:
                expected_rebuy_cents += amount_cents

    realized_ratio = (
        Decimal(realized_cents) / Decimal(realized_net_cents)
        if realized_net_cents > 0
        else None
    )
    expected_ratio = (
        Decimal(expected_rebuy_cents) / Decimal(sale_net_cents)
        if sale_net_cents > 0
        else (Decimal(0) if expected_rebuy_cents == 0 else None)
    )
    reported_ratio = _decimal(run_row["reported_ratio"])
    if reported_ratio is None:
        gaps.append("guadao report comprehensive ratio was not supplied")
        report_ratio_difference = None
    elif expected_ratio is None:
        gaps.append("expected final rebuy ratio cannot be calculated")
        report_ratio_difference = None
    else:
        report_ratio_difference = expected_ratio - reported_ratio
        tolerance = _decimal(run_row["ratio_tolerance"]) or Decimal(0)
        if abs(report_ratio_difference) > tolerance:
            failures.append(
                "expected final rebuy ratio differs from guadao report comprehensive ratio"
            )

    profit_trades = _profit_trade_purchase_index(profit_trade_rows)
    purchase_real_cost = Decimal(0)
    purchase_rows: list[dict[str, Any]] = []
    for purchase in official_purchases:
        paid_cents = purchase.get("paidCents")
        discount, trade_id = _purchase_discount(purchase, profit_trades)
        if paid_cents is None:
            gaps.append(f"Steam purchase {purchase['officialKey']} is missing actual spend")
            continue
        if discount is None:
            gaps.append(
                f"Steam purchase {purchase['officialKey']} has no uniquely saved balance discount"
            )
            purchase_rows.append(
                {
                    "officialKey": purchase["officialKey"],
                    "paidAmount": _money_text(paid_cents),
                    "balanceDiscount": None,
                    "realCost": None,
                    "profitTradeId": trade_id,
                }
            )
            continue
        real_cost = (Decimal(int(paid_cents)) / 100) * discount
        purchase_real_cost += real_cost
        purchase_rows.append(
            {
                "officialKey": purchase["officialKey"],
                "paidAmount": _money_text(paid_cents),
                "balanceDiscount": _ratio_text(discount),
                "realCost": _decimal_text(real_cost, places=FOUR_PLACES),
                "profitTradeId": trade_id,
            }
        )

    initial_real_value = _decimal(run_row["initial_real_value"]) or Decimal(0)
    predicted_real_value = (
        initial_real_value
        + Decimal(expected_rebuy_cents) / 100
        - purchase_real_cost
    )
    ending_discount = (
        predicted_real_value / (Decimal(actual_balance_cents) / 100)
        if actual_balance_cents is not None and actual_balance_cents > 0
        else None
    )
    wallet_verdict = "failed" if failures else ("inconclusive" if gaps else "passed")
    row = {
        "rowKey": "wallet-and-discount",
        "initialBalance": _money_text(initial_balance_cents),
        "officialSaleNet": _money_text(sale_net_cents),
        "officialPurchaseSpend": _money_text(purchase_cents),
        "predictedEndingBalance": _money_text(predicted_balance_cents),
        "actualEndingBalance": _money_text(actual_balance_cents),
        "balanceDifference": _money_text(balance_difference_cents),
        "balanceTolerance": _money_text(int(run_row["balance_tolerance_cents"])),
        "balanceWithinTolerance": (
            balance_difference_cents is not None
            and abs(balance_difference_cents) <= int(run_row["balance_tolerance_cents"])
        ),
        "realizedRebuyAmount": _money_text(realized_cents),
        "realizedSteamNet": _money_text(realized_net_cents),
        "realizedRebuyRatio": _ratio_text(realized_ratio),
        "expectedRebuyAmount": _money_text(expected_rebuy_cents),
        "expectedFinalRebuyRatio": _ratio_text(expected_ratio),
        "reportedComprehensiveRatio": _ratio_text(reported_ratio),
        "reportedRatioDifference": _ratio_text(report_ratio_difference),
        "initialRealValue": _decimal_text(initial_real_value, places=FOUR_PLACES),
        "purchaseRealCost": _decimal_text(purchase_real_cost, places=FOUR_PLACES),
        "predictedEndingRealValue": _decimal_text(predicted_real_value, places=FOUR_PLACES),
        "endingBalanceDiscount": _ratio_text(ending_discount),
        "walletAccounts": balance_accounts,
        "purchaseCostRows": purchase_rows,
        "verdict": wallet_verdict,
        "reason": (
            ",".join(failures) if failures else (
                ",".join(gaps) if gaps else "wallet_and_discount_reconciled"
            )
        ),
    }
    return row, list(dict.fromkeys(failures)), list(dict.fromkeys(gaps))


def _persist_evidence(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    local_payload: Mapping[str, Any],
    steam_payload: Mapping[str, Any],
    c5_payload: Mapping[str, Any],
    balance_payload: Mapping[str, Any],
) -> None:
    now = _utc_now_iso()

    def insert(
        source: str,
        payload: Any,
        *,
        account_id: Any = None,
        external_id: Any = None,
        occurred_at: Any = None,
        amount_cents: Any = None,
        currency_id: Any = None,
        coverage_complete: bool | None = None,
    ) -> None:
        evidence_key = _hash_payload(source, account_id, external_id, occurred_at, payload)
        conn.execute(
            """
            INSERT OR IGNORE INTO guadao_audit_evidence (
                request_id, source, evidence_key, account_id, external_id,
                occurred_at, amount_cents, currency_id, coverage_complete,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                source,
                evidence_key,
                str(account_id) if account_id not in (None, "") else None,
                str(external_id) if external_id not in (None, "") else None,
                _normalized_time(occurred_at, assume_cn=False) if occurred_at else None,
                _safe_int(amount_cents),
                _normalize_currency_id(currency_id),
                None if coverage_complete is None else int(bool(coverage_complete)),
                _json_dumps(payload),
                now,
            ),
        )

    insert("local_manifest", local_payload, coverage_complete=_provider_coverage(local_payload))
    for row in local_payload.get("sales", []):
        insert("local_sell", row, external_id=row.get("id"))
    for row in local_payload.get("rebuys", []):
        insert("local_rebuy", row, external_id=row.get("id"))
    for row in local_payload.get("profitTrades", []):
        insert("local_profit_trade", row, external_id=row.get("id"))
    for row in local_payload.get("manualAuditEvents", []):
        insert("local_manual_audit", row, external_id=row.get("id"))

    insert("steam_manifest", steam_payload, coverage_complete=_provider_coverage(steam_payload))
    for account in steam_payload.get("accounts", []) if isinstance(steam_payload.get("accounts"), list) else []:
        if not isinstance(account, dict):
            continue
        account_id = account.get("accountId") or account.get("id")
        for row in account.get("sales", []) if isinstance(account.get("sales"), list) else []:
            if isinstance(row, dict):
                insert(
                    "steam_sale",
                    row,
                    account_id=account_id,
                    external_id=row.get("purchaseId") or row.get("listingId"),
                    occurred_at=row.get("soldAt") or row.get("timeSold"),
                    amount_cents=_amount_cents(
                        row,
                        cents_keys=("netAmountCents", "receivedAmountCents"),
                        amount_keys=("netAmount", "receivedAmount"),
                    ),
                    currency_id=row.get("currencyId") or row.get("receivedCurrencyId"),
                    coverage_complete=account.get("coverageComplete") is True,
                )
        for row in account.get("purchases", []) if isinstance(account.get("purchases"), list) else []:
            if isinstance(row, dict):
                insert(
                    "steam_purchase",
                    row,
                    account_id=account_id,
                    external_id=row.get("purchaseId") or row.get("listingId"),
                    occurred_at=row.get("purchasedAt") or row.get("timePurchased"),
                    amount_cents=_amount_cents(
                        row,
                        cents_keys=("paidAmountCents", "paidTotalCents"),
                        amount_keys=("paidAmount", "paidTotal"),
                    ),
                    currency_id=row.get("currencyId"),
                    coverage_complete=account.get("coverageComplete") is True,
                )

    insert("c5_manifest", c5_payload, coverage_complete=_provider_coverage(c5_payload))
    for row in c5_payload.get("orders", []) if isinstance(c5_payload.get("orders"), list) else []:
        if isinstance(row, dict):
            insert(
                "c5_buyer_order_detail",
                row,
                external_id=(row.get("orderId") or row.get("orderAssetId")),
                amount_cents=_amount_cents(
                    row,
                    cents_keys=("actualAmountCents", "actualPayCents"),
                    amount_keys=("actualAmount", "actualPay", "price"),
                ),
                coverage_complete=_provider_coverage(c5_payload),
            )

    insert("steam_balance_manifest", balance_payload, coverage_complete=_provider_coverage(balance_payload))
    for row in balance_payload.get("accounts", []) if isinstance(balance_payload.get("accounts"), list) else []:
        if isinstance(row, dict):
            insert(
                "steam_balance",
                row,
                account_id=row.get("accountId") or row.get("id"),
                amount_cents=_amount_cents(
                    row,
                    cents_keys=("totalBalanceCents",),
                    amount_keys=("totalBalance",),
                ),
                currency_id=row.get("currencyId"),
                coverage_complete=row.get("coverageComplete") is True,
            )


def _persist_result_rows(
    conn: sqlite3.Connection,
    request_id: str,
    tables: Mapping[str, list[dict[str, Any]]],
) -> None:
    now = _utc_now_iso()
    for table_name, rows in tables.items():
        for index, row in enumerate(rows):
            row_key = str(row.get("rowKey") or f"{table_name}-{index}")
            conn.execute(
                """
                INSERT INTO guadao_audit_checks (
                    request_id, table_name, row_key, verdict,
                    expected_value, actual_value, difference_value,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    table_name,
                    row_key,
                    str(row.get("verdict") or "inconclusive"),
                    row.get("officialNet") or row.get("predictedEndingBalance"),
                    row.get("programNet") or row.get("actualEndingBalance"),
                    row.get("netDifference") or row.get("balanceDifference"),
                    _json_dumps(row),
                    now,
                ),
            )
            if table_name in {"steam_sales", "rebuys"}:
                conn.execute(
                    """
                    INSERT INTO guadao_audit_matches (
                        request_id, match_type, row_key, official_key,
                        local_operation_id, source_sell_operation_id,
                        effective_rebuy_operation_id, verdict, reason,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        table_name,
                        row_key,
                        row_key if not row_key.startswith("local-") else None,
                        _safe_int(row.get("localOperationId")),
                        _safe_int(row.get("sourceSellOperationId")),
                        _safe_int(row.get("effectiveRebuyOperationId")),
                        str(row.get("verdict") or "inconclusive"),
                        str(row.get("reason") or "") or None,
                        _json_dumps(row),
                        now,
                    ),
                )


def _set_stage(settings: Settings, request_id: str, stage: str) -> bool:
    now = _utc_now_iso()
    with _managed_connection(settings) as conn:
        cursor = conn.execute(
            """
            UPDATE guadao_audit_runs
            SET stage = ?, updated_at = ?
            WHERE request_id = ? AND status = 'running' AND cancel_requested = 0
            """,
            (stage, now, request_id),
        )
        return bool(cursor.rowcount)


def _terminal_result(settings: Settings, request_id: str) -> dict[str, Any]:
    result = get_guadao_audit_run(settings, request_id)
    if result is None:
        raise KeyError(f"guadao audit run not found: {request_id}")
    result["tables"] = list_guadao_audit_rows(settings, request_id)
    return result


def run_guadao_audit(
    settings: Settings,
    request_id: str,
    *,
    steam_evidence_provider: Any | None = None,
    c5_evidence_provider: Any | None = None,
    balance_evidence_provider: Any | None = None,
) -> dict[str, Any]:
    """Run one immutable, read-only reconciliation attempt.

    Only the four guadao_audit_* tables are mutated. Source trading data is
    opened through a SQLite read-only connection.
    """

    initialize_guadao_audit_schema(settings)
    run_id = str(request_id).strip()
    now = _utc_now_iso()
    with _managed_connection(settings) as conn:
        row = conn.execute(
            "SELECT * FROM guadao_audit_runs WHERE request_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"guadao audit run not found: {run_id}")
        if str(row["status"]) in TERMINAL_STATUSES:
            return _terminal_result(settings, run_id)
        cursor = conn.execute(
            """
            UPDATE guadao_audit_runs
            SET status = 'running', stage = 'local_collecting',
                started_at = COALESCE(started_at, ?), updated_at = ?, error = NULL
            WHERE request_id = ? AND status = 'pending' AND cancel_requested = 0
            """,
            (now, now, run_id),
        )
        if not cursor.rowcount:
            raise RuntimeError("guadao audit run is already running or cannot be claimed")
        run_row = conn.execute(
            "SELECT * FROM guadao_audit_runs WHERE request_id = ?", (run_id,)
        ).fetchone()
    assert run_row is not None

    try:
        local_payload = _read_local_evidence(settings)
        if not _set_stage(settings, run_id, "steam_collecting"):
            return _terminal_result(settings, run_id)
        account_ids = _json_loads(run_row["account_ids_json"], [])
        provider_context = {
            "settings": settings,
            "start_at": run_row["start_at"],
            "end_at": run_row["end_at"],
            "account_ids": account_ids if isinstance(account_ids, list) else [],
            "expected_account_count": int(run_row["expected_account_count"]),
            "request_id": run_id,
        }
        steam_payload = _call_provider(
            steam_evidence_provider,
            default_provider=_default_steam_evidence_provider,
            method_names=("collect_steam_evidence", "collect"),
            kwargs=provider_context,
            source_name="Steam history",
        )
        if not _set_stage(settings, run_id, "c5_collecting"):
            return _terminal_result(settings, run_id)
        order_ids = [
            order_id
            for row in local_payload.get("rebuys", [])
            for order_id in _rebuy_order_ids(row)
        ]
        c5_payload = _call_provider(
            c5_evidence_provider,
            default_provider=_default_c5_evidence_provider,
            method_names=("collect_c5_evidence", "collect"),
            kwargs={
                **provider_context,
                "order_ids": list(dict.fromkeys(order_ids)),
                "local_rebuys": local_payload.get("rebuys", []),
            },
            source_name="C5",
        )
        if not _set_stage(settings, run_id, "balance_collecting"):
            return _terminal_result(settings, run_id)
        balance_payload = _call_provider(
            balance_evidence_provider,
            default_provider=_default_balance_evidence_provider,
            method_names=("collect_balance_evidence", "collect"),
            kwargs=provider_context,
            source_name="Steam balance",
        )
        if not _set_stage(settings, run_id, "reconciling"):
            return _terminal_result(settings, run_id)

        start = _parse_datetime(run_row["start_at"], assume_cn=False)
        end = _parse_datetime(run_row["end_at"], assume_cn=False)
        if start is None or end is None:
            raise RuntimeError("persisted audit window is invalid")
        official_sales, official_purchases, steam_gaps = _normalize_steam_evidence(
            steam_payload,
            start_at=start,
            end_at=end,
            account_ids=provider_context["account_ids"],
            expected_account_count=provider_context["expected_account_count"],
        )
        sale_rows, official_to_local, sale_failures, sale_gaps = _sale_reconciliation(
            official_sales,
            local_payload.get("sales", []),
            start_at=start,
            end_at=end,
            official_coverage_complete=_provider_coverage(steam_payload),
        )
        rebuy_rows, rebuy_failures, rebuy_gaps = _rebuy_reconciliation(
            official_sales,
            official_to_local,
            local_payload.get("rebuys", []),
            c5_payload,
        )
        item_rows, item_failures = _item_conservation_rows(official_sales, rebuy_rows)
        wallet_row, wallet_failures, wallet_gaps = _wallet_and_ratio_row(
            run_row=run_row,
            official_sales=official_sales,
            official_purchases=official_purchases,
            rebuy_rows=rebuy_rows,
            profit_trade_rows=local_payload.get("profitTrades", []),
            balance_payload=balance_payload,
        )
        failures = list(
            dict.fromkeys(
                sale_failures + rebuy_failures + item_failures + wallet_failures
            )
        )
        gaps = list(
            dict.fromkeys(
                ([] if _provider_coverage(local_payload) else ["local database coverage is incomplete"])
                + _manifest_errors(local_payload, "local")
                + steam_gaps
                + sale_gaps
                + rebuy_gaps
                + wallet_gaps
            )
        )
        evidence_complete = not gaps
        final_status = "inconclusive" if gaps else ("failed" if failures else "passed")
        tables = {
            "steam_sales": sale_rows,
            "rebuys": rebuy_rows,
            "item_conservation": item_rows,
            "wallet_discount": [wallet_row],
        }
        summary = {
            "evidenceComplete": evidence_complete,
            "evidenceGaps": gaps,
            "failures": failures,
            "programSalesEqualOfficial": not any(
                row.get("verdict") == "failed" for row in sale_rows
            ),
            "allSalesHaveDestination": not any(
                row.get("destination") == "exception" for row in rebuy_rows
            ),
            "allItemsConserved": all(
                int(row.get("quantityDifference") or 0) == 0 for row in item_rows
            ),
            "physicalInventoryRestored": all(
                bool(row.get("physicallyRestored")) for row in item_rows
            ),
            "walletReconciled": bool(wallet_row.get("balanceWithinTolerance")),
            "realizedRebuyRatio": wallet_row.get("realizedRebuyRatio"),
            "expectedFinalRebuyRatio": wallet_row.get("expectedFinalRebuyRatio"),
            "endingBalanceDiscount": wallet_row.get("endingBalanceDiscount"),
            "tableCounts": {key: len(value) for key, value in tables.items()},
        }
        coverage = {
            "local": _provider_coverage(local_payload),
            "steamHistory": _provider_coverage(steam_payload),
            "c5": _provider_coverage(c5_payload),
            "steamBalance": _provider_coverage(balance_payload),
        }
        finished_at = _utc_now_iso()
        with _managed_connection(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status, cancel_requested FROM guadao_audit_runs WHERE request_id = ?",
                (run_id,),
            ).fetchone()
            if current is None:
                conn.rollback()
                raise KeyError(f"guadao audit run not found: {run_id}")
            if bool(current["cancel_requested"]) or str(current["status"]) == "cancelled":
                conn.rollback()
                return _terminal_result(settings, run_id)
            conn.execute("DELETE FROM guadao_audit_evidence WHERE request_id = ?", (run_id,))
            conn.execute("DELETE FROM guadao_audit_matches WHERE request_id = ?", (run_id,))
            conn.execute("DELETE FROM guadao_audit_checks WHERE request_id = ?", (run_id,))
            _persist_evidence(
                conn,
                run_id,
                local_payload=local_payload,
                steam_payload=steam_payload,
                c5_payload=c5_payload,
                balance_payload=balance_payload,
            )
            _persist_result_rows(conn, run_id, tables)
            conn.execute(
                """
                UPDATE guadao_audit_runs
                SET status = ?, stage = 'finished', coverage_json = ?,
                    summary_json = ?, error = NULL,
                    finished_at = ?, updated_at = ?
                WHERE request_id = ? AND status = 'running' AND cancel_requested = 0
                """,
                (
                    final_status,
                    _json_dumps(coverage),
                    _json_dumps(summary),
                    finished_at,
                    finished_at,
                    run_id,
                ),
            )
            conn.commit()
        return _terminal_result(settings, run_id)
    except Exception as exc:
        finished_at = _utc_now_iso()
        summary = {
            "evidenceComplete": False,
            "evidenceGaps": [f"audit execution failed before complete evidence: {exc}"],
            "failures": [],
        }
        with _managed_connection(settings) as conn:
            conn.execute(
                """
                UPDATE guadao_audit_runs
                SET status = 'inconclusive', stage = 'finished',
                    summary_json = ?, error = ?, finished_at = ?, updated_at = ?
                WHERE request_id = ? AND status = 'running' AND cancel_requested = 0
                """,
                (_json_dumps(summary), str(exc), finished_at, finished_at, run_id),
            )
        return _terminal_result(settings, run_id)


def list_guadao_audit_rows(
    settings: Settings,
    request_id: str,
    table: str | None = None,
) -> dict[str, list[dict[str, Any]]] | list[dict[str, Any]]:
    initialize_guadao_audit_schema(settings)
    if table is not None and table not in AUDIT_TABLE_NAMES:
        raise ValueError(f"unsupported guadao audit table: {table}")
    names = (table,) if table else AUDIT_TABLE_NAMES
    result: dict[str, list[dict[str, Any]]] = {}
    with _managed_connection(settings) as conn:
        exists = conn.execute(
            "SELECT 1 FROM guadao_audit_runs WHERE request_id = ?", (str(request_id),)
        ).fetchone()
        if exists is None:
            raise KeyError(f"guadao audit run not found: {request_id}")
        for table_name in names:
            rows = conn.execute(
                """
                SELECT payload_json FROM guadao_audit_checks
                WHERE request_id = ? AND table_name = ?
                ORDER BY id ASC
                """,
                (str(request_id), table_name),
            ).fetchall()
            result[table_name] = [
                payload
                for row in rows
                for payload in [_json_loads(row["payload_json"], {})]
                if isinstance(payload, dict)
            ]
    return result[table] if table else result


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_无记录_"
    preferred = [
        "rowKey",
        "marketHashName",
        "accountId",
        "listingId",
        "purchaseId",
        "assetId",
        "officialSoldAt",
        "programSoldAt",
        "officialGross",
        "sourceSellOperationId",
        "destination",
        "officialNet",
        "programNet",
        "effectiveAmount",
        "steamSold",
        "c5Success",
        "manualComplete",
        "c5DeliveryPending",
        "pendingRebuy",
        "c5SubmissionUnconfirmed",
        "exception",
        "quantityDifference",
        "physicallyRestored",
        "initialBalance",
        "officialSaleNet",
        "officialPurchaseSpend",
        "predictedEndingBalance",
        "actualEndingBalance",
        "balanceDifference",
        "balanceTolerance",
        "balanceWithinTolerance",
        "realizedRebuyAmount",
        "realizedRebuyRatio",
        "expectedRebuyAmount",
        "expectedFinalRebuyRatio",
        "reportedComprehensiveRatio",
        "reportedRatioDifference",
        "initialRealValue",
        "purchaseRealCost",
        "predictedEndingRealValue",
        "endingBalanceDiscount",
        "verdict",
        "reason",
    ]
    keys = [key for key in preferred if any(key in row for row in rows)]
    if not keys:
        keys = list(rows[0].keys())[:12]
    header = "| " + " | ".join(keys) + " |"
    separator = "| " + " | ".join("---" for _ in keys) + " |"

    def cell(value: Any) -> str:
        if isinstance(value, (dict, list)):
            value = _json_dumps(value)
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    body = ["| " + " | ".join(cell(row.get(key)) for key in keys) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def export_guadao_audit(
    settings: Settings,
    request_id: str,
    format_name: str,
) -> dict[str, str]:
    normalized = str(format_name or "").strip().lower()
    if normalized not in SUPPORTED_EXPORT_FORMATS:
        raise ValueError(
            "unsupported export format; only json, csv and markdown are allowed"
        )
    run = get_guadao_audit_run(settings, request_id)
    if run is None:
        raise KeyError(f"guadao audit run not found: {request_id}")
    tables = list_guadao_audit_rows(settings, request_id)
    if normalized == "json":
        content = json.dumps(
            {"run": run, "tables": tables},
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
        suffix = "json"
        content_type = "application/json; charset=utf-8"
    elif normalized == "csv":
        flat_rows: list[dict[str, Any]] = []
        for table_name, rows in tables.items():
            for row in rows:
                flat_rows.append(
                    {
                        "table": table_name,
                        "rowKey": row.get("rowKey"),
                        "verdict": row.get("verdict"),
                        "marketHashName": row.get("marketHashName"),
                        "reason": row.get("reason"),
                        "payloadJson": _json_dumps(row),
                    }
                )
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream,
            fieldnames=("table", "rowKey", "verdict", "marketHashName", "reason", "payloadJson"),
        )
        writer.writeheader()
        writer.writerows(flat_rows)
        content = stream.getvalue()
        suffix = "csv"
        content_type = "text/csv; charset=utf-8"
    else:
        title_by_table = {
            "steam_sales": "1. 程序卖出与 Steam 官方卖出",
            "rebuys": "2. 补仓与 C5/手动完结",
            "item_conservation": "3. 物品数量守恒",
            "wallet_discount": "4. Steam 钱包与综合折扣",
        }
        sections = [
            f"# 挂刀执行器对账报告 {request_id}",
            "",
            f"- 结论：{run['status']}",
            f"- 区间：{run['startAt']} 至 {run['endAt']}",
            f"- 证据完整：{run.get('summary', {}).get('evidenceComplete')}",
        ]
        for table_name in AUDIT_TABLE_NAMES:
            sections.extend(
                ["", f"## {title_by_table[table_name]}", "", _markdown_table(tables[table_name])]
            )
        content = "\n".join(sections) + "\n"
        suffix = "md"
        content_type = "text/markdown; charset=utf-8"
    return {
        "filename": f"guadao-audit-{request_id}.{suffix}",
        "contentType": content_type,
        "content": content,
    }


__all__ = [
    "SUPPORTED_EXPORT_FORMATS",
    "TERMINAL_STATUSES",
    "initialize_guadao_audit_schema",
    "create_guadao_audit_run",
    "get_guadao_audit_run",
    "run_guadao_audit",
    "list_guadao_audit_rows",
    "retry_guadao_audit_run",
    "cancel_guadao_audit_run",
    "retry_guadao_audit",
    "cancel_guadao_audit",
    "export_guadao_audit",
]
