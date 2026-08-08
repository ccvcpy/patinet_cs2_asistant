export const C5_RESEARCH_NO_WEAR_ID = "__none__";
function asRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
        ? value
        : {};
}
function firstPresent(record, keys) {
    for (const key of keys) {
        if (record[key] !== undefined && record[key] !== null)
            return record[key];
    }
    return undefined;
}
function firstText(record, keys) {
    const value = firstPresent(record, keys);
    return value === undefined ? "" : String(value).trim();
}
function textArray(record, arrayKeys, scalarKeys = []) {
    const values = [];
    for (const key of arrayKeys) {
        const value = record[key];
        if (Array.isArray(value)) {
            values.push(...value.map((entry) => String(entry ?? "").trim()).filter(Boolean));
        }
    }
    for (const key of scalarKeys) {
        const value = record[key];
        if (value !== undefined && value !== null && String(value).trim()) {
            values.push(String(value).trim());
        }
    }
    return [...new Set(values)];
}
function optionArray(record, keys) {
    for (const key of keys) {
        if (Array.isArray(record[key]))
            return record[key];
    }
    return [];
}
function normalizeTaxonomyOption(value, context = {}) {
    if (typeof value === "string" || typeof value === "number") {
        const text = String(value).trim();
        if (!text)
            return null;
        return {
            id: text,
            name: text,
            itemClassIds: context.itemClassIds || [],
            subtypeIds: context.subtypeIds || [],
            parentIds: [],
        };
    }
    const record = asRecord(value);
    const id = firstText(record, ["id", "value", "key", "code"]);
    const name = firstText(record, ["name", "label", "nameCn", "name_cn", "displayName"]);
    if (!id || !name)
        return null;
    const count = finiteNumber(firstPresent(record, ["count", "itemCount", "total"]));
    const supportsWearValue = firstPresent(record, ["supportsWear", "supports_wear", "hasWear"]);
    return {
        id,
        name,
        count: count === null ? undefined : Math.max(0, Math.round(count)),
        color: firstText(record, ["color", "hexColor", "hex_color"]) || null,
        itemClassIds: [...new Set([
                ...(context.itemClassIds || []),
                ...textArray(record, ["itemClassIds", "item_class_ids", "categoryIds", "category_ids", "itemTypeIds"], ["itemClassId", "item_class_id", "categoryId", "category_id", "itemTypeId"]),
            ])],
        subtypeIds: [...new Set([
                ...(context.subtypeIds || []),
                ...textArray(record, ["subtypeIds", "subtype_ids", "subCategoryIds", "sub_category_ids"], ["subtypeId", "subtype_id", "subCategoryId", "sub_category_id"]),
            ])],
        parentIds: textArray(record, ["parentIds", "parent_ids"], ["parentId", "parent_id"]),
        supportsWear: typeof supportsWearValue === "boolean" ? supportsWearValue : undefined,
    };
}
function mergeTaxonomyOptions(options) {
    const merged = new Map();
    for (const option of options) {
        if (!option)
            continue;
        const current = merged.get(option.id);
        if (!current) {
            merged.set(option.id, option);
            continue;
        }
        merged.set(option.id, {
            ...current,
            ...option,
            count: option.count ?? current.count,
            color: option.color || current.color,
            itemClassIds: [...new Set([...current.itemClassIds, ...option.itemClassIds])],
            subtypeIds: [...new Set([...current.subtypeIds, ...option.subtypeIds])],
            parentIds: [...new Set([...current.parentIds, ...option.parentIds])],
            supportsWear: option.supportsWear ?? current.supportsWear,
        });
    }
    return [...merged.values()].sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
}
function normalizeCollection(values, relation = null) {
    return mergeTaxonomyOptions(values.map((value) => {
        const option = normalizeTaxonomyOption(value);
        if (!option || !option.parentIds.length || !relation)
            return option;
        return {
            ...option,
            itemClassIds: relation === "itemClass"
                ? [...new Set([...option.itemClassIds, ...option.parentIds])]
                : option.itemClassIds,
            subtypeIds: relation === "subtype"
                ? [...new Set([...option.subtypeIds, ...option.parentIds])]
                : option.subtypeIds,
        };
    }));
}
function nestedOptions(parents, keys, relation) {
    const options = [];
    for (const parent of parents) {
        const parentRecord = asRecord(parent);
        const parentOption = normalizeTaxonomyOption(parent);
        if (!parentOption)
            continue;
        for (const child of optionArray(parentRecord, keys)) {
            options.push(normalizeTaxonomyOption(child, relation === "itemClass"
                ? { itemClassIds: [parentOption.id] }
                : {
                    itemClassIds: parentOption.itemClassIds.length
                        ? parentOption.itemClassIds
                        : parentOption.parentIds,
                    subtypeIds: [parentOption.id],
                }));
        }
    }
    return mergeTaxonomyOptions(options);
}
export function isNoWearOption(option) {
    const key = `${option.id} ${option.name}`.toLocaleLowerCase("zh-CN");
    return option.id === C5_RESEARCH_NO_WEAR_ID
        || key.includes("无磨损")
        || key.includes("no wear")
        || key.includes("no_wear")
        || key.includes("not applicable");
}
export function normalizeC5ResearchTaxonomy(payload) {
    const envelope = asRecord(payload);
    const data = asRecord(envelope.data);
    const nestedTaxonomy = asRecord(envelope.taxonomy);
    const dataTaxonomy = asRecord(data.taxonomy);
    const root = Object.keys(nestedTaxonomy).length
        ? nestedTaxonomy
        : Object.keys(dataTaxonomy).length
            ? dataTaxonomy
            : Object.keys(data).length
                ? data
                : envelope;
    const itemClassRows = optionArray(root, ["categories", "itemClasses", "item_classes", "itemTypes"]);
    const subtypeRows = optionArray(root, ["subtypes", "subTypes", "subcategories", "subCategories"]);
    const nestedSubtypeRows = itemClassRows.flatMap((itemClass) => optionArray(asRecord(itemClass), ["subtypes", "subTypes", "children", "subcategories", "subCategories"]));
    const itemClasses = normalizeCollection(itemClassRows);
    const subtypes = mergeTaxonomyOptions([
        ...normalizeCollection(subtypeRows, "itemClass"),
        ...nestedOptions(itemClassRows, ["subtypes", "subTypes", "children", "subcategories", "subCategories"], "itemClass"),
    ]);
    const weapons = mergeTaxonomyOptions([
        ...normalizeCollection(optionArray(root, ["weapons", "weaponModels", "weapon_models"]), "subtype"),
        ...nestedOptions(itemClassRows, ["weapons", "weaponModels", "weapon_models"], "itemClass"),
        ...nestedOptions([...subtypeRows, ...nestedSubtypeRows], ["weapons", "weaponModels", "weapon_models"], "subtype"),
    ]);
    const rarities = mergeTaxonomyOptions([
        ...normalizeCollection(optionArray(root, ["rarities", "rarity"]), "itemClass"),
        ...nestedOptions(itemClassRows, ["rarities", "rarity"], "itemClass"),
    ]);
    const versions = mergeTaxonomyOptions([
        ...normalizeCollection(optionArray(root, ["versions", "variants"]), "itemClass"),
        ...nestedOptions(itemClassRows, ["versions", "variants"], "itemClass"),
    ]);
    const wears = mergeTaxonomyOptions([
        ...normalizeCollection(optionArray(root, ["wears", "wearGrades", "wear_grades", "exteriors"]), "itemClass"),
        ...nestedOptions(itemClassRows, ["wears", "wearGrades", "wear_grades", "exteriors"], "itemClass"),
    ]);
    const phases = mergeTaxonomyOptions([
        ...normalizeCollection(optionArray(root, ["phases", "phaseVariants", "phase_variants"]), "itemClass"),
        ...nestedOptions(itemClassRows, ["phases", "phaseVariants", "phase_variants"], "itemClass"),
    ]);
    if (!wears.some(isNoWearOption)) {
        wears.push({
            id: C5_RESEARCH_NO_WEAR_ID,
            name: "无磨损",
            itemClassIds: [],
            subtypeIds: [],
            parentIds: [],
        });
    }
    return {
        generatedAt: firstText(root, ["generatedAt", "generated_at", "updatedAt", "updated_at"]) || undefined,
        catalogVersion: firstText(root, ["catalogVersion", "catalog_version", "version"]) || undefined,
        itemClasses,
        subtypes,
        weapons,
        rarities,
        versions,
        wears,
        phases,
    };
}
export function taxonomyOptionsForContext(options, context) {
    return options.filter((option) => {
        if (context.itemClassId
            && option.itemClassIds.length
            && !option.itemClassIds.includes(context.itemClassId))
            return false;
        if (context.subtypeId
            && option.subtypeIds.length
            && !option.subtypeIds.includes(context.subtypeId))
            return false;
        return true;
    });
}
export function itemClassSupportsWear(option) {
    if (!option)
        return null;
    if (typeof option.supportsWear === "boolean")
        return option.supportsWear;
    const key = `${option.id} ${option.name}`.toLocaleLowerCase("zh-CN");
    return key.includes("skin")
        || key.includes("武器皮肤")
        || key.includes("匕首")
        || key.includes("手套");
}
export function wearOptionsForItemClass(wears, itemClass) {
    if (itemClassSupportsWear(itemClass) !== false)
        return wears;
    const noWear = wears.find(isNoWearOption);
    return noWear ? [noWear] : [];
}
function singleton(value) {
    const normalized = String(value || "").trim();
    return normalized ? [normalized] : [];
}
function normalizedRange(minimum, maximum, bounds) {
    const parsedMinimum = finiteNumber(minimum);
    const parsedMaximum = finiteNumber(maximum);
    const bounded = (value) => {
        if (value === null)
            return null;
        const lower = Math.max(bounds.lowerBound, value);
        return bounds.upperBound === null ? lower : Math.min(bounds.upperBound, lower);
    };
    const low = bounded(parsedMinimum);
    const high = bounded(parsedMaximum);
    if (low !== null && high !== null && low > high)
        return { min: high, max: low };
    return { min: low, max: high };
}
export function buildC5ResearchFilterPayload(draft, options = {}) {
    const floatRange = normalizedRange(draft.wearMin, draft.wearMax, {
        lowerBound: 0,
        upperBound: 1,
    });
    const priceRange = normalizedRange(draft.priceMin, draft.priceMax, {
        lowerBound: 0,
        upperBound: null,
    });
    const wearIds = options.supportsWear === false
        ? [C5_RESEARCH_NO_WEAR_ID]
        : singleton(draft.wearId);
    return {
        categoryIds: singleton(draft.itemClassId),
        subtypeIds: singleton(draft.subtypeId),
        weaponIds: singleton(draft.weaponId),
        rarityIds: singleton(draft.rarityId),
        versions: singleton(draft.versionId),
        wearIds,
        phases: singleton(draft.phaseId),
        floatMin: options.supportsWear === false ? null : (floatRange.min ?? 0),
        floatMax: options.supportsWear === false ? null : (floatRange.max ?? 1),
        priceMin: priceRange.min,
        priceMax: priceRange.max,
        keyword: draft.keyword.trim(),
    };
}
export function buildC5ResearchEstimatePayload(filters) {
    const { priceMin: _priceMin, priceMax: _priceMax, ...catalogFilters } = filters;
    return catalogFilters;
}
export function isC5ResearchTerminalStatus(status) {
    return new Set(["completed", "completed_with_errors", "failed", "canceled", "cancelled"])
        .has(String(status || "").trim().toLowerCase());
}
export function finiteNumber(value) {
    const parsed = typeof value === "number" ? value : Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}
