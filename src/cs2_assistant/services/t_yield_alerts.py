from __future__ import annotations

from cs2_assistant.models import NotificationMessage


def _account_summary(accounts: list[dict[str, object]]) -> str:
    labels: list[str] = []
    seen: set[str] = set()
    for account in accounts:
        label = str(account.get("nickname") or account.get("steamId") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    if not labels:
        return "-"
    if len(labels) <= 2:
        return ", ".join(labels)
    return f"{', '.join(labels[:2])} 等{len(labels)}个账号"


def build_t_yield_notification(
    rows: list[dict[str, object]],
    *,
    top_n: int,
    min_price: float,
    missing_steam_prices: list[dict[str, object]],
) -> NotificationMessage:
    title = f"CS2 做T提醒 Top{min(len(rows), top_n)}"
    if min_price > 0:
        title += f" C5>={min_price:g}"

    lines = [
        "做T收益率提醒",
        f"条件: Top {top_n} | C5>={min_price:g}",
        "",
    ]

    if rows:
        for row in rows:
            inventory_status = str(row.get("inventoryStatusSummary") or "").strip()
            inventory_part = f" | {inventory_status}" if inventory_status else ""
            account_summary = _account_summary(list(row.get("steamAccounts", [])))
            lines.append(f"{row['rank']}. {row['name']}{inventory_part}")
            lines.append(
                f"   收益率 {row['tYieldPct']} | 折算比 {row['ratio']} | "
                f"C5 {row['c5LowestSellPrice']} | Steam {row['steamLowestSellPrice']} | "
                f"账号 {account_summary}"
            )
    else:
        lines.append("当前没有符合条件的做T候选。")

    if missing_steam_prices:
        lines.extend(["", f"缺少 Steam 价格: {len(missing_steam_prices)} 个"])
        for issue in missing_steam_prices[:10]:
            inventory_status = str(issue.get("inventoryStatusSummary") or "").strip()
            inventory_part = f" | {inventory_status}" if inventory_status else ""
            lines.append(
                f"- {issue['name']}{inventory_part} | C5 {issue['c5SellPrice']} | "
                f"HashName={issue['marketHashName']}"
            )
        if len(missing_steam_prices) > 10:
            lines.append(f"- 其余 {len(missing_steam_prices) - 10} 个请用脚本查看。")

    return NotificationMessage(title=title, body="\n".join(lines))
