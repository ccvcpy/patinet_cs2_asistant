import assert from "node:assert/strict";
import test from "node:test";

import {
  C5_RESEARCH_NO_WEAR_ID,
  adviceLabel,
  buildC5ResearchEstimatePayload,
  buildC5ResearchFilterPayload,
  compactRarityName,
  filterSelectionItems,
  inventoryAdvice,
  isC5ResearchTerminalStatus,
  itemClassSupportsWear,
  normalizeC5ResearchTaxonomy,
  optionalNumber,
  qualityTone,
  recommendationLabel,
  sortSelectionItems,
  taxonomyOptionsForContext,
  wearOptionsForItemClass,
} from "../src/pages/c5_t_monitor_shared.js";

test("blank optional numeric filters remain unset", () => {
  assert.equal(optionalNumber(""), null);
  assert.equal(optionalNumber("   "), null);
  assert.equal(optionalNumber("0"), 0);
  assert.equal(optionalNumber("12.50"), 12.5);
});

const stable = {
  marketHashName: "USP-S | Cortex (Factory New)",
  name: "USP消音版 | 脑洞大开（崭新出厂）",
  itemType: "手枪",
  rarityName: "隐秘级",
  wearName: "崭新出厂",
  minFloat: 0,
  maxFloat: 0.07,
  c5ListingPrice: 267.8,
  expectedRoi: 0.0137,
  averageRoi7d: 0.0084,
  positiveRoiShare7d: 1,
  validObservationCount7d: 372,
};

test("quality and inventory advice use the approved visible labels", () => {
  assert.equal(compactRarityName(stable.rarityName), "隐秘");
  assert.equal(qualityTone(stable), "covert");
  assert.equal(inventoryAdvice(stable), "ready");
  assert.equal(adviceLabel(stable), "可入库");
  assert.equal(recommendationLabel(stable), "稳定推荐");
});

test("complete filter set narrows by type quality wear price and keyword", () => {
  const rows = [
    stable,
    {
      ...stable,
      marketHashName: "M4A1-S | Solitude (Factory New)",
      name: "M4A1消音版 | 幽独（崭新出厂）",
      itemType: "步枪",
      rarityName: "保密级",
      c5ListingPrice: 126.19,
    },
  ];
  const result = filterSelectionItems(rows, {
    itemType: "手枪",
    quality: "隐秘",
    wearMin: 0,
    wearMax: 0.1,
    priceMin: 200,
    priceMax: 300,
    keyword: "Cortex",
  });
  assert.deepEqual(result.map((item) => item.marketHashName), [stable.marketHashName]);
});

test("stable recommendations sort ahead of watch and avoid rows", () => {
  const watch = {
    ...stable,
    marketHashName: "watch",
    expectedRoi: -0.002,
    averageRoi7d: 0.01,
  };
  const avoid = {
    ...stable,
    marketHashName: "avoid",
    expectedRoi: -0.02,
    averageRoi7d: -0.01,
    positiveRoiShare7d: 0.2,
  };
  assert.deepEqual(
    sortSelectionItems([avoid, watch, stable]).map((item) => item.marketHashName),
    [stable.marketHashName, "watch", "avoid"],
  );
});

test("taxonomy normalization keeps stable IDs, hierarchy, rarity families and all wear grades", () => {
  const taxonomy = normalizeC5ResearchTaxonomy({
    catalogVersion: "2026-08-04",
    categories: [
      {
        id: "skins_not_grouped",
        name: "武器皮肤",
        supportsWear: true,
        subtypes: [{
          id: "rifle",
          name: "步枪",
          weapons: [{ id: "weapon_ak47", name: "AK-47" }],
        }],
        rarities: [{ id: "rarity_covert_weapon", name: "隐秘级" }],
      },
      {
        id: "crates",
        name: "武器箱/容器",
        supportsWear: false,
        rarities: [{ id: "rarity_covert_container", name: "隐秘级" }],
      },
    ],
    wears: [
      { id: "wear_factory_new", name: "崭新出厂" },
      { id: "wear_minimal_wear", name: "略有磨损" },
      { id: "wear_field_tested", name: "久经沙场" },
      { id: "wear_well_worn", name: "破损不堪" },
      { id: "wear_battle_scarred", name: "战痕累累" },
    ],
  });

  assert.equal(taxonomy.catalogVersion, "2026-08-04");
  assert.deepEqual(taxonomy.itemClasses.map((item) => item.id).sort(), ["crates", "skins_not_grouped"]);
  assert.deepEqual(taxonomy.subtypes[0].itemClassIds, ["skins_not_grouped"]);
  assert.deepEqual(taxonomy.weapons[0].subtypeIds, ["rifle"]);
  assert.deepEqual(
    taxonomy.rarities.map((item) => item.id).sort(),
    ["rarity_covert_container", "rarity_covert_weapon"],
  );
  assert.deepEqual(
    taxonomy.wears.map((item) => item.name).sort(),
    ["崭新出厂", "久经沙场", "略有磨损", "破损不堪", "无磨损", "战痕累累"].sort(),
  );
});

