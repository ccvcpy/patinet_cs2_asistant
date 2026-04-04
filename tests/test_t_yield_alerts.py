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
                    "inventoryStatusSummary": "同时有冷却和不冷却 (1 冷却 / 1 不冷却)",
                    "tYieldPct": "9.08%",
                    "ratio": "0.9446",
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
                    "inventoryStatusSummary": "仅冷却 (1 冷却 / 0 不冷却)",
                    "marketHashName": "Fever Case",
                    "c5SellPrice": 4.64,
                }
            ],
        )
        self.assertIn("做T收益率提醒", message.body)
        self.assertIn("Rezan The Ready | Sabre", message.body)
        self.assertIn("同时有冷却和不冷却", message.body)
        self.assertIn("缺少 Steam 价格", message.body)
        self.assertIn("Fever Case", message.body)
        self.assertIn("账号 115", message.body)


if __name__ == "__main__":
    unittest.main()
