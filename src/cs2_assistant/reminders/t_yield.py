from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from cs2_assistant.clients import ServerChanClient
from cs2_assistant.config import Settings, load_settings
from cs2_assistant.models import NotificationMessage
from cs2_assistant.services.notifications import NotificationService
from cs2_assistant.services.t_yield_scan import (
    INVENTORY_FILTER_ALL,
    MissingSteamPriceIssue,
    TYieldCandidate,
    TYieldScanReport,
    load_missing_steam_report,
    normalize_inventory_filter,
    scan_t_yield,
)
from cs2_assistant.utils import ensure_parent_dir, utc_now_iso

INVENTORY_SCOPE_ALL = "all"
INVENTORY_SCOPE_ALL_COOLDOWN = "all_cooldown"
INVENTORY_SCOPE_NOT_ALL_COOLDOWN = "not_all_cooldown"

INVENTORY_SCOPE_LABELS: dict[str, str] = {
    INVENTORY_SCOPE_ALL: "全部",
    INVENTORY_SCOPE_ALL_COOLDOWN: "全冷却",
    INVENTORY_SCOPE_NOT_ALL_COOLDOWN: "存在不冷却",
}

INVENTORY_SCOPE_CHOICES: dict[str, str] = {
    "1": INVENTORY_SCOPE_NOT_ALL_COOLDOWN,
    "2": INVENTORY_SCOPE_ALL_COOLDOWN,
    "3": INVENTORY_SCOPE_ALL,
}


def inventory_scope_label(value: str) -> str:
    return INVENTORY_SCOPE_LABELS.get(value, value)


def normalize_inventory_scope(value: str | None) -> str:
    raw = str(value or INVENTORY_SCOPE_NOT_ALL_COOLDOWN).strip().lower()
    aliases = {
        "all": INVENTORY_SCOPE_ALL,
        "all_cooldown": INVENTORY_SCOPE_ALL_COOLDOWN,
        "cooldown_only": INVENTORY_SCOPE_ALL_COOLDOWN,
        "full_cooldown": INVENTORY_SCOPE_ALL_COOLDOWN,
        "not_all_cooldown": INVENTORY_SCOPE_NOT_ALL_COOLDOWN,
        "non_all_cooldown": INVENTORY_SCOPE_NOT_ALL_COOLDOWN,
        "not_full_cooldown": INVENTORY_SCOPE_NOT_ALL_COOLDOWN,
        "tradable_only": INVENTORY_SCOPE_NOT_ALL_COOLDOWN,
        "mixed_only": INVENTORY_SCOPE_NOT_ALL_COOLDOWN,
    }
    normalized = aliases.get(raw, raw)
    if normalized not in INVENTORY_SCOPE_LABELS:
        supported = ", ".join(sorted(INVENTORY_SCOPE_LABELS))
        raise ValueError(f"inventory_scope 必须是以下值之一: {supported}")
    return normalized


@dataclass(slots=True)
class TYieldReminderConfig:
    top_n: int = 10
    min_price: float = 10.0
    steam_discount: float = 0.73
    hot_threshold_pct: float = 10.0
    poll_interval_minutes: int = 30
    daily_summary_time: str = "15:30"
    inventory_scope: str = INVENTORY_SCOPE_NOT_ALL_COOLDOWN
    allow_cached_fallback: bool = True
    cache_max_age_minutes: int = 180

    def validate(self) -> None:
        if self.top_n <= 0:
            raise ValueError("top_n must be positive")
        if self.min_price < 0:
            raise ValueError("min_price must be non-negative")
        if self.hot_threshold_pct <= 0:
            raise ValueError("hot_threshold_pct must be positive")
        if self.poll_interval_minutes <= 0:
            raise ValueError("poll_interval_minutes must be positive")
        if self.cache_max_age_minutes <= 0:
            raise ValueError("cache_max_age_minutes must be positive")
        _parse_summary_time(self.daily_summary_time)
        self.inventory_scope = normalize_inventory_scope(self.inventory_scope)


@dataclass(slots=True)
class TYieldReminderState:
    last_hot_signature: str | None = None
    last_hot_sent_at: str | None = None
    last_daily_summary_date: str | None = None


