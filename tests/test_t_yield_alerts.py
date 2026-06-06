from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.services.t_yield_alerts import build_t_yield_notification


class TYieldAlertsTestCase(unittest.TestCase):
    def test_notification_includes_candidates_and_missing_price_section(self) -> None:
        message = build_t_yield_notification(
            [
                {
                    "rank": 1,
                    "name": "Rezan The Ready | Sabre",
                    "inventoryStatusSummary": "混合库存 (1 冷却 / 1 可交易)",
                    "tYieldPct": "9.08%",
                    "ratio": "0.9446",
                    "listingRatio": "1.0869",
                    "c5LowestSellPrice": 136.9,
                    "steamLowestSellPrice": 166.78,
                    "steamAccounts": [{"steamId": "1", "nickname": "115"}],
                }
            ],
            top_n=10,
            min_price=50,
            missing_steam_prices=[
                {
                    "name": "Fever Case",
                    "inventoryStatusSummary": "冷却库存 (1 冷却 / 0 可交易)",
                    "marketHashName": "Fever Case",
                    "c5SellPrice": 4.64,
                }
            ],
        )
        self.assertIn("做T收益提醒", message.body)
        self.assertIn("公式: 折算比=C5/Steam", message.body)
        self.assertIn("挂刀比 1.0869", message.body)
        self.assertIn("Rezan The Ready | Sabre", message.body)
        self.assertIn("混合库存", message.body)
        self.assertIn("缺少 Steam 价格", message.body)
        self.assertIn("Fever Case", message.body)
        self.assertIn("账号 115", message.body)


if __name__ == "__main__":
    unittest.main()