export function optionalNumber(value) {
    if (typeof value === "string" && !value.trim())
        return null;
    return finiteNumber(value);
}
export function formatMoney(value, fallback = "—") {
    const number = finiteNumber(value);
    if (number === null)
        return fallback;
    const amount = Math.abs(number).toLocaleString("zh-CN", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
    return `${number < 0 ? "-" : ""}¥${amount}`;
}
export function formatSignedMoney(value, fallback = "—") {
    const number = finiteNumber(value);
    if (number === null)
        return fallback;
    if (number === 0)
        return formatMoney(number);
    return `${number > 0 ? "+" : ""}${formatMoney(number)}`;
}
export function formatPercent(value, fallback = "—") {
    const number = finiteNumber(value);
    if (number === null)
        return fallback;
    return `${(number * 100).toFixed(2)}%`;
}
export function formatCount(value) {
    const number = finiteNumber(value);
    return number === null ? "0" : Math.round(number).toLocaleString("zh-CN");
}
export function compactRarityName(value) {
    const name = String(value || "").trim();
    if (!name)
        return "未知";
    return name.endsWith("级") ? name.slice(0, -1) : name;
}
export function qualityTone(item) {
    const name = compactRarityName(item.rarityName);
    if (name.includes("隐秘") || name.includes("违禁"))
        return "covert";
    if (name.includes("保密") || name.includes("非凡"))
        return "classified";
    if (name.includes("受限") || name.includes("卓越"))
        return "restricted";
    return "unknown";
}
export function inventoryAdvice(item) {
    if (item.inventoryAdvice)
        return item.inventoryAdvice;
    const current = finiteNumber(item.expectedRoi);
    const average = finiteNumber(item.averageRoi7d);
    const positiveShare = finiteNumber(item.positiveRoiShare7d) || 0;
    const sampleCount = Math.max(0, Number(item.validObservationCount7d || 0));
    if (current !== null
        && current > 0
        && average !== null
        && average > 0
        && positiveShare >= 0.8
        && sampleCount >= 12
        && !item.lastError) {
        return "ready";
    }
    if (current !== null
        && !item.lastError
        && ((average !== null && average > 0) || current > 0)) {
        return "watch";
    }
    return "avoid";
}
export function adviceLabel(item) {
    if (item.inventoryAdviceLabel)
        return item.inventoryAdviceLabel;
    return { ready: "可入库", watch: "观望", avoid: "不建议" }[inventoryAdvice(item)];
}
export function recommendationTone(item) {
    if (item.recommendationTone)
        return item.recommendationTone;
    const advice = inventoryAdvice(item);
    if (advice === "ready")
        return "stable";
    if (advice === "watch")
        return "observe";
    return "avoid";
}
export function recommendationLabel(item) {
    if (item.recommendationLabel)
        return item.recommendationLabel;
    const tone = recommendationTone(item);
    if (tone === "stable")
        return "稳定推荐";
    if (tone === "observe") {
        return (finiteNumber(item.expectedRoi) || 0) > 0 ? "继续观察" : "等待回落";
    }
    return "暂不建议";
}
function overlapsRange(minimum, maximum, filterMinimum, filterMaximum) {
    if (minimum === null && maximum === null)
        return true;
    const low = minimum ?? maximum ?? 0;
    const high = maximum ?? minimum ?? 1;
    if (filterMinimum !== null && high < filterMinimum)
        return false;
    if (filterMaximum !== null && low > filterMaximum)
        return false;
    return true;
}
export function filterSelectionItems(items, filters) {
    const keyword = filters.keyword.trim().toLocaleLowerCase("zh-CN");
    return items.filter((item) => {
        if (filters.itemType && item.itemType !== filters.itemType)
            return false;
        if (filters.quality && compactRarityName(item.rarityName) !== filters.quality)
            return false;
        if (!overlapsRange(finiteNumber(item.minFloat), finiteNumber(item.maxFloat), filters.wearMin, filters.wearMax))
            return false;
        const price = finiteNumber(item.c5ListingPrice);
        if (filters.priceMin !== null && (price === null || price < filters.priceMin))
            return false;
        if (filters.priceMax !== null && (price === null || price > filters.priceMax))
            return false;
        if (keyword) {
            const haystack = `${item.name || ""} ${item.marketHashName || ""} ${item.weaponName || ""}`.toLocaleLowerCase("zh-CN");
            if (!haystack.includes(keyword))
                return false;
        }
        return true;
    });
}
export function sortSelectionItems(items) {
    const weight = { ready: 3, watch: 2, avoid: 1 };
    return [...items].sort((left, right) => {
        const adviceDifference = weight[inventoryAdvice(right)] - weight[inventoryAdvice(left)];
        if (adviceDifference)
            return adviceDifference;
        const positiveDifference = Number((finiteNumber(right.expectedRoi) || 0) > 0)
            - Number((finiteNumber(left.expectedRoi) || 0) > 0);
        if (positiveDifference)
            return positiveDifference;
        const shareDifference = (finiteNumber(right.positiveRoiShare7d) || 0)
            - (finiteNumber(left.positiveRoiShare7d) || 0);
        if (shareDifference)
            return shareDifference;
        return (finiteNumber(right.expectedRoi) || Number.NEGATIVE_INFINITY)
            - (finiteNumber(left.expectedRoi) || Number.NEGATIVE_INFINITY);
    });
}
export function formatDuration(seconds) {
    const total = Math.max(0, Math.round(finiteNumber(seconds) || 0));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days > 0)
        return `${days}d${hours}h`;
    if (hours > 0)
        return `${hours}h${minutes}m`;
    return `${minutes}m`;
}
export function historyWindowStart(days) {
    return new Date(Date.now() - Math.max(1, days) * 86400000).toISOString();
}
