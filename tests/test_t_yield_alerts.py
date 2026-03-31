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
                    "name": "准备就绪的列赞 | 军刀",
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
                    "name": "热潮武器箱",
                    "marketHashName": "Fever Case",
                    "c5SellPrice": 4.64,
                }
            ],
        )
        self.assertIn("做T收益率提醒", message.body)
        self.assertIn("准备就绪的列赞 | 军刀", message.body)
        self.assertIn("价格缺失", message.body)
        self.assertIn("Fever Case", message.body)


if __name__ == "__main__":
    unittest.main()
