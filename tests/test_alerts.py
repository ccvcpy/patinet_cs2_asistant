from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.db import Database
from cs2_assistant.models import CatalogItem, MarketState
from cs2_assistant.services.alerts import AlertService


class AlertServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "assistant.db"
        self.db = Database(self.db_path)
        self.db.initialize()
        self.db.upsert_items(
            [
                CatalogItem(
                    market_hash_name="AK-47 | Redline (Field-Tested)",
                    name_cn="AK-47 | 火神测试",
                    c5_item_id="123",
                ),
                CatalogItem(
                    market_hash_name="AWP | Asiimov (Battle-Scarred)",
                    name_cn="AWP | 二西莫夫测试",
                    c5_item_id="456",
                ),
            ]
        )
        self.db.add_watch_item("AK-47 | Redline (Field-Tested)")
        self.db.add_basket("test-basket")
        self.db.add_basket_item("test-basket", "AK-47 | Redline (Field-Tested)", quantity=1)
        self.db.add_basket_item("test-basket", "AWP | Asiimov (Battle-Scarred)", quantity=2)

    def tearDown(self) -> None:
        self.db.close()
        self.tempdir.cleanup()

    def test_item_price_and_ratio_rules_trigger(self) -> None:
        self.db.add_alert_rule(
            target_type="item",
            target_key="AK-47 | Redline (Field-Tested)",
            metric="c5_price",
            operator="lte",
            threshold=99,
            anchor_value=None,
            cooldown_minutes=60,
            note=None,
        )
        self.db.add_alert_rule(
            target_type="item",
            target_key="AK-47 | Redline (Field-Tested)",
            metric="ratio",
            operator="lte",
            threshold=0.95,
            anchor_value=None,
            cooldown_minutes=60,
            note=None,
        )
        service = AlertService(self.db)
        states = [
            MarketState(
                market_hash_name="AK-47 | Redline (Field-Tested)",
                name_cn="AK-47 | 火神测试",
                c5_sell_price=90,
                steam_sell_price=110,
                ratio=90 / (0.869 * 110),
            )
        ]
        baskets = service.build_baskets(states)
        alerts = service.evaluate(states, baskets)
        self.assertEqual(2, len(alerts))

    def test_basket_total_is_sum_of_component_prices(self) -> None:
        self.db.add_alert_rule(
            target_type="basket",
            target_key="test-basket",
            metric="basket_total",
            operator="gte",
            threshold=350,
            anchor_value=None,
            cooldown_minutes=60,
            note=None,
        )
        service = AlertService(self.db)
        states = [
            MarketState(
                market_hash_name="AK-47 | Redline (Field-Tested)",
                name_cn="AK-47 | 火神测试",
                c5_sell_price=100,
            ),
            MarketState(
                market_hash_name="AWP | Asiimov (Battle-Scarred)",
                name_cn="AWP | 二西莫夫测试",
                c5_sell_price=150,
            ),
        ]
        baskets = service.build_baskets(states)
        self.assertEqual(400.0, baskets[0].total_value)
        alerts = service.evaluate(states, baskets)
        self.assertEqual(1, len(alerts))


if __name__ == "__main__":
    unittest.main()
