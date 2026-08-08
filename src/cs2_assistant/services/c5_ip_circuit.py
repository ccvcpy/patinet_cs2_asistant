from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from cs2_assistant.clients.c5game import C5GameClient, C5GameError
from cs2_assistant.clients.serverchan import ServerChanClient
from cs2_assistant.config import Settings
from cs2_assistant.db import Database


C5_IP_WHITELIST_ERROR_CODE = 499100
C5_IP_CIRCUIT_KEY = "global_ip_whitelist"
C5_IP_PROBE_INTERVAL_SECONDS = 5 * 60


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_payload(row: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(row["payload_json"] or "{}"))
    except (TypeError, ValueError, KeyError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_c5_ip_circuit_open(db: Database) -> bool:
    row = db.get_c5_api_circuit(C5_IP_CIRCUIT_KEY)
    return row is not None and str(row["state"]) == "open"


def build_c5_ip_request_guard(settings: Settings) -> Callable[[], bool]:
    def guard() -> bool:
        db = Database(settings.db_path)
        try:
            db.initialize()
            return not is_c5_ip_circuit_open(db)
        except Exception:
            # A local circuit read failure must fail closed for real C5 execution.
            return False
        finally:
            db.close()

    return guard


def _send_alert(settings: Settings, db: Database, *, recovery: bool) -> bool:
    if not settings.serverchan_sendkey:
        return False
    claim = db.claim_c5_api_alert(circuit_key=C5_IP_CIRCUIT_KEY, recovery=recovery)
    if claim is None:
        return False
    row = db.get_c5_api_circuit(C5_IP_CIRCUIT_KEY)
    if row is None:
        db.release_c5_api_alert_claim(
            claim,
            circuit_key=C5_IP_CIRCUIT_KEY,
            recovery=recovery,
        )
        return False
    payload = _parse_payload(row)
    if recovery:
        title = "[C5 API] IP 白名单已恢复，执行已解锁"
        body = (
            f"恢复时间: {row['recovered_at'] or '-'}\n"
            f"此前请求 IP: {row['request_ip'] or '-'}\n"
            "处理: Profit Trade 与挂刀的 C5 执行动作已恢复。"
        )
    else:
        title = "[C5 API] IP 白名单异常，执行已暂停"
        body = (
            f"错误码: {row['error_code'] or C5_IP_WHITELIST_ERROR_CODE}\n"
            f"当前请求 IP: {row['request_ip'] or '-'}\n"
            f"触发来源: {row['trigger_source'] or '-'}\n"
            f"触发接口: {row['trigger_operation'] or '-'}\n"
            f"关联流水: {payload.get('trade_no') or payload.get('operation_id') or '-'}\n"
            "处理: 已暂停 Profit Trade 与挂刀所有新的 C5 请求；"
            "Steam 挂单确认和成交终态同步继续运行。\n"
            "请将当前 IP 加入 C5 API 白名单。程序每 5 分钟只读探测一次，恢复后会再次通知。"
        )
    try:
        ServerChanClient(
            settings.serverchan_sendkey,
            settings.serverchan_base_url,
            timeout=10,
        ).send(title, body)
    except Exception:
        db.release_c5_api_alert_claim(
            claim,
            circuit_key=C5_IP_CIRCUIT_KEY,
            recovery=recovery,
        )
        return False
    return True


def notify_c5_ip_circuit_if_pending(settings: Settings, db: Database) -> bool:
    return _send_alert(settings, db, recovery=False)


def trip_c5_ip_circuit(settings: Settings, event: dict[str, Any]) -> bool:
    """Persist the first 499100 globally and immediately issue one alert."""

    try:
        error_code = int(event.get("error_code") or 0)
    except (TypeError, ValueError):
        return False
    if error_code != C5_IP_WHITELIST_ERROR_CODE:
        return False
    source = str(event.get("source") or "unknown")
    operation = str(event.get("operation") or "c5_http_request")
    safe_payload = {
        key: event.get(key)
        for key in (
            "trade_id",
            "trade_no",
            "market_hash_name",
            "asset_id",
            "account_id",
            "steam_id64",
            "request_id",
            "endpoint",
        )
        if event.get(key) not in (None, "")
    }
    db = Database(settings.db_path)
    try:
        db.initialize()
        _, newly_opened = db.trip_c5_api_circuit(
            circuit_key=C5_IP_CIRCUIT_KEY,
            error_code=error_code,
            request_ip=str(event.get("request_ip") or "").strip() or None,
            trigger_source=source,
            trigger_operation=operation,
            next_probe_at=_now_utc() + timedelta(seconds=C5_IP_PROBE_INTERVAL_SECONDS),
            payload=safe_payload,
        )
        _send_alert(settings, db, recovery=False)
        return newly_opened
    finally:
        db.close()


def bind_c5_ip_circuit_telemetry(
    settings: Settings,
    *,
    source: str,
    downstream: Callable[[dict[str, Any]], None] | None = None,
) -> Callable[[dict[str, Any]], None]:
    """Compose business logging with the shared IP circuit without changing C5 results."""

    def callback(event: dict[str, Any]) -> None:
        normalized = {"source": source, **dict(event)}
        if downstream is not None:
            try:
                downstream(normalized)
            except Exception:
                pass
        try:
            trip_c5_ip_circuit(settings, normalized)
        except Exception:
            # Circuit observability must not rewrite the C5 request result.
            pass

    return callback


def probe_c5_ip_circuit(
    settings: Settings,
    db: Database,
    *,
    worker_id: str,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Run the single leased read-only recovery probe when it becomes due."""

    claimed = db.claim_c5_api_probe(
        worker_id,
        circuit_key=C5_IP_CIRCUIT_KEY,
        lease_seconds=45,
    )
    if claimed is None:
        return {"probed": False, "state": "open" if is_c5_ip_circuit_open(db) else "closed"}
    try:
        effective_api_key = str(api_key or settings.c5_api_key or "").strip()
        if not effective_api_key:
            raise C5GameError("missing C5 API key")
        C5GameClient(
            effective_api_key,
            settings.c5_base_url,
            timeout=10,
        ).steam_info()
    except Exception as exc:
        db.defer_c5_api_probe(
            worker_id,
            circuit_key=C5_IP_CIRCUIT_KEY,
            next_probe_at=_now_utc() + timedelta(seconds=C5_IP_PROBE_INTERVAL_SECONDS),
        )
        return {"probed": True, "recovered": False, "error": str(exc)[:300]}
    recovered = db.recover_c5_api_circuit(worker_id, circuit_key=C5_IP_CIRCUIT_KEY)
    if recovered:
        _send_alert(settings, db, recovery=True)
    return {"probed": True, "recovered": recovered}
