from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
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
    warm_threshold_pct: float = 0.5
    warm_cooldown_hours: float = 2.0
    signature_profit_bucket_pct: float = 0.25
    poll_interval_minutes: int = 30
    fixed_summary_times: list[str] = field(default_factory=lambda: ["08:30", "18:00"])
    no_hot_summary_interval_hours: float = 4.0
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
        if self.warm_threshold_pct <= 0:
            raise ValueError("warm_threshold_pct must be positive")
        if self.warm_threshold_pct >= self.hot_threshold_pct:
            raise ValueError("warm_threshold_pct must be lower than hot_threshold_pct")
        if self.warm_cooldown_hours <= 0:
            raise ValueError("warm_cooldown_hours must be positive")
        if self.signature_profit_bucket_pct <= 0:
            raise ValueError("signature_profit_bucket_pct must be positive")
        if self.poll_interval_minutes <= 0:
            raise ValueError("poll_interval_minutes must be positive")
        if self.no_hot_summary_interval_hours <= 0:
            raise ValueError("no_hot_summary_interval_hours must be positive")
        if self.cache_max_age_minutes <= 0:
            raise ValueError("cache_max_age_minutes must be positive")
        if not self.fixed_summary_times:
            raise ValueError("fixed_summary_times must not be empty")
        self.fixed_summary_times = _normalize_summary_times(self.fixed_summary_times)
        self.inventory_scope = normalize_inventory_scope(self.inventory_scope)


@dataclass(slots=True)
class TYieldReminderState:
    last_hot_signature: str | None = None
    last_hot_sent_at: str | None = None
    last_warm_signature: str | None = None
    last_warm_sent_at: str | None = None
    last_fixed_summary_sent: dict[str, str] = field(default_factory=dict)
    last_no_hot_summary_at: str | None = None


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


def _format_summary_time(value: str) -> str:
    hour, minute = _parse_summary_time(value)
    return f"{hour:02d}:{minute:02d}"


