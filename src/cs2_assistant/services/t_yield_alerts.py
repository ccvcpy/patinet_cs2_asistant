from __future__ import annotations

from cs2_assistant.models import NotificationMessage


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
        "## 做T收益率提醒",
        "",
        f"- 条件: Top {top_n}",
        f"- C5最低售价门槛: {min_price:g}",
        "",
    ]

    if rows:
        for row in rows:
            account_names = ", ".join(
                str(account.get("nickname") or account.get("steamId") or "-")
                for account in row.get("steamAccounts", [])
            )
            lines.append(
                f"{row['rank']}. {row['name']} | 收益率 {row['tYieldPct']} | "
                f"挂刀比例 {row['ratio']} | C5 {row['c5LowestSellPrice']} | "
                f"Steam {row['steamLowestSellPrice']} | 账号 {account_names}"
            )
    else:
        lines.append("当前没有符合条件的做T候选。")

    if missing_steam_prices:
        lines.extend(["", "## 价格缺失", ""])
        for issue in missing_steam_prices[:10]:
            lines.append(
                f"- {issue['name']} | C5 {issue['c5SellPrice']} | "
                f"缺少 Steam 最低售价 | HashName={issue['marketHashName']}"
            )
        if len(missing_steam_prices) > 10:
            lines.append(f"- 其余 {len(missing_steam_prices) - 10} 个问题请用脚本查看。")

    return NotificationMessage(title=title, body="\n".join(lines))