@dataclass(slots=True)
class TYieldReminderDecision:
    reason: str
    should_notify: bool
    local_message: str
    notification: NotificationMessage | None = None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _data_path(filename: str) -> Path:
    return _project_root() / "data" / filename


def config_path() -> Path:
    return _data_path("t_yield_reminder_config.json")


def state_path() -> Path:
    return _data_path("t_yield_reminder_state.json")


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_summary_time(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.strip().split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("summary time must be HH:MM")
    return hour, minute


def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _migrate_inventory_scope(payload: dict[str, Any]) -> str:
    if payload.get("inventory_scope"):
        return str(payload["inventory_scope"])

    hot_inventory_filter = str(payload.get("hot_inventory_filter") or "").strip().lower()
    daily_inventory_filter = str(payload.get("daily_inventory_filter") or "").strip().lower()
    candidate = hot_inventory_filter or daily_inventory_filter

    if candidate in {"cooldown_only", "all_cooldown", "full_cooldown"}:
        return INVENTORY_SCOPE_ALL_COOLDOWN
    if candidate in {"all", ""}:
        return INVENTORY_SCOPE_ALL if candidate == "all" else INVENTORY_SCOPE_NOT_ALL_COOLDOWN
    return INVENTORY_SCOPE_NOT_ALL_COOLDOWN


def load_config() -> TYieldReminderConfig | None:
    payload = _load_json_file(config_path())
    if payload is None:
        return None
    config = TYieldReminderConfig(
        top_n=int(payload.get("top_n") or 10),
        min_price=float(payload.get("min_price") or 10.0),
        steam_discount=float(payload.get("steam_discount") or 0.73),
        hot_threshold_pct=float(payload.get("hot_threshold_pct") or 10.0),
        poll_interval_minutes=int(payload.get("poll_interval_minutes") or 30),
        daily_summary_time=str(payload.get("daily_summary_time") or "15:30"),
        inventory_scope=_migrate_inventory_scope(payload),
        allow_cached_fallback=bool(payload.get("allow_cached_fallback", True)),
        cache_max_age_minutes=int(payload.get("cache_max_age_minutes") or 180),
    )
    config.validate()
    return config


def save_config(config: TYieldReminderConfig) -> Path:
    config.validate()
    path = config_path()
    ensure_parent_dir(path)
    payload = asdict(config)
    payload["updated_at"] = utc_now_iso()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_state() -> TYieldReminderState:
    payload = _load_json_file(state_path()) or {}
    return TYieldReminderState(
        last_hot_signature=payload.get("last_hot_signature"),
        last_hot_sent_at=payload.get("last_hot_sent_at"),
        last_daily_summary_date=payload.get("last_daily_summary_date"),
    )


def save_state(state: TYieldReminderState) -> Path:
    path = state_path()
    ensure_parent_dir(path)
    payload = asdict(state)
    payload["updated_at"] = utc_now_iso()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _prompt(text: str, default: str, parser: Callable[[str], Any]) -> Any:
    while True:
        raw = input(f"{text} [{default}]: ").strip()
        if not raw:
            raw = default
        try:
            return parser(raw)
        except Exception:
            print("输入格式不正确，请重新输入。")


def _prompt_bool(text: str, default: bool) -> bool:
    default_label = "Y" if default else "N"
    while True:
        raw = input(f"{text} [默认 {default_label}, 输入 Y/N]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("请输入 Y 或 N。")


def _prompt_inventory_scope(default_scope: str) -> str:
    current_scope = normalize_inventory_scope(default_scope)
    default_choice = next(
        (choice for choice, value in INVENTORY_SCOPE_CHOICES.items() if value == current_scope),
        "1",
    )
    print("提醒饰品范围：")
    print("1. 存在不冷却（推荐）")
    print("2. 全冷却")
    print("3. 全部")
    selected_choice = _prompt(
        "输入编号",
        default_choice,
        lambda value: value if value in INVENTORY_SCOPE_CHOICES else (_ for _ in ()).throw(ValueError()),
    )
    return INVENTORY_SCOPE_CHOICES[selected_choice]


def prompt_for_config(existing: TYieldReminderConfig | None = None) -> TYieldReminderConfig:
    current = existing or TYieldReminderConfig()
    print("提醒规则固定为：高收益快报 + 15:30 固定提醒")
    inventory_scope = _prompt_inventory_scope(current.inventory_scope)

    config = TYieldReminderConfig(
        top_n=_prompt("Top N 候选数量", str(current.top_n), int),
        min_price=_prompt("C5 最低售价门槛", f"{current.min_price:g}", float),
        steam_discount=_prompt("Steam 余额折扣", f"{current.steam_discount:g}", float),
        hot_threshold_pct=_prompt("高收益提醒阈值(%)", f"{current.hot_threshold_pct:g}", float),
        poll_interval_minutes=_prompt("轮询间隔(分钟)", str(current.poll_interval_minutes), int),
        daily_summary_time=_prompt("固定提醒时间(HH:MM)", current.daily_summary_time, str),
        inventory_scope=inventory_scope,
        allow_cached_fallback=_prompt_bool(
            "C5 库存拉取失败时是否允许回退到缓存",
            current.allow_cached_fallback,
        ),
        cache_max_age_minutes=_prompt(
            "缓存最大可接受时长(分钟)",
            str(current.cache_max_age_minutes),
            int,
        ),
    )
    config.validate()
    return config


def _matches_inventory_scope(candidate: TYieldCandidate | MissingSteamPriceIssue, scope: str) -> bool:
    normalized_scope = normalize_inventory_scope(scope)
    if normalized_scope == INVENTORY_SCOPE_ALL:
        return True
    if normalized_scope == INVENTORY_SCOPE_ALL_COOLDOWN:
        return candidate.tradable_count == 0
    return candidate.tradable_count > 0


def _account_summary(accounts: list[TYieldAccountRef]) -> str:
    labels: list[str] = []
    seen: set[str] = set()
    for account in accounts:
        label = str(account.nickname or account.steam_id or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    if not labels:
        return "-"
    if len(labels) <= 2:
        return ", ".join(labels)
    return f"{', '.join(labels[:2])} 等{len(labels)}个账号"


def _filter_candidates_for_scope(
    candidates: list[TYieldCandidate],
    scope: str,
) -> list[TYieldCandidate]:
    return [candidate for candidate in candidates if _matches_inventory_scope(candidate, scope)]


def _filter_missing_for_scope(
    issues: list[MissingSteamPriceIssue],
    scope: str,
) -> list[MissingSteamPriceIssue]:
    return [issue for issue in issues if _matches_inventory_scope(issue, scope)]


def _candidate_line(candidate: TYieldCandidate, *, starred: bool) -> list[str]:
    prefix = "★" if starred else "-"
    return [
        f"{prefix} {candidate.name} | 收益 {candidate.t_yield_pct:.2f}%",
        (
            f"  C5 {candidate.c5_lowest_sell_price:.2f} | "
            f"Steam {candidate.steam_lowest_sell_price:.2f} | "
            f"折算比 {candidate.ratio:.4f} | {candidate.inventory_status_summary} | "
            f"账号 { _account_summary(candidate.steam_accounts) }"
        ),
    ]


def _top_lines(candidates: list[TYieldCandidate], config: TYieldReminderConfig) -> list[str]:
    lines: list[str] = []
    for candidate in candidates[: config.top_n]:
        lines.extend(
            _candidate_line(
                candidate,
                starred=candidate.t_yield_pct >= config.hot_threshold_pct,
            )
        )
    if not lines:
        lines.append("- 当前没有符合条件的做T候选。")
    return lines


def _missing_lines(missing_issues: list[MissingSteamPriceIssue], path: str) -> list[str]:
    if not missing_issues:
        return []
    lines = [
        "",
        f"缺少 Steam 价格: {len(missing_issues)} 个",
    ]
    for issue in missing_issues[:5]:
        lines.append(
            f"- {issue.name} | C5 {issue.c5_sell_price:.2f} | "
            f"{issue.inventory_status_summary} | 账号 {_account_summary(issue.steam_accounts)}"
        )
    lines.append(f"- 明细: {path}")
    return lines


def build_notification_message(
    report: TYieldScanReport,
    config: TYieldReminderConfig,
    *,
    reason: str,
) -> NotificationMessage:
    candidates = _filter_candidates_for_scope(report.candidates, config.inventory_scope)
    missing_issues = _filter_missing_for_scope(report.missing_steam_prices, config.inventory_scope)
    scope_label = inventory_scope_label(config.inventory_scope)

    if reason == "hot":
        hot_count = sum(
            1 for candidate in candidates if candidate.t_yield_pct >= config.hot_threshold_pct
        )
        title = f"CS2 做T高收益提醒 {hot_count}个"
        header = [
            "做T高收益提醒",
            f"范围: {scope_label}",
            f"条件: 收益 >= {config.hot_threshold_pct:.2f}% | C5 >= {config.min_price:.2f}",
            f"来源: {report.inventory_source}",
            "",
        ]
    else:
        title = f"CS2 做T 15:30 固定提醒 Top{min(len(candidates), config.top_n)}"
        header = [
            "做T 15:30 固定提醒",
            f"范围: {scope_label}",
            f"C5 >= {config.min_price:.2f}",
            f"来源: {report.inventory_source}",
            "",
        ]

    body_lines = header + _top_lines(candidates, config) + _missing_lines(
        missing_issues,
        report.missing_steam_price_path,
    )
    return NotificationMessage(title=title, body="\n".join(body_lines))


def build_local_message(
    report: TYieldScanReport,
    config: TYieldReminderConfig,
    *,
    reason: str,
    note: str | None = None,
) -> str:
    shown_candidates = _filter_candidates_for_scope(report.candidates, config.inventory_scope)
    missing_issues = _filter_missing_for_scope(report.missing_steam_prices, config.inventory_scope)
    lines = [
        f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] 做T提醒扫描",
        "- 提醒规则: 高收益快报 + 15:30 固定提醒",
        f"- 触发原因: {reason}",
        f"- 饰品范围: {inventory_scope_label(config.inventory_scope)}",
        f"- 库存来源: {report.inventory_source}",
        f"- 库存类型数: {report.inventory_type_count}/{report.inventory_type_total_count}",
        f"- 候选总数: {len(report.candidates)}",
        f"- 当前规则候选数: {len(shown_candidates)}",
        f"- 缺少 Steam 价格: {len(missing_issues)}",
    ]
    if report.inventory_source == "cache" and report.inventory_cached_at:
        lines.append(f"- 缓存时间: {report.inventory_cached_at}")
    if note:
        lines.append(f"- 备注: {note}")
    lines.extend(["", *_top_lines(shown_candidates, config), *_missing_lines(missing_issues, report.missing_steam_price_path)])
    return "\n".join(lines)


def _hot_signature(candidates: list[TYieldCandidate], threshold_pct: float) -> str | None:
    hot_rows = [
        {
            "marketHashName": candidate.market_hash_name,
            "tYieldPct": round(candidate.t_yield_pct, 4),
        }
        for candidate in candidates
        if candidate.t_yield_pct >= threshold_pct
    ]
    if not hot_rows:
        return None
    return json.dumps(hot_rows, ensure_ascii=False, sort_keys=True)


def _is_daily_summary_due(
    now: datetime,
    config: TYieldReminderConfig,
    state: TYieldReminderState,
) -> bool:
    hour, minute = _parse_summary_time(config.daily_summary_time)
    summary_time_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < summary_time_today:
        return False
    return state.last_daily_summary_date != now.date().isoformat()


def evaluate_reminder(
    report: TYieldScanReport,
    config: TYieldReminderConfig,
    state: TYieldReminderState,
    *,
    now: datetime | None = None,
) -> TYieldReminderDecision:
    current_time = now or datetime.now().astimezone()
    scoped_candidates = _filter_candidates_for_scope(report.candidates, config.inventory_scope)
    hot_signature = _hot_signature(scoped_candidates, config.hot_threshold_pct)

    if hot_signature is None:
        state.last_hot_signature = None

    if _is_daily_summary_due(current_time, config, state):
        state.last_daily_summary_date = current_time.date().isoformat()
        return TYieldReminderDecision(
            reason="daily",
            should_notify=True,
            local_message=build_local_message(report, config, reason="daily"),
            notification=build_notification_message(report, config, reason="daily"),
        )

    if hot_signature:
        notification = build_notification_message(report, config, reason="hot")
        if state.last_hot_signature != hot_signature:
            state.last_hot_signature = hot_signature
            state.last_hot_sent_at = current_time.isoformat()
            return TYieldReminderDecision(
                reason="hot",
                should_notify=True,
                local_message=build_local_message(report, config, reason="hot"),
                notification=notification,
            )
        return TYieldReminderDecision(
            reason="hot_duplicate",
            should_notify=False,
            local_message=build_local_message(
                report,
                config,
                reason="hot_duplicate",
                note="高收益候选和上次相同，本次不重复推送。",
            ),
            notification=None,
        )

    return TYieldReminderDecision(
        reason="local_only",
        should_notify=False,
        local_message=build_local_message(
            report,
            config,
            reason="local_only",
            note="本次未命中推送条件，仅在本地输出。",
        ),
        notification=None,
    )


def run_once(
    settings: Settings,
    config: TYieldReminderConfig,
    state: TYieldReminderState,
) -> TYieldReminderDecision:
    report = scan_t_yield(
        settings,
        min_price=config.min_price,
        steam_discount=config.steam_discount,
        allow_cached_fallback=config.allow_cached_fallback,
        cache_max_age_minutes=config.cache_max_age_minutes,
        inventory_filter=INVENTORY_FILTER_ALL,
    )
    return evaluate_reminder(report, config, state)


def _seconds_until_next_run(now: datetime, interval_minutes: int) -> int:
    minute_bucket = (now.minute // interval_minutes) * interval_minutes
    next_run = now.replace(minute=minute_bucket, second=0, microsecond=0)
    if next_run <= now:
        next_run = next_run.replace(second=0, microsecond=0) + timedelta(minutes=interval_minutes)
    return max(1, int((next_run - now).total_seconds()))


def _deliver(settings: Settings, decision: TYieldReminderDecision) -> None:
    if not decision.should_notify or decision.notification is None:
        print(decision.local_message)
        return

    if not settings.serverchan_sendkey:
        print(
            decision.local_message
            + "\n\n[提醒降级] 当前命中推送条件，但缺少 SERVERCHAN_SENDKEY / SCTKEY，已改为本地输出。"
        )
        return

    service = NotificationService(
        ServerChanClient(
            settings.serverchan_sendkey,
            base_url=settings.serverchan_base_url,
        )
    )
    service.send(decision.notification)
    print(
        f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"已发送 ServerChan 提醒: {decision.notification.title}"
    )


def _configure_if_needed(existing: TYieldReminderConfig | None, force: bool) -> TYieldReminderConfig:
    if existing is not None and not force:
        return existing
    config = prompt_for_config(existing)
    path = save_config(config)
    print(f"提醒配置已保存: {path}")
    return config


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="做T提醒独立脚本")
    parser.add_argument("--configure", action="store_true", help="重新配置做T提醒参数")
    parser.add_argument("--once", action="store_true", help="只执行一次扫描与提醒判断")
    parser.add_argument("--show-config", action="store_true", help="输出当前提醒配置")
    parser.add_argument("--show-missing-steam", action="store_true", help="输出最近一次缺失 Steam 价格的明细")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()

    if args.show_missing_steam:
        _print_json(load_missing_steam_report(settings))
        return 0

    current_config = load_config()
    config = _configure_if_needed(current_config, args.configure or current_config is None)
    config.validate()

    if args.show_config:
        _print_json(asdict(config))
        if not args.once:
            return 0

    state = load_state()

    if args.once:
        decision = run_once(settings, config, state)
        save_state(state)
        _deliver(settings, decision)
        return 0

    print("做T提醒脚本已启动。按 Ctrl+C 可以停止。")
    print("提醒规则: 高收益快报 + 15:30 固定提醒")
    print(
        f"轮询间隔: {config.poll_interval_minutes} 分钟 | "
        f"高收益阈值: {config.hot_threshold_pct:.2f}% | "
        f"饰品范围: {inventory_scope_label(config.inventory_scope)}"
    )

    try:
        while True:
            decision = run_once(settings, config, state)
            save_state(state)
            _deliver(settings, decision)
            now = datetime.now().astimezone()
            sleep_seconds = _seconds_until_next_run(now, config.poll_interval_minutes)
            next_time = now + timedelta(seconds=sleep_seconds)
            print(f"下一次扫描时间: {next_time.strftime('%Y-%m-%d %H:%M:%S')}")
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        print("做T提醒脚本已停止。")
        return 130
