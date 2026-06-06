from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.config import apply_proxy_mode, normalize_proxy_mode


class ProxyModeTestCase(unittest.TestCase):
    def test_normalize_proxy_mode_accepts_aliases(self) -> None:
        self.assertEqual("system", normalize_proxy_mode("system"))
        self.assertEqual("system", normalize_proxy_mode("proxy"))
        self.assertEqual("none", normalize_proxy_mode("none"))
        self.assertEqual("none", normalize_proxy_mode("direct"))

    def test_apply_none_proxy_mode_disables_environment_proxy(self) -> None:
        original = dict(os.environ)
        try:
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
            os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
            mode = apply_proxy_mode("none")
            self.assertEqual("none", mode)
            self.assertNotIn("HTTP_PROXY", os.environ)
            self.assertNotIn("HTTPS_PROXY", os.environ)
            self.assertEqual("*", os.environ["NO_PROXY"])
        finally:
            os.environ.clear()
            os.environ.update(original)


if __name__ == "__main__":
    unittest.main()
