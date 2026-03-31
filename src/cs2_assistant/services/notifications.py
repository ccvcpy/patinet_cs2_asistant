from __future__ import annotations

from typing import Any

from cs2_assistant.clients import ServerChanClient
from cs2_assistant.models import NotificationMessage, TriggeredAlert


class NotificationService:
    def __init__(self, serverchan_client: ServerChanClient | None = None):
        self.serverchan_client = serverchan_client

    def send(self, message: NotificationMessage) -> dict[str, Any] | None:
        if self.serverchan_client is None:
            return None
        return self.serverchan_client.send(message.title, message.body)

    @staticmethod
    def build_rule_alert_message(alerts: list[TriggeredAlert]) -> NotificationMessage:
        title = f"CS2 理财助手提醒 {len(alerts)} 条"
        lines = ["## 触发规则", ""]
        for index, alert in enumerate(alerts, start=1):
            lines.append(f"{index}. {alert.message}")
        return NotificationMessage(title=title, body="\n".join(lines))