test("taxonomy prefers categories and remains compatible with itemClasses", () => {
  const primary = normalizeC5ResearchTaxonomy({
    categories: [{ id: "categories-primary", name: "Primary" }],
    itemClasses: [{ id: "legacy-shadow", name: "Legacy shadow" }],
  });
  const compatible = normalizeC5ResearchTaxonomy({
    itemClasses: [{ id: "legacy-only", name: "Legacy only" }],
  });

  assert.deepEqual(primary.itemClasses.map((item) => item.id), ["categories-primary"]);
  assert.deepEqual(compatible.itemClasses.map((item) => item.id), ["legacy-only"]);
});

test("taxonomy context and non-skin wear linkage do not mix unrelated families", () => {
  const taxonomy = normalizeC5ResearchTaxonomy({
    categories: [
      { id: "skins", name: "武器皮肤", supportsWear: true },
      { id: "crates", name: "武器箱/容器", supportsWear: false },
    ],
    rarities: [
      { id: "weapon_red", name: "隐秘级", categoryIds: ["skins"] },
      { id: "container_red", name: "隐秘级", categoryIds: ["crates"] },
    ],
    wears: [
      { id: "wear_factory_new", name: "崭新出厂" },
      { id: "__none__", name: "无磨损" },
    ],
  });
  const crates = taxonomy.itemClasses.find((item) => item.id === "crates");

  assert.equal(itemClassSupportsWear(crates), false);
  assert.deepEqual(
    taxonomyOptionsForContext(taxonomy.rarities, { itemClassId: "crates" }).map((item) => item.id),
    ["container_red"],
  );
  assert.deepEqual(wearOptionsForItemClass(taxonomy.wears, crates).map((item) => item.name), ["无磨损"]);
});

test("research request submits rarity and five-grade wear by ID and normalizes ranges", () => {
  const payload = buildC5ResearchFilterPayload({
    itemClassId: "skins",
    subtypeId: "rifle",
    weaponId: "weapon_ak47",
    rarityId: "rarity_covert_weapon",
    versionId: "stattrak",
    wearId: "wear_factory_new",
    wearMin: "0.07",
    wearMax: "0.00",
    phaseId: "emerald",
    priceMin: "300",
    priceMax: "100",
    keyword: "  AK-47  ",
  }, { supportsWear: true });

  assert.deepEqual(payload.categoryIds, ["skins"]);
  assert.equal("itemClassIds" in payload, false);
  assert.deepEqual(payload.rarityIds, ["rarity_covert_weapon"]);
  assert.deepEqual(payload.wearIds, ["wear_factory_new"]);
  assert.deepEqual(
    [payload.floatMin, payload.floatMax, payload.priceMin, payload.priceMax],
    [0, 0.07, 100, 300],
  );
  assert.equal("floatRange" in payload, false);
  assert.equal("priceRange" in payload, false);
  assert.deepEqual(payload.versions, ["stattrak"]);
  assert.deepEqual(payload.phases, ["emerald"]);
  assert.equal(payload.keyword, "AK-47");
  const estimatePayload = buildC5ResearchEstimatePayload(payload);
  assert.equal("priceMin" in estimatePayload, false);
  assert.equal("priceMax" in estimatePayload, false);
  assert.deepEqual(estimatePayload.categoryIds, ["skins"]);

  const nonSkin = buildC5ResearchFilterPayload({
    itemClassId: "crates",
    subtypeId: "",
    weaponId: "",
    rarityId: "",
    versionId: "",
    wearId: "",
    wearMin: "0",
    wearMax: "1",
    phaseId: "",
    priceMin: "",
    priceMax: "",
    keyword: "",
  }, { supportsWear: false });
  assert.deepEqual(nonSkin.wearIds, ["__none__"]);
  assert.equal(nonSkin.floatMin, null);
  assert.equal(nonSkin.floatMax, null);
});

test("only real terminal scan states stop polling", () => {
  for (const status of ["completed", "completed_with_errors", "failed", "canceled", "cancelled"]) {
    assert.equal(isC5ResearchTerminalStatus(status), true, status);
  }
  for (const status of ["queued", "running", "paused", "retry"]) {
    assert.equal(isC5ResearchTerminalStatus(status), false, status);
  }
});