def _normalize_summary_times(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        summary_time = _format_summary_time(str(value))
        if summary_time in seen:
            continue
        seen.add(summary_time)
        normalized.append(summary_time)
    normalized.sort(key=_parse_summary_time)
    return normalized


def _parse_summary_times(value: str) -> list[str]:
    raw_times = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    if not raw_times:
        raise ValueError("summary times must not be empty")
    return _normalize_summary_times(raw_times)


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
    raw_fixed_summary_times = payload.get("fixed_summary_times")
    if isinstance(raw_fixed_summary_times, list):
        fixed_summary_times = [str(value) for value in raw_fixed_summary_times]
    elif raw_fixed_summary_times:
        fixed_summary_times = _parse_summary_times(str(raw_fixed_summary_times))
    elif payload.get("daily_summary_time"):
        fixed_summary_times = [str(payload["daily_summary_time"])]
    else:
        fixed_summary_times = ["08:30", "18:00"]
    config = TYieldReminderConfig(
        top_n=int(payload.get("top_n") or 10),
        min_price=float(payload.get("min_price") or 10.0),
        steam_discount=float(payload.get("steam_discount") or 0.73),
        hot_threshold_pct=float(payload.get("hot_threshold_pct") or 10.0),
        warm_threshold_pct=float(payload.get("warm_threshold_pct") or 0.5),
        warm_cooldown_hours=float(payload.get("warm_cooldown_hours") or 2.0),
        signature_profit_bucket_pct=float(payload.get("signature_profit_bucket_pct") or 0.25),
        poll_interval_minutes=int(payload.get("poll_interval_minutes") or 30),
        fixed_summary_times=fixed_summary_times,
        no_hot_summary_interval_hours=float(payload.get("no_hot_summary_interval_hours") or 4.0),
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
    raw_fixed_summary_sent = payload.get("last_fixed_summary_sent")
    fixed_summary_sent: dict[str, str] = {}
    if isinstance(raw_fixed_summary_sent, dict):
        fixed_summary_sent = {
            _format_summary_time(str(key)): str(value)
            for key, value in raw_fixed_summary_sent.items()
            if value
        }
    elif payload.get("last_daily_summary_date") and payload.get("daily_summary_time"):
        fixed_summary_sent[_format_summary_time(str(payload["daily_summary_time"]))] = str(
            payload["last_daily_summary_date"]
        )
    return TYieldReminderState(
        last_hot_signature=payload.get("last_hot_signature"),
        last_hot_sent_at=payload.get("last_hot_sent_at"),
        last_warm_signature=payload.get("last_warm_signature"),
        last_warm_sent_at=payload.get("last_warm_sent_at"),
        last_fixed_summary_sent=fixed_summary_sent,
        last_no_hot_summary_at=payload.get("last_no_hot_summary_at"),
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
    print("提醒规则固定为：定时扫描 + 一档快报 + 二档机会 + 固定摘要 + 无机会摘要")
    inventory_scope = _prompt_inventory_scope(current.inventory_scope)

    config = TYieldReminderConfig(
        top_n=_prompt("Top N 候选数量", str(current.top_n), int),
        min_price=_prompt("C5 最低售价门槛", f"{current.min_price:g}", float),
        steam_discount=_prompt("Steam 余额折扣", f"{current.steam_discount:g}", float),
        hot_threshold_pct=_prompt("一档快报阈值(%)", f"{current.hot_threshold_pct:g}", float),
        warm_threshold_pct=_prompt("二档机会阈值(%)", f"{current.warm_threshold_pct:g}", float),
        warm_cooldown_hours=_prompt("二档机会提醒间隔(小时)", f"{current.warm_cooldown_hours:g}", float),
        signature_profit_bucket_pct=_prompt("相似收益率判定粒度(%)", f"{current.signature_profit_bucket_pct:g}", float),
        poll_interval_minutes=_prompt("轮询间隔(分钟)", str(current.poll_interval_minutes), int),
        fixed_summary_times=_prompt(
            "固定提醒时间(HH:MM, 多个用逗号分隔)",
            ",".join(current.fixed_summary_times),
            _parse_summary_times,
        ),
        no_hot_summary_interval_hours=_prompt(
            "无机会摘要间隔(小时)",
            f"{current.no_hot_summary_interval_hours:g}",
            float,
        ),
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


def _hot_candidates(candidates: list[TYieldCandidate], config: TYieldReminderConfig) -> list[TYieldCandidate]:
    return [
        candidate
        for candidate in candidates
        if candidate.t_yield_pct >= config.hot_threshold_pct
    ]


def _warm_candidates(candidates: list[TYieldCandidate], config: TYieldReminderConfig) -> list[TYieldCandidate]:
    return [
        candidate
        for candidate in candidates
        if config.warm_threshold_pct <= candidate.t_yield_pct < config.hot_threshold_pct
    ]


def _candidate_tier_label(candidate: TYieldCandidate, config: TYieldReminderConfig) -> str:
    if candidate.t_yield_pct >= config.hot_threshold_pct:
        return "一档"
    if candidate.t_yield_pct >= config.warm_threshold_pct:
        return "二档"
    return "普通"


def _candidate_line(candidate: TYieldCandidate, config: TYieldReminderConfig) -> list[str]:
    tier_label = _candidate_tier_label(candidate, config)
    prefix = "★" if tier_label == "一档" else "◆" if tier_label == "二档" else "-"
    return [
        f"{prefix} [{tier_label}] {candidate.name}",
        (
            f"利润 {candidate.t_yield_pct:.2f}% | "
            f"折算比 {candidate.ratio:.4f} | 挂刀比 {candidate.listing_ratio:.4f}"
        ),
        f"C5 {candidate.c5_lowest_sell_price:.2f} | Steam {candidate.steam_lowest_sell_price:.2f}",
        f"库存 {candidate.inventory_status_summary}",
        f"账号 {_account_summary(candidate.steam_accounts)}",
        "",
    ]


def _top_lines(candidates: list[TYieldCandidate], config: TYieldReminderConfig) -> list[str]:
    lines: list[str] = []
    for candidate in candidates[: config.top_n]:
        lines.extend(_candidate_line(candidate, config))
    if not lines:
        lines.append("- 当前没有符合条件的做T候选。")
    return lines


def _missing_lines(missing_issues: list[MissingSteamPriceIssue], path: str) -> list[str]:
    if not missing_issues:
        return []
    lines = ["", f"缺少 Steam 价格: {len(missing_issues)} 个"]
    for issue in missing_issues[:5]:
        lines.append(f"- {issue.name}")
        lines.append(f"  C5 {issue.c5_sell_price:.2f} | {issue.inventory_status_summary}")
        lines.append(f"  账号 {_account_summary(issue.steam_accounts)}")
    lines.append(f"- 明细: {path}")
    return lines


def _shown_candidates_for_reason(
    candidates: list[TYieldCandidate],
    config: TYieldReminderConfig,
    reason: str,
) -> list[TYieldCandidate]:
    return candidates


def _filter_summary_lines(
    report: TYieldScanReport,
    config: TYieldReminderConfig,
    scoped_candidates: list[TYieldCandidate],
    scoped_missing: list[MissingSteamPriceIssue],
    shown_candidates: list[TYieldCandidate],
) -> list[str]:
    hot_count = len(_hot_candidates(scoped_candidates, config))
    warm_count = len(_warm_candidates(scoped_candidates, config))
    return [
        f"筛选: 总库存品类 {report.inventory_type_total_count} | 扫描范围 {report.inventory_type_count}",
        f"筛选: 当前库存范围 {inventory_scope_label(config.inventory_scope)} | 完整价格候选 {len(report.candidates)} | 当前范围候选 {len(scoped_candidates)}",
        f"筛选: 一档命中 {hot_count} (利润 >= {config.hot_threshold_pct:.2f}%) | 二档命中 {warm_count} ({config.warm_threshold_pct:.2f}% <= 利润 < {config.hot_threshold_pct:.2f}%)",
        f"筛选: 缺少 Steam 价格 {len(scoped_missing)} | 展示 {min(len(shown_candidates), config.top_n)}/{config.top_n}",
    ]


def build_notification_message(
    report: TYieldScanReport,
    config: TYieldReminderConfig,
    *,
    reason: str,
) -> NotificationMessage:
    scoped_candidates = _filter_candidates_for_scope(report.candidates, config.inventory_scope)
    missing_issues = _filter_missing_for_scope(report.missing_steam_prices, config.inventory_scope)
    shown_candidates = _shown_candidates_for_reason(scoped_candidates, config, reason)
    scope_label = inventory_scope_label(config.inventory_scope)
    shown_count = min(len(shown_candidates), config.top_n)
    title_count = f"Top{shown_count}/{config.top_n}"
    filter_lines = _filter_summary_lines(report, config, scoped_candidates, missing_issues, shown_candidates)
    hot_count = len(_hot_candidates(scoped_candidates, config))
    warm_count = len(_warm_candidates(scoped_candidates, config))

    if reason == "hot":
        title = f"CS2 做T机会提醒 一档{hot_count}/二档{warm_count}"
        header = [
            "做T机会提醒",
            f"条件: 利润 >= {config.hot_threshold_pct:.2f}% | 每 {config.poll_interval_minutes} 分钟扫描 | 相似则不重复",
            "内容: 完整 Top N，一档/二档/普通都展示",
        ]
    elif reason == "warm":
        title = f"CS2 做T机会提醒 二档{warm_count}"
        header = [
            "做T机会提醒",
            (
                f"条件: {config.warm_threshold_pct:.2f}% <= 利润 < {config.hot_threshold_pct:.2f}% | "
                f"最多每 {config.warm_cooldown_hours:g} 小时推送一次 | 相似则不重复"
            ),
            "内容: 完整 Top N，一档/二档/普通都展示",
        ]
    elif reason == "startup":
        title = f"CS2 做T启动提醒 {title_count}"
        header = ["做T启动提醒", "notify 命令启动首轮强制推送", "内容: 完整 Top N，一档/二档/普通都展示"]
    elif reason == "no_hot_summary":
        title = f"CS2 做T无机会摘要 {title_count}"
        header = [
            "做T无机会摘要",
            f"条件: 当前无利润 >= {config.warm_threshold_pct:.2f}% 的候选 | 摘要间隔 {config.no_hot_summary_interval_hours:g} 小时",
            "内容: 完整 Top N，普通候选也展示",
        ]
    else:
        fixed_time = reason.split(":", 1)[1] if reason.startswith("fixed:") else ""
        fixed_label = f"{fixed_time} 固定提醒" if fixed_time else "固定提醒"
        title = f"CS2 做T{fixed_label} {title_count}"
        header = [f"做T{fixed_label}", "固定报告不受一档/二档去重限制", "内容: 完整 Top N，一档/二档/普通都展示"]

    body_lines = [
        *header,
        f"范围: {scope_label}",
        f"库存源: {report.inventory_source}",
        "价格源: C5官方API / Steam官方orderbook",
        "公式: 折算比=C5/Steam×0.99 | 利润=折算比-导余额折扣 | 面折比=C5/Steam",
        *filter_lines,
        "",
        *_top_lines(shown_candidates, config),
        *_missing_lines(missing_issues, report.missing_steam_price_path),
    ]
    return NotificationMessage(title=title, body="\n".join(body_lines).strip())


def build_local_message(
    report: TYieldScanReport,
    config: TYieldReminderConfig,
    *,
    reason: str,
    note: str | None = None,
) -> str:
    scoped_candidates = _filter_candidates_for_scope(report.candidates, config.inventory_scope)
    missing_issues = _filter_missing_for_scope(report.missing_steam_prices, config.inventory_scope)
    shown_candidates = _shown_candidates_for_reason(scoped_candidates, config, reason)
    lines = [
        f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] 做T提醒扫描",
        (
            "- 提醒规则: "
            f"每 {config.poll_interval_minutes} 分钟扫描 | "
            f"一档 >= {config.hot_threshold_pct:.2f}% 即时去重 | "
            f"二档 {config.warm_threshold_pct:.2f}%~{config.hot_threshold_pct:.2f}% 每 {config.warm_cooldown_hours:g} 小时去重 | "
            f"无机会 {config.no_hot_summary_interval_hours:g} 小时摘要 | "
            f"固定 {', '.join(config.fixed_summary_times)}"
        ),
        f"- 触发原因: {reason}",
        f"- 范围: {inventory_scope_label(config.inventory_scope)}",
        f"- 库存源: {report.inventory_source}",
        "- 价格源: C5官方API / Steam官方orderbook",
        "- 公式: 折算比=C5/Steam×0.99 | 利润=折算比-导余额折扣 | 面折比=C5/Steam",
        *_filter_summary_lines(report, config, scoped_candidates, missing_issues, shown_candidates),
    ]
    if report.inventory_source == "cache" and report.inventory_cached_at:
        lines.append(f"- 缓存时间: {report.inventory_cached_at}")
    if note:
        lines.append(f"- 备注: {note}")
    lines.extend(["", *_top_lines(shown_candidates, config), *_missing_lines(missing_issues, report.missing_steam_price_path)])
    return "\n".join(lines).strip()


def _bucket_profit_pct(value: float, bucket_pct: float) -> float:
    return round(round(value / bucket_pct) * bucket_pct, 4)


def _tier_signature(
    candidates: list[TYieldCandidate],
    config: TYieldReminderConfig,
    *,
    tier: str,
) -> str | None:
    if tier == "hot":
        tier_candidates = _hot_candidates(candidates, config)
    elif tier == "warm":
        tier_candidates = _warm_candidates(candidates, config)
    else:
        raise ValueError(f"unsupported tier: {tier}")
    rows = [
        {
            "marketHashName": candidate.market_hash_name,
            "profitBucketPct": _bucket_profit_pct(
                candidate.t_yield_pct,
                config.signature_profit_bucket_pct,
            ),
        }
        for candidate in tier_candidates[: config.top_n]
    ]
    if not rows:
        return None
    return json.dumps(rows, ensure_ascii=False, sort_keys=True)


def _latest_due_fixed_summary_time(
    now: datetime,
    config: TYieldReminderConfig,
    state: TYieldReminderState,
) -> str | None:
    today = now.date().isoformat()
    due_times: list[str] = []
    window = timedelta(minutes=max(1, config.poll_interval_minutes))
    for summary_time in config.fixed_summary_times:
        hour, minute = _parse_summary_time(summary_time)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now <= target + window:
            due_times.append(summary_time)
    if not due_times:
        return None
    latest_due = due_times[-1]
    if state.last_fixed_summary_sent.get(latest_due) == today:
        return None
    return latest_due


def _mark_latest_due_fixed_summary_sent(
    now: datetime,
    config: TYieldReminderConfig,
    state: TYieldReminderState,
) -> None:
    today = now.date().isoformat()
    due_times: list[str] = []
    window = timedelta(minutes=max(1, config.poll_interval_minutes))
    for summary_time in config.fixed_summary_times:
        hour, minute = _parse_summary_time(summary_time)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now <= target + window:
            due_times.append(summary_time)
    if due_times:
        state.last_fixed_summary_sent[due_times[-1]] = today


def _parse_state_datetime(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or now.tzinfo is None:
        return parsed.replace(tzinfo=None)
    return parsed.astimezone(now.tzinfo)


def _is_no_hot_summary_due(
    now: datetime,
    config: TYieldReminderConfig,
    state: TYieldReminderState,
) -> bool:
    last_sent_at = _parse_state_datetime(state.last_no_hot_summary_at, now)
    if last_sent_at is None:
        return True
    comparable_now = now if last_sent_at.tzinfo is not None else now.replace(tzinfo=None)
    interval = timedelta(hours=config.no_hot_summary_interval_hours)
    return comparable_now - last_sent_at >= interval


def _is_warm_due(
    now: datetime,
    config: TYieldReminderConfig,
    state: TYieldReminderState,
) -> bool:
    last_sent_at = _parse_state_datetime(state.last_warm_sent_at, now)
    if last_sent_at is None:
        return True
    comparable_now = now if last_sent_at.tzinfo is not None else now.replace(tzinfo=None)
    interval = timedelta(hours=config.warm_cooldown_hours)
    return comparable_now - last_sent_at >= interval


def evaluate_reminder(
    report: TYieldScanReport,
    config: TYieldReminderConfig,
    state: TYieldReminderState,
    *,
    now: datetime | None = None,
    force_startup_notify: bool = False,
) -> TYieldReminderDecision:
    current_time = now or datetime.now().astimezone()
    scoped_candidates = _filter_candidates_for_scope(report.candidates, config.inventory_scope)
    hot_signature = _tier_signature(scoped_candidates, config, tier="hot")
    warm_signature = _tier_signature(scoped_candidates, config, tier="warm")

    if hot_signature is None:
        state.last_hot_signature = None
    if warm_signature is None:
        state.last_warm_signature = None

    if force_startup_notify:
        if hot_signature:
            state.last_hot_signature = hot_signature
            state.last_hot_sent_at = current_time.isoformat()
        if warm_signature:
            state.last_warm_signature = warm_signature
            state.last_warm_sent_at = current_time.isoformat()
        if hot_signature is None and warm_signature is None:
            state.last_no_hot_summary_at = current_time.isoformat()
        _mark_latest_due_fixed_summary_sent(current_time, config, state)
        return TYieldReminderDecision(
            reason="startup",
            should_notify=True,
            local_message=build_local_message(
                report,
                config,
                reason="startup",
                note="notify 命令启动首轮，按规则强制推送一次。",
            ),
            notification=build_notification_message(report, config, reason="startup"),
        )

    fixed_summary_time = _latest_due_fixed_summary_time(current_time, config, state)
    if fixed_summary_time:
        state.last_fixed_summary_sent[fixed_summary_time] = current_time.date().isoformat()
        if hot_signature is None and warm_signature is None:
            state.last_no_hot_summary_at = current_time.isoformat()
        reason = f"fixed:{fixed_summary_time}"
        return TYieldReminderDecision(
            reason=reason,
            should_notify=True,
            local_message=build_local_message(report, config, reason=reason),
            notification=build_notification_message(report, config, reason=reason),
        )

    if hot_signature:
        notification = build_notification_message(report, config, reason="hot")
        if state.last_hot_signature != hot_signature:
            state.last_hot_signature = hot_signature
            state.last_hot_sent_at = current_time.isoformat()
            if warm_signature:
                state.last_warm_signature = warm_signature
                state.last_warm_sent_at = current_time.isoformat()
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
                note="一档高收益候选和上次相似，本次不重复推送；二档会随一档报告展示，不单独补发。",
            ),
            notification=None,
        )

    if warm_signature:
        if not _is_warm_due(current_time, config, state):
            return TYieldReminderDecision(
                reason="warm_cooldown",
                should_notify=False,
                local_message=build_local_message(
                    report,
                    config,
                    reason="warm_cooldown",
                    note=(
                        f"二档机会仍在 {config.warm_cooldown_hours:g} 小时冷却窗口内，"
                        "本次不重复推送。"
                    ),
                ),
                notification=None,
            )
        if state.last_warm_signature != warm_signature:
            state.last_warm_signature = warm_signature
            state.last_warm_sent_at = current_time.isoformat()
            return TYieldReminderDecision(
                reason="warm",
                should_notify=True,
                local_message=build_local_message(report, config, reason="warm"),
                notification=build_notification_message(report, config, reason="warm"),
            )
        return TYieldReminderDecision(
            reason="warm_duplicate",
            should_notify=False,
            local_message=build_local_message(
                report,
                config,
                reason="warm_duplicate",
                note="二档机会和上次相似，本次不重复推送。",
            ),
            notification=None,
        )

    if _is_no_hot_summary_due(current_time, config, state):
        state.last_no_hot_summary_at = current_time.isoformat()
        return TYieldReminderDecision(
            reason="no_hot_summary",
            should_notify=True,
            local_message=build_local_message(
                report,
                config,
                reason="no_hot_summary",
                note=(
                    f"当前没有利润达到二档阈值的候选，按 "
                    f"{config.no_hot_summary_interval_hours:g} 小时摘要间隔推送。"
                ),
            ),
            notification=build_notification_message(report, config, reason="no_hot_summary"),
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
    *,
    force_startup_notify: bool = False,
) -> TYieldReminderDecision:
    report = scan_t_yield(
        settings,
        min_price=config.min_price,
        steam_discount=config.steam_discount,
        allow_cached_fallback=config.allow_cached_fallback,
        cache_max_age_minutes=config.cache_max_age_minutes,
        inventory_filter=config.inventory_scope,
    )
    return evaluate_reminder(
        report,
        config,
        state,
        force_startup_notify=force_startup_notify,
    )


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
        decision = run_once(settings, config, state, force_startup_notify=True)
        save_state(state)
        _deliver(settings, decision)
        return 0

    print("做T提醒脚本已启动。按 Ctrl+C 可以停止。")
    print("提醒规则: 一档快报 + 二档机会 + 固定摘要 + 无机会摘要")
    print(
        f"轮询间隔: {config.poll_interval_minutes} 分钟 | "
        f"一档阈值: {config.hot_threshold_pct:.2f}% | "
        f"二档阈值: {config.warm_threshold_pct:.2f}% | "
        f"二档间隔: {config.warm_cooldown_hours:g} 小时 | "
        f"无机会摘要: {config.no_hot_summary_interval_hours:g} 小时 | "
        f"固定提醒: {', '.join(config.fixed_summary_times)} | "
        f"饰品范围: {inventory_scope_label(config.inventory_scope)}"
    )

    is_first_run = True
    try:
        while True:
            decision = run_once(settings, config, state, force_startup_notify=is_first_run)
            is_first_run = False
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
