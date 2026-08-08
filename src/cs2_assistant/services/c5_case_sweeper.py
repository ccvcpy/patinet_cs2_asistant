from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from cs2_assistant.accounts import AccountStore
from cs2_assistant.clients.c5game import C5GameClient
from cs2_assistant.config import PROJECT_ROOT, Settings
from cs2_assistant.db import Database


DEFAULT_MARKET_HASH_NAME = "Kilowatt Case"
DEFAULT_DISPLAY_NAME = "千瓦武器箱"
DEFAULT_INTERVAL_SECONDS = 60
C5_MARKET_SEARCH_MAX_PAGE_SIZE = 50
C5_MARKET_LIST_MAX_PAGES_PER_CYCLE = 5
STATE_VERSION = 3
# Keep the existing filename so an already-created v2 task can be migrated in place.
DEFAULT_STATE_PATH = Path(
    os.environ.get("CS2_ASSISTANT_C5_SWEEPER_STATE_PATH")
    or PROJECT_ROOT / "data" / "c5_case_sweeper_v2_state.json"
)
TERMINAL_STATUSES = {"completed", "stopped"}
EDITABLE_STATUSES = {"draft", "paused"}


class C5SweeperClient(Protocol):
    def steam_info(self) -> dict[str, Any]: ...

    def price_batch(self, market_hash_names: list[str], app_id: int = 730) -> dict[str, Any]: ...

    def market_products_list(self, **kwargs: Any) -> dict[str, Any]: ...
    def batch_buy(self, **kwargs: Any) -> dict[str, Any]: ...

    def buyer_order_status(self, **kwargs: Any) -> dict[str, Any]: ...

    def buyer_order_detail(self, order_id: str) -> dict[str, Any]: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_order_asset_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("orderAssetId", "order_asset_id", "assetOrderId", "asset_order_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    for key in ("data", "order", "orderInfo"):
        found = _extract_order_asset_id(payload.get(key))
        if found:
            return found
    return None


def _extract_trade_order_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("orderId", "order_id", "tradeOrderId", "trade_order_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _delivery_status(detail: dict[str, Any]) -> str:
    status = _safe_int(detail.get("status"))
    name = str(detail.get("statusName") or "").strip().lower()
    failed_code = str(detail.get("failedCode") or "").strip()
    failed_desc = str(detail.get("failedDesc") or "").strip()
    if status == 11 or name in {"failed", "fail", "failure", "失败", "已失败"} or failed_code or failed_desc:
        return "failed"
    if status == 10 or name in {
        "success", "succeeded", "complete", "completed", "finished", "done",
        "delivered", "received", "已完成", "已收货", "发货成功", "成功",
    }:
        return "delivered"
    return "pending"


def _is_weapon_case(market_hash_name: str, display_name: str | None = None) -> bool:
    """Only weapon cases are supported by this C5 batch-buy execution mode."""
    normalized = market_hash_name.strip().lower()
    chinese = str(display_name or "").strip()
    return normalized.endswith(" case") or chinese.endswith("武器箱")


def _steam_id64_from_trade_url(trade_url: str | None) -> str | None:
    if not trade_url:
        return None
    match = re.search(r"[?&]partner=(\d+)", trade_url)
    if not match:
        return None
    return str(76561197960265728 + int(match.group(1)))


class C5CaseSweeper:
    """C5 multi-item sweeper with independent, persistent campaign rounds."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: C5SweeperClient | None = None,
        account_store: AccountStore | None = None,
        state_path: Path = DEFAULT_STATE_PATH,
        now: Callable[[], datetime] = _utc_now,
        start_worker: bool = True,
    ) -> None:
        self.settings = settings
        self.client = client or C5GameClient(str(settings.c5_api_key or ""), settings.c5_base_url)
        self.account_store = account_store or AccountStore(PROJECT_ROOT / "config")
        self.state_path = Path(state_path)
        self._now = now
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._next_idle_audit_at = self._now()
        self._receiving_accounts_cache: tuple[datetime, list[dict[str, Any]]] | None = None
        self._state = self._load_state()
        restarted = False
        for round_data in self._state["rounds"]:
            if round_data.get("status") == "running":
                round_data["status"] = "paused"
                round_data["nextRunAt"] = None
                round_data["lastMessage"] = "后端已重启；为安全起见，真实扫货已暂停，请手动继续。"
                restarted = True
        if restarted:
            self._save_locked()
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(target=self._worker, name="c5-case-sweeper", daemon=True)
            self._thread.start()

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "nextRoundNumber": 1,
            "currentRoundId": None,
            "recentItems": [
                {"marketHashName": DEFAULT_MARKET_HASH_NAME, "displayName": DEFAULT_DISPLAY_NAME},
            ],
            "rounds": [],
        }

    def _migrate_v2_task(self, task: dict[str, Any]) -> dict[str, Any]:
        now = _iso(self._now())
        max_price = round(float(task.get("maxPrice") or 1.10), 2)
        target_count = max(1, int(task.get("targetCount") or 100))
        return {
            "id": str(task.get("id") or uuid.uuid4().hex),
            "roundNumber": 1,
            "marketHashName": str(task.get("marketHashName") or DEFAULT_MARKET_HASH_NAME),
            "displayName": str(task.get("displayName") or DEFAULT_DISPLAY_NAME),
            "maxPrice": max_price,
            "budget": round(max_price * target_count, 2),
            "targetCount": target_count,
            "intervalSeconds": DEFAULT_INTERVAL_SECONDS,
            "delivery": int(task.get("delivery") or 2),
            "receivingAccountId": task.get("receivingAccountId"),
            "receivingAccountName": task.get("receivingAccountName"),
            "receivingSteamId": task.get("receivingSteamId"),
            "status": str(task.get("status") or "draft"),
            "stopReason": task.get("stopReason"),
            "createdAt": str(task.get("createdAt") or now),
            "startedAt": task.get("startedAt"),
            "updatedAt": str(task.get("updatedAt") or now),
            "completedAt": task.get("completedAt"),
            "nextRunAt": task.get("nextRunAt"),
            "lastRunAt": task.get("lastRunAt"),
            "lastPrice": task.get("lastPrice"),
            "lastMessage": str(task.get("lastMessage") or "旧版任务已迁移为第 1 轮。"),
            "attemptCount": int(task.get("attemptCount") or 0),
            "orders": list(task.get("orders") or [])[-500:],
            "events": list(task.get("events") or [])[-120:],
        }

    def _load_state(self) -> dict[str, Any]:
        state = self._default_state()
        if not self.state_path.exists():
            return state
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return state
        if not isinstance(loaded, dict):
            return state
        if isinstance(loaded.get("task"), dict):
            migrated = self._migrate_v2_task(loaded["task"])
            state.update({
                "nextRoundNumber": 2,
                "currentRoundId": migrated["id"],
                "recentItems": [{
                    "marketHashName": migrated["marketHashName"],
                    "displayName": migrated["displayName"],
                }],
                "rounds": [migrated],
            })
            return state
        rounds = loaded.get("rounds")
        if not isinstance(rounds, list):
            return state
        clean_rounds: list[dict[str, Any]] = []
        for row in rounds[-100:]:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            row = dict(row)
            # Purchase submission cadence is a safety boundary, not a tunable strategy value.
            row["intervalSeconds"] = DEFAULT_INTERVAL_SECONDS
            row["orders"] = list(row.get("orders") or [])[-500:]
            row["events"] = list(row.get("events") or [])[-120:]
            clean_rounds.append(row)
        state.update(loaded)
        state["version"] = STATE_VERSION
        state["rounds"] = clean_rounds
        state["recentItems"] = list(state.get("recentItems") or [])[-10:]
        state["nextRoundNumber"] = max(
            int(state.get("nextRoundNumber") or 1),
            max((int(row.get("roundNumber") or 0) for row in clean_rounds), default=0) + 1,
        )
        if state.get("currentRoundId") not in {row["id"] for row in clean_rounds}:
            state["currentRoundId"] = clean_rounds[-1]["id"] if clean_rounds else None
        return state

    def _save_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _round_locked(self, round_id: str | None = None, *, required: bool = True) -> dict[str, Any] | None:
        target_id = round_id or self._state.get("currentRoundId")
        if target_id:
            for row in self._state["rounds"]:
                if str(row.get("id")) == str(target_id):
                    return row
        if required:
            raise ValueError("扫货轮次不存在")
        return None

    @staticmethod
    def _counts(round_data: dict[str, Any]) -> dict[str, int]:
        orders = round_data.get("orders") or []
        return {
            "accepted": len(orders),
            "delivered": sum(1 for row in orders if row.get("status") == "delivered"),
            "pending": sum(1 for row in orders if row.get("status") == "pending"),
            "failed": sum(1 for row in orders if row.get("status") == "failed"),
        }

    @staticmethod
    def _money(round_data: dict[str, Any]) -> dict[str, float | int]:
        orders = round_data.get("orders") or []
        accepted = round(sum(float(row.get("actualPay") or 0) for row in orders), 2)
        committed = round(sum(
            float(row.get("actualPay") or 0)
            for row in orders
            if row.get("status") in {"pending", "delivered"}
        ), 2)
        settled = round(sum(
            float(row.get("actualPay") or 0)
            for row in orders
            if row.get("status") == "delivered"
        ), 2)
        failed = round(sum(
            float(row.get("actualPay") or 0)
            for row in orders
            if row.get("status") == "failed"
        ), 2)
        budget = round(float(round_data.get("budget") or 0), 2)
        max_price = float(round_data.get("maxPrice") or 0)
        return {
            "budget": budget,
            "acceptedAmount": accepted,
            "committedAmount": committed,
            "settledAmount": settled,
            "failedAmount": failed,
            "remainingBudget": round(max(0.0, budget - committed), 2),
            "averageAcceptedPrice": round(accepted / len(orders), 2) if orders else 0.0,
            "averageDeliveredPrice": round(
                settled / max(1, sum(1 for row in orders if row.get("status") == "delivered")),
                2,
            ) if settled else 0.0,
            "maxAffordableCount": math.floor(budget / max_price) if max_price > 0 else 0,
            "targetEstimatedCost": round(max_price * int(round_data.get("targetCount") or 0), 2),
        }

    def _event_locked(
        self,
        round_data: dict[str, Any],
        status: str,
        message: str,
        *,
        deduplicate: bool = False,
    ) -> None:
        event = {"at": _iso(self._now()), "status": status, "message": message}
        round_data["lastMessage"] = message
        events = round_data.setdefault("events", [])
        if not (
            deduplicate
            and events
            and events[-1].get("status") == status
            and events[-1].get("message") == message
        ):
            events.append(event)
            round_data["events"] = events[-120:]

    def _validate_round_values(
        self,
        *,
        market_hash_name: str,
        max_price: float,
        budget: float,
        target_count: int,
        interval_seconds: int,
        delivery: int,
    ) -> tuple[str, float, float, int, int, int]:
        market_hash_name = market_hash_name.strip()
        max_price = round(float(max_price), 2)
        budget = round(float(budget), 2)
        target_count = int(target_count)
        # One batch request every 60 seconds. The request itself may contain many products.
        interval_seconds = DEFAULT_INTERVAL_SECONDS
        delivery = int(delivery)
        if not market_hash_name:
            raise ValueError("请选择要扫描的饰品")
        if not _is_weapon_case(market_hash_name):
            raise ValueError("当前 C5 扫货接口只支持武器箱（market_hash_name 必须以 ' Case' 结尾）")
        if max_price <= 0:
            raise ValueError("最高买价必须大于 0")
        if budget <= 0:
            raise ValueError("本轮总预算必须大于 0")
        if target_count <= 0:
            raise ValueError("目标数量必须大于 0")
        if delivery not in {1, 2}:
            raise ValueError("delivery 只能是 1 或 2")
        if budget < 0.01:
            raise ValueError("本轮总预算不能低于 CNY 0.01")
        return market_hash_name, max_price, budget, target_count, interval_seconds, delivery

    def _catalog_display_name(self, market_hash_name: str, fallback: str | None = None) -> str:
        if fallback and fallback.strip():
            return fallback.strip()
        db = Database(self.settings.db_path)
        try:
            row = db.get_item(market_hash_name)
            if row is not None and str(row["name_cn"] or "").strip():
                return str(row["name_cn"]).strip()
        except Exception:
            pass
        finally:
            db.close()
        return market_hash_name

    def _catalog_c5_item_id(self, market_hash_name: str) -> str | None:
        db = Database(self.settings.db_path)
        try:
            row = db.get_item(market_hash_name)
            if row is not None and str(row["c5_item_id"] or "").strip():
                return str(row["c5_item_id"]).strip()
        except Exception:
            pass
        finally:
            db.close()
        return None

    def receiving_accounts(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            cached = self._receiving_accounts_cache
            if cached and not refresh and (self._now() - cached[0]).total_seconds() < 300:
                return json.loads(json.dumps(cached[1], ensure_ascii=False))

        info = self.client.steam_info()
        c5_rows = info.get("steamList") if isinstance(info.get("steamList"), list) else [info]
        c5_lookup = {
            str(row.get("steamId") or "").strip(): row
            for row in c5_rows
            if isinstance(row, dict) and str(row.get("steamId") or "").strip()
        }
        accounts: list[dict[str, Any]] = []
        for account in self.account_store.list_accounts():
            steam_id = str(account.steam_id64 or "").strip()
            trade_steam_id = _steam_id64_from_trade_url(account.trade_url)
            c5_row = c5_lookup.get(steam_id)
            accounts.append({
                "id": account.id,
                "name": account.name,
                "steamId": steam_id,
                "steamIdMasked": f"{steam_id[:7]}***{steam_id[-4:]}" if len(steam_id) >= 11 else steam_id,
                "c5Nickname": (c5_row or {}).get("nickname") or (c5_row or {}).get("username"),
                "c5Bound": c5_row is not None,
                "hasTradeUrl": bool(account.trade_url),
                "tradeUrlMatches": bool(steam_id and trade_steam_id == steam_id),
                "available": bool(c5_row is not None and account.trade_url and trade_steam_id == steam_id),
            })
        accounts.sort(key=lambda row: row["name"].lower())
        with self._lock:
            self._receiving_accounts_cache = (self._now(), accounts)
        return json.loads(json.dumps(accounts, ensure_ascii=False))

    def _resolve_receiving_account(self, account_id: str | None, *, verify_c5: bool) -> Any:
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ValueError("请选择接收武器箱的 Steam 账号")
        account = self.account_store.get_account(account_id)
        if account is None:
            raise ValueError("接收 Steam 账号不存在或已从本地配置删除")
        steam_id = str(account.steam_id64 or "").strip()
        if not steam_id:
            raise ValueError(f"接收账号 {account.name} 缺少 steam_id64")
        if not account.trade_url:
            raise ValueError(f"接收账号 {account.name} 缺少 trade_url")
        trade_steam_id = _steam_id64_from_trade_url(account.trade_url)
        if trade_steam_id != steam_id:
            raise ValueError(
                f"接收账号 {account.name} 的 trade_url 与 SteamID 不匹配，禁止购买"
            )
        if verify_c5:
            remote = next(
                (row for row in self.receiving_accounts(refresh=True) if row["id"] == account.id),
                None,
            )
            if remote is None or not remote.get("c5Bound"):
                raise ValueError(f"接收账号 {account.name} 当前没有绑定在 C5，禁止购买")
            if not remote.get("available"):
                raise ValueError(f"接收账号 {account.name} 当前接收配置不完整，禁止购买")
        return account

    def search_items(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.search_items_page(query, limit=limit, offset=0)["items"]

    def search_items_page(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        query = query.strip()
        limit = max(1, min(50, int(limit)))
        offset = max(0, int(offset))
        db = Database(self.settings.db_path)
        try:
            rows = db.search_items(query, limit=None)
        finally:
            db.close()
        result = [
            {
                "marketHashName": str(row["market_hash_name"]),
                "displayName": str(row["name_cn"] or row["market_hash_name"]),
                "c5ItemId": row["c5_item_id"],
            }
            for row in rows
            if _is_weapon_case(str(row["market_hash_name"]), str(row["name_cn"] or ""))
        ]
        if query:
            lowered = query.lower()
            result.sort(key=lambda row: (
                0 if row["marketHashName"].lower() == lowered or row["displayName"].lower() == lowered else
                1 if row["marketHashName"].lower().startswith(lowered) or row["displayName"].lower().startswith(lowered) else
                2,
                row["displayName"],
            ))
        if (
            query
            and _is_weapon_case(query)
            and not any(row["marketHashName"].lower() == query.lower() for row in result)
        ):
            custom = {
                "marketHashName": query,
                "displayName": query,
                "c5ItemId": None,
                "custom": True,
            }
            result = [custom, *result]
        total = len(result)
        page = result[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "items": page,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "hasMore": next_offset < total,
                "nextOffset": next_offset if next_offset < total else None,
            },
        }

    def create_round(
        self,
        *,
        market_hash_name: str,
        display_name: str | None,
        receiving_account_id: str,
        max_price: float,
        budget: float,
        target_count: int,
        interval_seconds: int = 60,
        delivery: int = 2,
    ) -> dict[str, Any]:
        values = self._validate_round_values(
            market_hash_name=market_hash_name,
            max_price=max_price,
            budget=budget,
            target_count=target_count,
            interval_seconds=interval_seconds,
            delivery=delivery,
        )
        market_hash_name, max_price, budget, target_count, interval_seconds, delivery = values
        receiving_account = self._resolve_receiving_account(receiving_account_id, verify_c5=False)
        with self._lock:
            current = self._round_locked(required=False)
            if current is not None and current.get("status") not in TERMINAL_STATUSES:
                raise ValueError("当前轮次尚未结束；请继续当前轮次，或先停止后再新建下一轮")
            now = _iso(self._now())
            round_number = int(self._state["nextRoundNumber"])
            round_data = {
                "id": uuid.uuid4().hex,
                "roundNumber": round_number,
                "marketHashName": market_hash_name,
                "displayName": self._catalog_display_name(market_hash_name, display_name),
                "maxPrice": max_price,
                "budget": budget,
                "targetCount": target_count,
                "intervalSeconds": interval_seconds,
                "delivery": delivery,
                "receivingAccountId": receiving_account.id,
                "receivingAccountName": receiving_account.name,
                "receivingSteamId": receiving_account.steam_id64,
                "status": "draft",
                "stopReason": None,
                "createdAt": now,
                "startedAt": None,
                "updatedAt": now,
                "completedAt": None,
                "nextRunAt": None,
                "lastRunAt": None,
                "lastPrice": None,
                "lastMessage": "轮次草稿已保存；输入确认词后才会开始真实购买。",
                "attemptCount": 0,
                "orders": [],
                "events": [],
            }
            self._event_locked(round_data, "created", f"第 {round_number} 轮已创建：{round_data['displayName']}，预算 CNY {budget:.2f}。")
            self._state["rounds"].append(round_data)
            self._state["currentRoundId"] = round_data["id"]
            self._state["nextRoundNumber"] = round_number + 1
            recent = {
                "marketHashName": market_hash_name,
                "displayName": round_data["displayName"],
            }
            old_recent = [
                row for row in self._state.get("recentItems", [])
                if str(row.get("marketHashName")) != market_hash_name
            ]
            self._state["recentItems"] = [recent, *old_recent][:10]
            self._save_locked()
        return self.dashboard(round_data["id"])

    def update_round(
        self,
        round_id: str,
        *,
        market_hash_name: str,
        display_name: str | None,
        receiving_account_id: str,
        max_price: float,
        budget: float,
        target_count: int,
        interval_seconds: int = 60,
        delivery: int = 2,
    ) -> dict[str, Any]:
        values = self._validate_round_values(
            market_hash_name=market_hash_name,
            max_price=max_price,
            budget=budget,
            target_count=target_count,
            interval_seconds=interval_seconds,
            delivery=delivery,
        )
        market_hash_name, max_price, budget, target_count, interval_seconds, delivery = values
        receiving_account = self._resolve_receiving_account(receiving_account_id, verify_c5=False)
        with self._lock:
            round_data = self._round_locked(round_id)
            if round_data.get("status") not in EDITABLE_STATUSES:
                raise ValueError("只有草稿或暂停中的轮次可以修改")
            counts = self._counts(round_data)
            money = self._money(round_data)
            if round_data.get("orders") and market_hash_name != round_data.get("marketHashName"):
                raise ValueError("已有购买记录的轮次不能更换饰品，请停止后新建下一轮")
            if round_data.get("orders") and receiving_account.id != round_data.get("receivingAccountId"):
                raise ValueError("已有购买记录的轮次不能更换接收账号，请停止后新建下一轮")
            if budget + 0.005 < float(money["committedAmount"]):
                raise ValueError("新预算不能低于当前已占用金额")
            if target_count < counts["delivered"] + counts["pending"]:
                raise ValueError("新目标数量不能低于成功交付与待交付数量之和")
            round_data.update({
                "marketHashName": market_hash_name,
                "displayName": self._catalog_display_name(market_hash_name, display_name),
                "maxPrice": max_price,
                "budget": budget,
                "targetCount": target_count,
                "intervalSeconds": interval_seconds,
                "delivery": delivery,
                "receivingAccountId": receiving_account.id,
                "receivingAccountName": receiving_account.name,
                "receivingSteamId": receiving_account.steam_id64,
                "updatedAt": _iso(self._now()),
            })
            self._event_locked(round_data, "configured", f"第 {round_data['roundNumber']} 轮参数已保存，独立预算 CNY {budget:.2f}。")
            self._state["currentRoundId"] = round_data["id"]
            self._save_locked()
        return self.dashboard(round_id)

    def start(self, confirmation: str, *, round_id: str | None = None) -> dict[str, Any]:
        if confirmation.strip() != "开始扫货":
            raise ValueError("请输入“开始扫货”确认真实购买")
        if not self.settings.c5_api_key:
            raise ValueError("未配置 C5GAME_API_KEY，不能开启真实扫货")
        with self._lock:
            round_data = self._round_locked(round_id)
            if any(row.get("status") == "running" for row in self._state["rounds"]):
                raise ValueError("已有扫货轮次正在运行，不能重复启动或同时启动另一轮")
            if round_data.get("status") not in EDITABLE_STATUSES:
                raise ValueError("只有草稿或暂停中的轮次可以开始")
            counts = self._counts(round_data)
            money = self._money(round_data)
            if any(
                submission.get("status") in {"submitting", "uncertain"}
                for submission in round_data.get("submissions", [])
            ):
                raise ValueError("当前轮次仍有购买结果不确定的批次，必须先完成 C5 远端对账，禁止继续购买")
            if counts["delivered"] >= int(round_data["targetCount"]):
                raise ValueError("当前轮次已经达到目标数量")
            if float(money["remainingBudget"]) <= 0:
                raise ValueError("当前轮次预算已经用完")
            receiving_account_id = str(round_data.get("receivingAccountId") or "")
        # C5 binding is external state, so verify it immediately before enabling execution.
        self._resolve_receiving_account(receiving_account_id, verify_c5=True)
        with self._lock:
            round_data = self._round_locked(round_id)
            now = _iso(self._now())
            round_data["status"] = "running"
            round_data["startedAt"] = round_data.get("startedAt") or now
            round_data["nextRunAt"] = now
            round_data["updatedAt"] = now
            round_data["stopReason"] = None
            self._state["currentRoundId"] = round_data["id"]
            self._event_locked(
                round_data,
                "started",
                "真实扫货已开启：立即提交一次批量购买，之后每 60 秒提交下一批；每批按剩余预算、目标数量和最高单价尽可能购买。",
            )
            self._save_locked()
        self._wake.set()
        return self.dashboard(round_data["id"])

    def pause(self, *, round_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            round_data = self._round_locked(round_id)
            if round_data.get("status") != "running":
                raise ValueError("当前轮次没有在运行")
            round_data["status"] = "paused"
            round_data["nextRunAt"] = None
            round_data["updatedAt"] = _iso(self._now())
            self._event_locked(round_data, "paused", "本轮已暂停，不会再发起购买；待交付订单仍会继续审计。")
            self._save_locked()
        self._wake.set()
        return self.dashboard(round_data["id"])

    def stop(self, *, round_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            round_data = self._round_locked(round_id)
            if round_data.get("status") in TERMINAL_STATUSES:
                return self.dashboard(round_data["id"])
            round_data["status"] = "stopped"
            round_data["stopReason"] = "manual"
            round_data["nextRunAt"] = None
            round_data["completedAt"] = _iso(self._now())
            round_data["updatedAt"] = _iso(self._now())
            self._event_locked(round_data, "stopped", "本轮已手动停止；轮次、订单与预算账本均已归档。")
            self._save_locked()
        self._wake.set()
        return self.dashboard(round_data["id"])

    def _complete_locked(self, round_data: dict[str, Any], *, reason: str, message: str) -> None:
        round_data["status"] = "completed"
        round_data["stopReason"] = reason
        round_data["nextRunAt"] = None
        round_data["completedAt"] = _iso(self._now())
        round_data["updatedAt"] = _iso(self._now())
        self._event_locked(round_data, "completed", message)

    def _read_live_price(self, market_hash_name: str) -> float:
        payload = self.client.price_batch([market_hash_name], app_id=self.settings.app_id)
        row = payload.get(market_hash_name) if isinstance(payload, dict) else None
        price = _safe_float(row.get("price")) if isinstance(row, dict) else _safe_float(row)
        if price is None or price <= 0:
            raise RuntimeError("C5 批量价格接口没有返回有效价格")
        return round(price, 2)

    def _reconcile_uncertain_submissions(self) -> None:
        with self._lock:
            uncertain = [
                (round_data["id"], dict(submission))
                for round_data in self._state["rounds"]
                for submission in round_data.get("submissions", [])
                if submission.get("status") in {"submitting", "uncertain"}
            ]
        if not uncertain:
            return

        remote_rows: list[dict[str, Any]] = []
        for page_num in range(1, 6):
            try:
                payload = self.client.buyer_order_status(
                    page_num=page_num,
                    page_size=100,
                    status=None,
                )
            except Exception as exc:
                with self._lock:
                    for round_id, snapshot in uncertain:
                        round_data = self._round_locked(round_id, required=False)
                        if round_data is None:
                            continue
                        for submission in round_data.get("submissions", []):
                            if submission.get("id") == snapshot.get("id"):
                                submission["lastCheckError"] = str(exc)
                                submission["lastCheckedAt"] = _iso(self._now())
                    self._save_locked()
                return
            rows = payload.get("list") if isinstance(payload, dict) else None
            if isinstance(rows, list):
                remote_rows.extend(row for row in rows if isinstance(row, dict))
            pages = _safe_int(payload.get("pages")) if isinstance(payload, dict) else None
            if not rows or (pages is not None and page_num >= pages):
                break

        remote_by_product = {
            str(row.get("productId") or ""): row
            for row in remote_rows
            if str(row.get("productId") or "")
        }
        with self._lock:
            for round_id, snapshot in uncertain:
                round_data = self._round_locked(round_id, required=False)
                if round_data is None:
                    continue
                submission = next(
                    (
                        row
                        for row in round_data.get("submissions", [])
                        if row.get("id") == snapshot.get("id")
                    ),
                    None,
                )
                if submission is None:
                    continue
                existing_by_product = {
                    str(order.get("productId") or ""): order
                    for order in round_data.get("orders", [])
                    if str(order.get("productId") or "")
                }
                matched_now = 0
                unresolved: list[str] = []
                for requested in submission.get("products", []):
                    product_id = str(requested.get("productId") or "")
                    remote = remote_by_product.get(product_id)
                    if remote is None:
                        unresolved.append(product_id)
                        continue
                    remote_steam_id = str(remote.get("receiveSteamId") or "")
                    expected_steam_id = str(submission.get("receivingSteamId") or "")
                    if expected_steam_id and remote_steam_id and remote_steam_id != expected_steam_id:
                        unresolved.append(product_id)
                        continue
                    status = _delivery_status(remote)
                    order_id = str(remote.get("orderId") or "")
                    accepted_at = _iso(self._now())
                    create_time = _safe_int(remote.get("createTime"))
                    if create_time:
                        accepted_at = _iso(datetime.fromtimestamp(create_time, timezone.utc))
                    order = existing_by_product.get(product_id)
                    if order is None:
                        order = {
                            "id": uuid.uuid4().hex,
                            "roundId": round_id,
                            "acceptedAt": accepted_at,
                            "marketHashName": round_data["marketHashName"],
                            "outTradeNo": str(requested.get("outTradeNo") or ""),
                            "productId": product_id,
                            "receivingAccountId": submission.get("receivingAccountId"),
                            "receivingAccountName": submission.get("receivingAccountName"),
                            "receivingSteamId": submission.get("receivingSteamId"),
                            "livePrice": submission.get("livePrice"),
                            "maxPrice": submission.get("maxPrice"),
                        }
                        round_data.setdefault("orders", []).append(order)
                        existing_by_product[product_id] = order
                        matched_now += 1
                    order.update({
                        "status": status,
                        "orderAssetId": order_id,
                        "tradeOrderId": order_id,
                        "actualPay": round(
                            _safe_float(remote.get("price"))
                            or _safe_float(requested.get("buyPrice"))
                            or 0,
                            2,
                        ),
                        "detailStatus": remote.get("status"),
                        "detailStatusName": remote.get("statusName"),
                        "lastCheckedAt": _iso(self._now()),
                    })
                    if status != "pending":
                        order["finishedAt"] = _iso(self._now())
                submission["lastCheckedAt"] = _iso(self._now())
                submission["matchedCount"] = len(submission.get("products", [])) - len(unresolved)
                submission["unresolvedProductIds"] = unresolved
                submission.pop("lastCheckError", None)
                if not unresolved:
                    submission["status"] = "reconciled"
                    submission["reconciledAt"] = _iso(self._now())
                else:
                    submission["status"] = "uncertain"
                if matched_now:
                    self._event_locked(
                        round_data,
                        "buy_reconciled",
                        f"C5 超时购买对账补回 {matched_now} 件；本轮继续保持暂停，需人工确认后再继续。",
                    )
            self._save_locked()

    def _audit_all_pending(self) -> None:
        self._reconcile_uncertain_submissions()
        with self._lock:
            pending = [
                (round_data["id"], dict(order))
                for round_data in self._state["rounds"]
                for order in round_data.get("orders", [])
                if order.get("status") == "pending"
            ]
        changed = False
        for round_id, snapshot in pending:
            order_id = str(snapshot.get("orderAssetId") or "")
            if not order_id:
                continue
            try:
                detail = self.client.buyer_order_detail(order_id)
            except Exception as exc:
                with self._lock:
                    round_data = self._round_locked(round_id, required=False)
                    if round_data is None:
                        continue
                    for order in round_data.get("orders", []):
                        if order.get("id") == snapshot.get("id"):
                            order["lastCheckedAt"] = _iso(self._now())
                            order["lastCheckError"] = str(exc)
                            changed = True
                continue
            final = _delivery_status(detail)
            with self._lock:
                round_data = self._round_locked(round_id, required=False)
                if round_data is None:
                    continue
                for order in round_data.get("orders", []):
                    if order.get("id") != snapshot.get("id"):
                        continue
                    previous = order.get("status")
                    order["status"] = final
                    order["lastCheckedAt"] = _iso(self._now())
                    order["detailStatus"] = detail.get("status")
                    order["detailStatusName"] = detail.get("statusName")
                    order["failedCode"] = detail.get("failedCode")
                    order["failedDesc"] = detail.get("failedDesc")
                    order.pop("lastCheckError", None)
                    if final != "pending":
                        order["finishedAt"] = _iso(self._now())
                    if final != previous and final == "delivered":
                        self._event_locked(round_data, "delivered", f"C5 已确认 1 件交付成功：{round_data['displayName']}。")
                    elif final != previous and final == "failed":
                        reason = order.get("failedDesc") or order.get("failedCode") or "未知原因"
                        self._event_locked(round_data, "failed", f"C5 最终交付失败，预算已释放：{reason}")
                    changed = True
        if changed:
            with self._lock:
                self._save_locked()

    def run_cycle(self, *, allow_buy: bool = True, round_id: str | None = None) -> dict[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            return self.dashboard(round_id)
        try:
            self._audit_all_pending()
            with self._lock:
                round_data = self._round_locked(round_id, required=False)
                if round_data is None:
                    return self.dashboard()
                running = round_data.get("status") == "running"
                round_data["lastRunAt"] = _iso(self._now())
                market_hash_name = str(round_data["marketHashName"])
                max_price = float(round_data["maxPrice"])
                target_count = int(round_data["targetCount"])
                delivery = int(round_data["delivery"])
                receiving_account_id = str(round_data.get("receivingAccountId") or "")
            try:
                live_price = self._read_live_price(market_hash_name)
            except Exception as exc:
                with self._lock:
                    round_data = self._round_locked(round_data["id"])
                    self._event_locked(round_data, "price_error", f"读取 C5 实时价格失败：{exc}", deduplicate=True)
                    self._save_locked()
                return self.dashboard(round_data["id"])

            with self._lock:
                round_data = self._round_locked(round_data["id"])
                round_data["lastPrice"] = live_price
                counts = self._counts(round_data)
                money = self._money(round_data)
                active_count = counts["delivered"] + counts["pending"]
                remaining_budget = float(money["remainingBudget"])
                if counts["delivered"] >= target_count:
                    self._complete_locked(round_data, reason="target_reached", message="目标交付数量已经达到，本轮自动完成并归档。")
                    self._save_locked()
                    return self.dashboard(round_data["id"])
                if active_count >= target_count:
                    self._event_locked(round_data, "waiting_delivery", "成功交付与待交付数量已达到目标，本轮不再购买，继续等待最终交付。", deduplicate=True)
                    self._save_locked()
                    return self.dashboard(round_data["id"])
                if remaining_budget <= 0.005:
                    if counts["pending"]:
                        self._event_locked(round_data, "waiting_budget_delivery", "本轮预算已全部占用，等待待交付订单最终结果。", deduplicate=True)
                    else:
                        self._complete_locked(round_data, reason="budget_reached", message="本轮独立预算已经用完，本轮自动完成并归档。")
                    self._save_locked()
                    return self.dashboard(round_data["id"])
                if not running or not allow_buy:
                    self._event_locked(round_data, "refreshed", f"已刷新 {round_data['displayName']} 的 C5 价格：CNY {live_price:.2f}；没有发起购买。", deduplicate=True)
                    self._save_locked()
                    return self.dashboard(round_data["id"])
                if live_price > max_price:
                    self._event_locked(round_data, "price_too_high", f"当前 C5 价 CNY {live_price:.2f} 高于最高价 CNY {max_price:.2f}，等待下一轮。", deduplicate=True)
                    self._save_locked()
                    return self.dashboard(round_data["id"])
                if live_price > remaining_budget + 0.005:
                    if counts["pending"]:
                        self._event_locked(round_data, "budget_waiting_delivery", f"剩余预算 CNY {remaining_budget:.2f} 暂不足购买，先等待待交付订单结果。", deduplicate=True)
                    else:
                        self._complete_locked(
                            round_data,
                            reason="budget_limit",
                            message=f"剩余预算 CNY {remaining_budget:.2f} 不足购买当前最低价 CNY {live_price:.2f}，本轮按预算上限完成。",
                        )
                    self._save_locked()
                    return self.dashboard(round_data["id"])

            receiving_account = self._resolve_receiving_account(receiving_account_id, verify_c5=False)
            remaining_count = max(0, target_count - active_count)
            c5_item_id = self._catalog_c5_item_id(market_hash_name)
            if not c5_item_id:
                with self._lock:
                    round_data = self._round_locked(round_data["id"])
                    self._event_locked(
                        round_data,
                        "missing_c5_item_id",
                        f"{round_data['displayName']} 缺少 C5 itemId，无法分页读取具体在售；本轮没有发起购买。",
                        deduplicate=True,
                    )
                    self._save_locked()
                return self.dashboard(round_data["id"])

            market_rows: list[dict[str, Any]] = []
            page_size = min(C5_MARKET_SEARCH_MAX_PAGE_SIZE, max(1, remaining_count))
            max_pages = min(
                C5_MARKET_LIST_MAX_PAGES_PER_CYCLE,
                max(1, math.ceil(remaining_count / page_size)),
            )
            for page_num in range(1, max_pages + 1):
                try:
                    market_payload = self.client.market_products_list(
                        item_id=c5_item_id,
                        delivery=delivery,
                        page_num=page_num,
                        page_size=page_size,
                    )
                except Exception as exc:
                    with self._lock:
                        round_data = self._round_locked(round_data["id"])
                        self._event_locked(
                            round_data,
                            "market_search_failed",
                            f"读取 C5 可批量购买在售第 {page_num} 页失败：{exc}",
                            deduplicate=True,
                        )
                        self._save_locked()
                    return self.dashboard(round_data["id"])
                page_rows = market_payload.get("list") if isinstance(market_payload, dict) else None
                if isinstance(page_rows, list):
                    market_rows.extend(row for row in page_rows if isinstance(row, dict))
                if not isinstance(market_payload, dict) or not market_payload.get("hasMore"):
                    break
                if len(market_rows) >= remaining_count:
                    break

            candidates: list[tuple[float, str]] = []
            seen_product_ids: set[str] = set()
            for row in market_rows:
                product_id = str(row.get("productId") or "").strip()
                price = _safe_float(row.get("price"))
                if not product_id or product_id in seen_product_ids or price is None or price <= 0:
                    continue
                if price > max_price + 0.005:
                    continue
                seen_product_ids.add(product_id)
                candidates.append((round(price, 2), product_id))
            candidates.sort(key=lambda row: (row[0], row[1]))

            selected: list[dict[str, Any]] = []
            selected_total = 0.0
            for price, product_id in candidates:
                if len(selected) >= remaining_count:
                    break
                if selected_total + price > remaining_budget + 0.005:
                    continue
                selected.append({
                    "productId": product_id,
                    "buyPrice": price,
                    "outTradeNo": uuid.uuid4().hex,
                })
                selected_total = round(selected_total + price, 2)

            if not selected:
                with self._lock:
                    round_data = self._round_locked(round_data["id"])
                    self._event_locked(
                        round_data,
                        "no_batch_candidates",
                        f"当前没有单价不高于 CNY {max_price:.2f} 且符合剩余预算的在售，等待下一次批量扫描。",
                        deduplicate=True,
                    )
                    self._save_locked()
                return self.dashboard(round_data["id"])

            with self._lock:
                round_data = self._round_locked(round_data["id"])
                round_data["attemptCount"] = int(round_data.get("attemptCount") or 0) + 1
                submission = {
                    "id": uuid.uuid4().hex,
                    "submittedAt": _iso(self._now()),
                    "status": "submitting",
                    "marketHashName": market_hash_name,
                    "livePrice": live_price,
                    "maxPrice": max_price,
                    "receivingAccountId": receiving_account.id,
                    "receivingAccountName": receiving_account.name,
                    "receivingSteamId": receiving_account.steam_id64,
                    "products": json.loads(json.dumps(selected)),
                }
                round_data.setdefault("submissions", []).append(submission)
                round_data["submissions"] = round_data["submissions"][-200:]
                round_data["updatedAt"] = _iso(self._now())
                self._save_locked()
            try:
                payload = self.client.batch_buy(
                    product_list=selected,
                    trade_url=receiving_account.trade_url,
                )
            except Exception as exc:
                with self._lock:
                    round_data = self._round_locked(round_data["id"])
                    submission["status"] = "uncertain"
                    submission["error"] = str(exc)
                    submission["lastCheckedAt"] = _iso(self._now())
                    round_data["status"] = "paused"
                    round_data["nextRunAt"] = None
                    round_data["stopReason"] = "buy_uncertain"
                    self._event_locked(
                        round_data,
                        "buy_uncertain",
                        f"C5 批量购买请求结果不确定：{exc}；本轮已自动暂停，将按 productId 查询远端订单，禁止继续购买。",
                    )
                    self._save_locked()
                return self.dashboard(round_data["id"])

            success_rows = payload.get("successList") if isinstance(payload, dict) else None
            failed_rows = payload.get("failedList") if isinstance(payload, dict) else None
            success_rows = success_rows if isinstance(success_rows, list) else []
            failed_rows = failed_rows if isinstance(failed_rows, list) else []
            if not success_rows and not failed_rows:
                with self._lock:
                    round_data = self._round_locked(round_data["id"])
                    round_data["status"] = "paused"
                    round_data["nextRunAt"] = None
                    round_data["stopReason"] = "buy_uncertain"
                    submission["status"] = "uncertain"
                    self._event_locked(round_data, "buy_uncertain", "C5 批量购买响应没有 successList/failedList，本轮已自动暂停，不会继续购买；请人工核对。")
                    self._save_locked()
                return self.dashboard(round_data["id"])

            requested_by_trade_no = {str(row["outTradeNo"]): row for row in selected}
            requested_by_product_id = {str(row["productId"]): row for row in selected}
            now_iso = _iso(self._now())
            new_orders: list[dict[str, Any]] = []
            uncertain_count = 0
            success_amount = 0.0
            for row in success_rows:
                if not isinstance(row, dict):
                    continue
                request_row = (
                    requested_by_trade_no.get(str(row.get("outTradeNo") or ""))
                    or requested_by_product_id.get(str(row.get("productId") or ""))
                    or {}
                )
                actual_pay = round(
                    _safe_float(row.get("actualPay"))
                    or _safe_float(request_row.get("buyPrice"))
                    or live_price,
                    2,
                )
                order_asset_id = _extract_order_asset_id(row)
                if not order_asset_id:
                    uncertain_count += 1
                success_amount = round(success_amount + actual_pay, 2)
                new_orders.append({
                    "id": uuid.uuid4().hex,
                    "roundId": round_data["id"],
                    "acceptedAt": now_iso,
                    "status": "pending",
                    "marketHashName": market_hash_name,
                    "outTradeNo": str(row.get("outTradeNo") or request_row.get("outTradeNo") or ""),
                    "productId": str(row.get("productId") or request_row.get("productId") or ""),
                    "orderAssetId": order_asset_id or "",
                    "tradeOrderId": _extract_trade_order_id(row),
                    "actualPay": actual_pay,
                    "livePrice": live_price,
                    "maxPrice": max_price,
                    "receivingAccountId": receiving_account.id,
                    "receivingAccountName": receiving_account.name,
                    "receivingSteamId": receiving_account.steam_id64,
                })

            for row in failed_rows:
                if not isinstance(row, dict):
                    continue
                request_row = (
                    requested_by_trade_no.get(str(row.get("outTradeNo") or ""))
                    or requested_by_product_id.get(str(row.get("productId") or ""))
                    or {}
                )
                amount = round(
                    _safe_float(row.get("amount"))
                    or _safe_float(request_row.get("buyPrice"))
                    or 0,
                    2,
                )
                new_orders.append({
                    "id": uuid.uuid4().hex,
                    "roundId": round_data["id"],
                    "acceptedAt": now_iso,
                    "finishedAt": now_iso,
                    "status": "failed",
                    "marketHashName": market_hash_name,
                    "outTradeNo": str(row.get("outTradeNo") or request_row.get("outTradeNo") or ""),
                    "productId": str(row.get("productId") or request_row.get("productId") or ""),
                    "orderAssetId": "",
                    "tradeOrderId": None,
                    "actualPay": amount,
                    "livePrice": live_price,
                    "maxPrice": max_price,
                    "failedCode": row.get("errorCode") or row.get("failedCode") or "BATCH_BUY_FAILED",
                    "failedDesc": row.get("errorMsg") or row.get("failedDesc") or "C5 批量购买未成功",
                    "receivingAccountId": receiving_account.id,
                    "receivingAccountName": receiving_account.name,
                    "receivingSteamId": receiving_account.steam_id64,
                })

            with self._lock:
                round_data = self._round_locked(round_data["id"])
                round_data.setdefault("orders", []).extend(new_orders)
                if uncertain_count:
                    submission["status"] = "uncertain"
                    round_data["status"] = "paused"
                    round_data["nextRunAt"] = None
                    round_data["stopReason"] = "buy_uncertain"
                    self._event_locked(
                        round_data,
                        "buy_uncertain",
                        f"C5 批量购买返回 {uncertain_count} 件成功项但缺少 orderAssetId；相关金额已占用，本轮自动暂停，请人工核对。",
                    )
                else:
                    submission["status"] = "resolved"
                    submission["resolvedAt"] = _iso(self._now())
                    self._event_locked(
                        round_data,
                        "accepted",
                        f"C5 本次批量购买成功 {len(success_rows)} 件、失败 {len(failed_rows)} 件，成功金额 CNY {success_amount:.2f}；60 秒后再提交下一批。",
                    )
                self._save_locked()
            return self.dashboard(round_data["id"])
        finally:
            self._cycle_lock.release()

    def _round_summary_locked(self, round_data: dict[str, Any]) -> dict[str, Any]:
        counts = self._counts(round_data)
        money = self._money(round_data)
        return {
            "id": round_data["id"],
            "roundNumber": round_data["roundNumber"],
            "marketHashName": round_data["marketHashName"],
            "displayName": round_data["displayName"],
            "status": round_data["status"],
            "stopReason": round_data.get("stopReason"),
            "budget": money["budget"],
            "committedAmount": money["committedAmount"],
            "settledAmount": money["settledAmount"],
            "targetCount": round_data["targetCount"],
            "deliveredCount": counts["delivered"],
            "pendingCount": counts["pending"],
            "failedCount": counts["failed"],
            "averageAcceptedPrice": money["averageAcceptedPrice"],
            "receivingAccountId": round_data.get("receivingAccountId"),
            "receivingAccountName": round_data.get("receivingAccountName"),
            "receivingSteamId": round_data.get("receivingSteamId"),
            "createdAt": round_data["createdAt"],
            "startedAt": round_data.get("startedAt"),
            "completedAt": round_data.get("completedAt"),
        }

    def dashboard(self, round_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            round_data = self._round_locked(round_id, required=False)
            all_summaries = [self._round_summary_locked(row) for row in reversed(self._state["rounds"])]
            recent_items = json.loads(json.dumps(self._state.get("recentItems", []), ensure_ascii=False))
            real_running = any(row.get("status") == "running" for row in self._state["rounds"])
            if round_data is None:
                return {
                    "apiOnline": True,
                    "realExecutionRunning": real_running,
                    "round": None,
                    "counts": {"accepted": 0, "delivered": 0, "pending": 0, "failed": 0},
                    "money": {
                        "budget": 0.0,
                        "acceptedAmount": 0.0,
                        "committedAmount": 0.0,
                        "settledAmount": 0.0,
                        "failedAmount": 0.0,
                        "remainingBudget": 0.0,
                        "averageAcceptedPrice": 0.0,
                        "averageDeliveredPrice": 0.0,
                        "maxAffordableCount": 0,
                        "targetEstimatedCost": 0.0,
                    },
                    "orders": [],
                    "events": [],
                    "rounds": all_summaries,
                    "recentItems": recent_items,
                }
            snapshot = json.loads(json.dumps(round_data, ensure_ascii=False))
            counts = self._counts(round_data)
            money = self._money(round_data)
        orders = snapshot.pop("orders", [])
        events = snapshot.pop("events", [])
        return {
            "apiOnline": True,
            "realExecutionRunning": real_running,
            "round": snapshot,
            "counts": counts,
            "money": money,
            "orders": list(reversed(orders[-100:])),
            "events": list(reversed(events[-40:])),
            "rounds": all_summaries,
            "recentItems": recent_items,
        }

    def _worker(self) -> None:
        while not self._shutdown.is_set():
            with self._lock:
                running_round = next((row for row in self._state["rounds"] if row.get("status") == "running"), None)
                has_pending = any(
                    order.get("status") == "pending"
                    for row in self._state["rounds"]
                    for order in row.get("orders", [])
                ) or any(
                    submission.get("status") in {"submitting", "uncertain"}
                    for row in self._state["rounds"]
                    for submission in row.get("submissions", [])
                )
                next_run = _parse_iso(running_round.get("nextRunAt")) if running_round else None
            now = self._now()
            if running_round is not None:
                if next_run is None or next_run <= now:
                    self.run_cycle(allow_buy=True, round_id=running_round["id"])
                    with self._lock:
                        current = self._round_locked(running_round["id"], required=False)
                        if current is not None and current.get("status") == "running":
                            current["nextRunAt"] = _iso(
                                self._now() + timedelta(seconds=DEFAULT_INTERVAL_SECONDS)
                            )
                            current["updatedAt"] = _iso(self._now())
                            self._save_locked()
                    continue
                timeout = min(5.0, max(0.1, (next_run - now).total_seconds()))
                self._wake.wait(timeout)
                self._wake.clear()
                continue
            if has_pending and now >= self._next_idle_audit_at:
                if self._cycle_lock.acquire(blocking=False):
                    try:
                        self._audit_all_pending()
                    finally:
                        self._cycle_lock.release()
                self._next_idle_audit_at = self._now() + timedelta(seconds=DEFAULT_INTERVAL_SECONDS)
                continue
            self._wake.wait(5.0)
            self._wake.clear()

    def close(self) -> None:
        self._shutdown.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
