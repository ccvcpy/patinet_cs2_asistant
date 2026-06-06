from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.catalog import is_csgo_api_weapon_case, load_csgo_api_catalog
from cs2_assistant.db import Database
from cs2_assistant.models import CatalogItem


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class CsgoApiCatalogTestCase(unittest.TestCase):
    def test_loads_csgo_api_categories_and_counts_weapon_cases(self) -> None:
        def fake_get(url: str, **_: object) -> FakeResponse:
            if url.endswith("/zh-CN/crates.json"):
                return FakeResponse(
                    [
                        {
                            "id": "crate-4904",
                            "name": "千瓦武器箱",
                            "market_hash_name": "Kilowatt Case",
                            "type": "Case",
                        },
                        {
                            "id": "crate-sticker",
                            "name": "印花胶囊",
                            "market_hash_name": "Sticker Capsule",
                            "type": "Sticker Capsule",
                        },
                    ]
                )
            if url.endswith("/zh-CN/keychains.json"):
                return FakeResponse(
                    [
                        {
                            "id": "keychain-1",
                            "name": "挂件 | 小道长",
                            "market_hash_name": "Charm | Lil' Zen",
                        }
                    ]
                )
            raise AssertionError(f"unexpected url: {url}")

        with patch("cs2_assistant.catalog.requests.get", side_effect=fake_get):
            result = load_csgo_api_catalog(categories=["crates", "keychains"])

        by_name = {item.market_hash_name: item for item in result.items}
        self.assertEqual(3, len(result.items))
        self.assertEqual({"crates": 2, "keychains": 1}, result.category_counts)
        self.assertEqual(2, result.weapon_case_count)
        self.assertTrue(is_csgo_api_weapon_case(by_name["Kilowatt Case"].raw_json))
        self.assertTrue(is_csgo_api_weapon_case(by_name["Sticker Capsule"].raw_json))
        self.assertEqual("挂件 | 小道长", by_name["Charm | Lil' Zen"].name_cn)

    def test_csgo_api_upsert_preserves_existing_platform_ids(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            db = Database(Path(temp_dir.name) / "assistant.db")
            db.initialize()
            db.upsert_items(
                [
                    CatalogItem(
                        market_hash_name="Kilowatt Case",
                        name_cn="Kilowatt Case",
                        c5_item_id="c5-123",
                        steam_item_id="steam-456",
                        raw_json={"source": "steamdt"},
                    )
                ]
            )
            db.upsert_items(
                [
                    CatalogItem(
                        market_hash_name="Kilowatt Case",
                        name_cn="千瓦武器箱",
                        raw_json={
                            "market_hash_name": "Kilowatt Case",
                            "type": "Case",
                            "csgoApi": {
                                "source": "ByMykel/CSGO-API",
                                "category": "crates",
                                "categories": ["crates"],
                            },
                        },
                    )
                ],
                preserve_existing_ids=True,
            )

            row = db.get_item("Kilowatt Case")
            assert row is not None
            self.assertEqual("c5-123", row["c5_item_id"])
            self.assertEqual("steam-456", row["steam_item_id"])
            self.assertEqual("千瓦武器箱", row["name_cn"])
            raw_json = json.loads(row["raw_json"])
            self.assertEqual("ByMykel/CSGO-API", raw_json["csgoApi"]["source"])
        finally:
            db.close()
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
