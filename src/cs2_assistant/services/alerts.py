from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from cs2_assistant.db import Database
from cs2_assistant.models import BasketState, MarketState, TriggeredAlert


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _metric_value(
    metric: str,
    *,
    item_state: MarketState | None = None,
    basket_state: BasketState | None = None,
    anchor_value: float | None = None,
) -> float | None:
    if metric == "c5_price":
        return item_state.c5_sell_price if item_state else None
    if metric == "steam_price":
        return item_state.steam_sell_price if item_state else None
    if metric == "c5_bid_price":
        return item_state.c5_bid_price if item_state else None
    if metric == "ratio":
        return item_state.ratio if item_state else None
    if metric == "basket_total":
        return basket_state.total_value if basket_state else None
    if metric == "c5_change_pct":
        if item_state is None or item_state.c5_sell_price is None or not anchor_value:
            return None
        return (item_state.c5_sell_price - anchor_value) / anchor_value * 100
    if metric == "steam_change_pct":
        if item_state is None or item_state.steam_sell_price is None or not anchor_value:
            return None
        return (item_state.steam_sell_price - anchor_value) / anchor_value * 100
    if metric == "basket_change_pct":
        if basket_state is None or not anchor_value:
            return None
        return (basket_state.total_value - anchor_value) / anchor_value * 100
    raise ValueError(f"Unsupported metric: {metric}")


def _compare(operator: str, observed_value: float, threshold: float) -> bool:
    if operator == "lte":
        return observed_value <= threshold
    if operator == "gte":
        return observed_value >= threshold
    raise ValueError(f"Unsupported operator: {operator}")


def _cooldown_active(last_triggered_at: str | None, cooldown_minutes: int) -> bool:
    last_trigger_time = _parse_iso(last_triggered_at)
    if last_trigger_time is None:
        return False
    now = datetime.now(timezone.utc)
    return now < last_trigger_time + timedelta(minutes=cooldown_minutes)


class AlertService:
    def __init__(self, db: Database):
        self.db = db

    def build_baskets(self, item_states: list[MarketState]) -> list[BasketState]:
        state_map = {state.market_hash_name: state for state in item_states}
        basket_rows = self.db.list_baskets()
        basket_items = self.db.list_basket_items()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in basket_items:
            grouped.setdefault(row["basket_name"], []).append(
                {
                    "market_hash_name": row["market_hash_name"],
                    "name_cn": row["name_cn"],
                    "quantity": row["quantity"],
                }
            )

        baskets: list[BasketState] = []
        for basket in basket_rows:
            components = grouped.get(basket["name"], [])
            total_value = 0.0
            detailed_components: list[dict[str, Any]] = []
            for component in components:
                item_state = state_map.get(component["market_hash_name"])
                quantity = float(component["quantity"] or 0)
                c5_price = item_state.c5_sell_price if item_state else None
                if c5_price is not None:
                    total_value += c5_price * quantity
                detailed_components.append(
                    {
                        "market_hash_name": component["market_hash_name"],
                        "name_cn": component["name_cn"],
                        "quantity": quantity,
                        "c5_price": c5_price,
                    }
                )
            basket_state = BasketState(
                name=basket["name"],
                total_value=total_value,
                components=detailed_components,
            )
            self.db.save_basket_snapshot(
                basket["name"],
                total_value,
                {"components": detailed_components},
            )
            baskets.append(basket_state)
        return baskets

    def evaluate(self, item_states: list[MarketState], basket_states: list[BasketState]) -> list[TriggeredAlert]:
        for state in item_states:
            self.db.save_price_snapshot(state)

        item_map = {state.market_hash_name: state for state in item_states}
        basket_map = {state.name: state for state in basket_states}
        alerts: list[TriggeredAlert] = []

        for rule in self.db.list_alert_rules(enabled_only=True):
            if _cooldown_active(rule["last_triggered_at"], int(rule["cooldown_minutes"])):
                continue

            item_state = None
            basket_state = None
            if rule["target_type"] == "item":
                item_state = item_map.get(rule["target_key"])
                if item_state is None:
                    continue
            elif rule["target_type"] == "basket":
                basket_state = basket_map.get(rule["target_key"])
                if basket_state is None:
                    continue
            else:
                continue

            observed = _metric_value(
                str(rule["metric"]),
                item_state=item_state,
                basket_state=basket_state,
                anchor_value=rule["anchor_value"],
            )
            if observed is None:
                continue
            if not _compare(str(rule["operator"]), observed, float(rule["threshold"])):
                continue

            message = self._build_message(rule, observed, item_state=item_state, basket_state=basket_state)
            triggered = TriggeredAlert(
                rule_id=int(rule["id"]),
                target_type=str(rule["target_type"]),
                target_key=str(rule["target_key"]),
                metric=str(rule["metric"]),
                observed_value=float(observed),
                threshold=float(rule["threshold"]),
                message=message,
            )
            alerts.append(triggered)
            self.db.add_alert_event(
                rule_id=triggered.rule_id,
                target_type=triggered.target_type,
                target_key=triggered.target_key,
                metric=triggered.metric,
                observed_value=triggered.observed_value,
                threshold=triggered.threshold,
                message=triggered.message,
            )
            self.db.set_rule_triggered(triggered.rule_id, triggered.observed_value)
        return alerts

    def _build_message(
        self,
        rule: Any,
        observed_value: float,
        *,
        item_state: MarketState | None,
        basket_state: BasketState | None,
    ) -> str:
        operator_text = "<=" if rule["operator"] == "lte" else ">="
        target_key = str(rule["target_key"])
        metric = str(rule["metric"])
        if item_state is not None:
            display_name = item_state.name_cn or target_key
            return (
                f"{display_name} 命中规则: {metric} {operator_text} {rule['threshold']} "
                f"(当前 {observed_value:.4f}, HashName={target_key})"
            )
        if basket_state is not None:
            return (
                f"篮子 {basket_state.name} 命中规则: {metric} {operator_text} {rule['threshold']} "
                f"(当前 {observed_value:.4f})"
            )
        return f"{target_key} 命中规则"

    @staticmethod
    def render_serverchan_markdown(alerts: list[TriggeredAlert]) -> str:
        lines = ["## 触发规则", ""]
        for index, alert in enumerate(alerts, start=1):
            lines.append(f"{index}. {alert.message}")
        return "\n".join(lines)
