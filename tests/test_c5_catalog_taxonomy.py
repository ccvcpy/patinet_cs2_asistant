from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from cs2_assistant.config import Settings
from cs2_assistant.services.c5_catalog_taxonomy import (
    build_c5_catalog_taxonomy,
    estimate_c5_catalog_filter,
    filter_c5_catalog_items,
)


WEAR_FN = "SFUI_InvTooltip_Wear_Amount_0"
WEAR_MW = "SFUI_InvTooltip_Wear_Amount_1"
WEAR_FT = "SFUI_InvTooltip_Wear_Amount_2"
WEAR_WW = "SFUI_InvTooltip_Wear_Amount_3"
WEAR_BS = "SFUI_InvTooltip_Wear_Amount_4"


class C5CatalogTaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name) / "assistant.db"
        self.settings = Settings(db_path=self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE items (
                    market_hash_name TEXT PRIMARY KEY,
                    name_cn TEXT NOT NULL,
                    c5_item_id TEXT,
                    steam_item_id TEXT,
                    raw_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE pool_operations (
                    id INTEGER PRIMARY KEY,
                    note TEXT NOT NULL
                );
                INSERT INTO pool_operations (id, note) VALUES (1, 'must-stay-unchanged');
                """
            )

    def _insert(
        self,
        market_hash_name: str,
        name_cn: str,
        raw: dict[str, Any],
    ) -> None:
        payload = dict(raw)
        payload.setdefault("market_hash_name", market_hash_name)
        payload.setdefault("name", name_cn)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO items (
                    market_hash_name, name_cn, raw_json, imported_at, updated_at
                ) VALUES (?, ?, ?, '2026-08-04T00:00:00Z', '2026-08-04T00:00:00Z')
                """,
                (market_hash_name, name_cn, json.dumps(payload, ensure_ascii=False)),
            )

    @staticmethod
    def _skin(
        *,
        wear_id: str | None,
        wear_name: str | None = None,
        rarity_id: str = "rarity_mythical_weapon",
        rarity_name: str = "受限级",
        stattrak: bool = False,
        souvenir: bool = False,
        phase: str | None = None,
        min_float: float = 0.0,
        max_float: float = 1.0,
        raw_type: str | None = None,
    ) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "csgoApi": {
                "source": "ByMykel/CSGO-API",
                "category": "skins_not_grouped",
                "categories": ["skins_not_grouped"],
            },
            "category": {
                "id": "csgo_inventory_weapon_category_rifles",
                "name": "步枪",
            },
            "weapon": {"id": "weapon_ak47", "name": "AK-47"},
            "rarity": {
                "id": rarity_id,
                "name": rarity_name,
                "color": "#8847ff",
            },
            "wear": (
                {"id": wear_id, "name": wear_name or wear_id}
                if wear_id is not None
                else None
            ),
            "stattrak": stattrak,
            "souvenir": souvenir,
            "phase": phase,
            "min_float": min_float,
            "max_float": max_float,
            "image": "https://example.invalid/item.png",
        }
        if raw_type is not None:
            raw["type"] = raw_type
        return raw

    def test_builds_dynamic_layered_taxonomy_from_raw_catalog(self) -> None:
        self._insert(
            "AK-47 | Test (Minimal Wear)",
            "AK-47 | 测试（略有磨损）",
            self._skin(wear_id=WEAR_MW),
        )
        self._insert(
            "Test Case",
            "测试武器箱",
            {
                "csgoApi": {
                    "source": "ByMykel/CSGO-API",
                    "category": "crates",
                    "categories": ["crates"],
                },
                "type": "Case",
                "rarity": {"id": "rarity_common", "name": "普通级", "color": "#b0c3d9"},
            },
        )

        taxonomy = build_c5_catalog_taxonomy(self.settings)

        self.assertEqual(2, taxonomy["totalCount"])
        categories = {option["id"]: option for option in taxonomy["categories"]}
        self.assertEqual({"crates", "skins_not_grouped"}, set(categories))
        skin_subtype = categories["skins_not_grouped"]["subtypes"][0]
        self.assertEqual("csgo_inventory_weapon_category_rifles", skin_subtype["id"])
        self.assertEqual("weapon_ak47", skin_subtype["weapons"][0]["id"])
        self.assertEqual("Case", categories["crates"]["subtypes"][0]["id"])

        self._insert(
            "Sticker | Dynamic",
            "印花 | 动态新增",
            {
                "csgoApi": {"category": "stickers", "categories": ["stickers"]},
                "type": "Team",
                "rarity": {"id": "rarity_rare", "name": "高级"},
            },
        )
        refreshed = build_c5_catalog_taxonomy(self.settings)
        self.assertEqual(3, refreshed["totalCount"])
        self.assertIn("stickers", {option["id"] for option in refreshed["categories"]})

    def test_subtype_prefers_category_id_over_original_type(self) -> None:
        raw = self._skin(wear_id=WEAR_FN, raw_type="Must Not Win")
        self._insert("AK-47 | Layered (Factory New)", "AK-47 | 分层（崭新出厂）", raw)

        row = filter_c5_catalog_items(self.settings, {})[0]

        self.assertEqual("skins_not_grouped", row["categoryId"])
        self.assertEqual("csgo_inventory_weapon_category_rifles", row["subtypeId"])

    def test_wear_order_none_bucket_and_no_variant_fabrication(self) -> None:
        wear_rows = [
            (WEAR_FN, "Factory New"),
            (WEAR_MW, "Minimal Wear"),
            (WEAR_FT, "Field-Tested"),
            (WEAR_WW, "Well-Worn"),
            (WEAR_BS, "Battle-Scarred"),
        ]
        for index, (wear_id, wear_name) in enumerate(wear_rows):
            self._insert(
                f"AK-47 | Wear {index}",
                f"AK-47 | 磨损 {index}",
                self._skin(wear_id=wear_id, wear_name=wear_name),
            )
        self._insert(
            "Grouped Skin Without Current Wear",
            "没有当前磨损的分组皮肤",
            {
                "csgoApi": {"category": "skins", "categories": ["skins"]},
                "wear": None,
                "wears": [{"id": wear_id, "name": wear_name} for wear_id, wear_name in wear_rows],
                "min_float": 0.0,
                "max_float": 1.0,
            },
        )

        taxonomy = build_c5_catalog_taxonomy(self.settings)
        self.assertEqual(
            [WEAR_FN, WEAR_MW, WEAR_FT, WEAR_WW, WEAR_BS, "__none__"],
            [option["id"] for option in taxonomy["wears"]],
        )
        self.assertEqual(
            ["崭新出厂", "略有磨损", "久经沙场", "破损不堪", "战痕累累", "无磨损"],
            [option["name"] for option in taxonomy["wears"]],
        )
        no_wear = filter_c5_catalog_items(self.settings, {"wearIds": ["__none__"]})
        self.assertEqual(["Grouped Skin Without Current Wear"], [row["marketHashName"] for row in no_wear])
        self.assertIsNone(no_wear[0]["minFloat"])
        self.assertEqual([], filter_c5_catalog_items(self.settings, {"wearIds": ["missing-wear"]}))

    def test_rarity_is_keyed_by_id_even_when_names_match_and_supports_none(self) -> None:
        self._insert(
            "AK-47 | Same Name (Factory New)",
            "AK-47 | 同名（崭新出厂）",
            self._skin(
                wear_id=WEAR_FN,
                rarity_id="rarity_legendary_weapon",
                rarity_name="同名品质",
            ),
        )
        self._insert(
            "Agent | Same Name",
            "探员 | 同名",
            {
                "csgoApi": {"category": "agents", "categories": ["agents"]},
                "rarity": {
                    "id": "rarity_legendary_character",
                    "name": "同名品质",
                    "color": "#d32ce6",
                },
            },
        )
        self._insert(
            "Test Case Key",
            "测试武器箱钥匙",
            {"csgoApi": {"category": "keys", "categories": ["keys"]}},
        )

        taxonomy = build_c5_catalog_taxonomy(self.settings)
        rarity_ids = {option["id"] for option in taxonomy["rarities"]}
        self.assertEqual(
            {"rarity_legendary_weapon", "rarity_legendary_character", "__none__"},
            rarity_ids,
        )
        weapon_rows = filter_c5_catalog_items(
            self.settings,
            {"rarityIds": ["rarity_legendary_weapon"]},
        )
        self.assertEqual(["AK-47 | Same Name (Factory New)"], [row["marketHashName"] for row in weapon_rows])
        no_rarity_rows = filter_c5_catalog_items(self.settings, {"rarityIds": ["__none__"]})
        self.assertEqual(["Test Case Key"], [row["marketHashName"] for row in no_rarity_rows])
        self.assertEqual("无品质", no_rarity_rows[0]["rarityName"])

    def test_non_skin_uses_raw_type_and_has_no_wear_or_float(self) -> None:
        self._insert(
            "Sticker | Test",
            "印花 | 测试",
            {
                "csgoApi": {"category": "stickers", "categories": ["stickers"]},
                "type": "Team",
                "rarity": {"id": "rarity_rare", "name": "高级", "color": "#4b69ff"},
                "image": "https://example.invalid/sticker.png",
            },
        )

        row = filter_c5_catalog_items(self.settings, {"subtypeIds": ["Team"]})[0]

        self.assertEqual("stickers", row["categoryId"])
        self.assertEqual("Team", row["subtypeId"])
        self.assertIsNone(row["weaponId"])
        self.assertEqual("normal", row["version"])
        self.assertEqual("__none__", row["wearId"])
        self.assertIsNone(row["minFloat"])
        self.assertIsNone(row["maxFloat"])
        self.assertEqual([], filter_c5_catalog_items(self.settings, {"floatMin": 0.0}))

    def test_versions_phase_keyword_and_combined_facets(self) -> None:
        self._insert(
            "AK-47 | Normal (Factory New)",
            "AK-47 | 普通（崭新出厂）",
            self._skin(wear_id=WEAR_FN),
        )
        self._insert(
            "StatTrak™ AK-47 | Doppler (Factory New)",
            "StatTrak™ AK-47 | 多普勒（崭新出厂）",
            self._skin(wear_id=WEAR_FN, stattrak=True, phase="Phase 2"),
        )
        self._insert(
            "Souvenir AK-47 | Test (Field-Tested)",
            "纪念品 AK-47 | 测试（久经沙场）",
            self._skin(wear_id=WEAR_FT, souvenir=True),
        )
        self._insert(
            "StatTrak™ Music Kit | Dynamic",
            "StatTrak™ 音乐盒 | 动态",
            {"csgoApi": {"category": "music_kits", "categories": ["music_kits"]}},
        )

        taxonomy = build_c5_catalog_taxonomy(self.settings)
        version_counts = {option["id"]: option["count"] for option in taxonomy["versions"]}
        self.assertEqual({"normal": 1, "stattrak": 2, "souvenir": 1}, version_counts)

        rows = filter_c5_catalog_items(
            self.settings,
            {
                "categoryIds": ["skins_not_grouped"],
                "subtypeIds": ["csgo_inventory_weapon_category_rifles"],
                "weaponIds": ["weapon_ak47"],
                "rarityIds": ["rarity_mythical_weapon"],
                "versions": ["stattrak"],
                "wearIds": [WEAR_FN],
                "phases": ["Phase 2"],
                "keyword": "多普勒",
            },
        )
        self.assertEqual(["StatTrak™ AK-47 | Doppler (Factory New)"], [row["marketHashName"] for row in rows])

    def test_float_filter_uses_real_wear_intersection_and_exact_boundaries(self) -> None:
        self._insert(
            "AK-47 | Restricted (Minimal Wear)",
            "AK-47 | 受限范围（略有磨损）",
            self._skin(wear_id=WEAR_MW, min_float=0.10, max_float=0.70),
        )
        self._insert(
            "AK-47 | Restricted (Field-Tested)",
            "AK-47 | 受限范围（久经沙场）",
            self._skin(wear_id=WEAR_FT, min_float=0.10, max_float=0.70),
        )

        all_rows = {row["wearId"]: row for row in filter_c5_catalog_items(self.settings, {})}
        self.assertEqual(0.10, all_rows[WEAR_MW]["minFloat"])
        self.assertEqual(0.15, all_rows[WEAR_MW]["maxFloat"])
        self.assertEqual(0.15, all_rows[WEAR_FT]["minFloat"])
        self.assertEqual(0.38, all_rows[WEAR_FT]["maxFloat"])

        at_boundary = filter_c5_catalog_items(
            self.settings,
            {"floatMin": 0.15, "floatMax": 0.15},
        )
        self.assertEqual([WEAR_FT], [row["wearId"] for row in at_boundary])
        inside_minimal_wear = filter_c5_catalog_items(
            self.settings,
            {"floatMin": 0.149, "floatMax": 0.149},
        )
        self.assertEqual([WEAR_MW], [row["wearId"] for row in inside_minimal_wear])
        self.assertEqual([], filter_c5_catalog_items(self.settings, {"wearIds": [WEAR_BS]}))

    def test_estimate_is_dynamic_and_does_not_touch_transaction_tables(self) -> None:
        self._insert(
            "Test Case",
            "测试武器箱",
            {"csgoApi": {"category": "crates", "categories": ["crates"]}, "type": "Case"},
        )
        self._insert(
            "Sticker | Test",
            "印花 | 测试",
            {"csgoApi": {"category": "stickers", "categories": ["stickers"]}, "type": "Team"},
        )

        estimate = estimate_c5_catalog_filter(self.settings, {"categoryIds": ["crates"]})

        self.assertEqual(2, estimate["totalCatalogCount"])
        self.assertEqual(1, estimate["catalogMatchedCount"])
        self.assertEqual(1, estimate["requiresC5PriceCount"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                [(1, "must-stay-unchanged")],
                connection.execute("SELECT id, note FROM pool_operations").fetchall(),
            )


if __name__ == "__main__":
    unittest.main()
