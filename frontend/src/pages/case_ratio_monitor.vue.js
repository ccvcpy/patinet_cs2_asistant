import { computed, onMounted, ref } from "vue";
const typeOrder = ["weapon_case", "capsule", "souvenir_package", "container", "crate", "other"];
const loading = ref(true);
const error = ref("");
const report = ref(null);
const keyword = ref("");
const selected = ref("");
const typeFilter = ref("all");
const typeOptions = computed(() => {
    const counts = report.value?.crateTypeCounts ?? {};
    const labels = report.value?.crateTypeLabels ?? {};
    const present = Object.keys(counts).filter((key) => counts[key] > 0);
    const keys = typeOrder.filter((key) => present.includes(key));
    for (const key of present) {
        if (!keys.includes(key))
            keys.push(key);
    }
    return [
        { key: "all", label: "全部", count: report.value?.itemCount ?? 0 },
        ...keys.map((key) => ({ key, label: labels[key] ?? key, count: counts[key] ?? 0 })),
    ];
});
const visibleItems = computed(() => {
    const source = report.value?.items ?? [];
    const needle = keyword.value.trim().toLowerCase();
    return source.filter((item) => {
        const typeMatched = typeFilter.value === "all" || item.crateType === typeFilter.value;
        const textMatched = !needle || `${item.marketHashName} ${item.name} ${item.crateTypeLabel}`.toLowerCase().includes(needle);
        return typeMatched && textMatched;
    });
});
const rankedItems = computed(() => [...visibleItems.value].sort((left, right) => right.recommendationScore - left.recommendationScore ||
    (left.effectiveRecommendedMaxListingRatio ?? left.recommendedMaxListingRatio) -
        (right.effectiveRecommendedMaxListingRatio ?? right.recommendedMaxListingRatio) ||
    (right.steamVolume24h ?? 0) - (left.steamVolume24h ?? 0) ||
    right.coveragePct - left.coveragePct ||
    left.marketHashName.localeCompare(right.marketHashName)));
const selectedItem = computed(() => {
    if (selected.value) {
        const found = visibleItems.value.find((item) => item.marketHashName === selected.value);
        if (found)
            return found;
    }
    return rankedItems.value[0] ?? visibleItems.value[0];
});
const okSnapshotCount = computed(() => report.value?.statusCounts?.ok ?? 0);
const missingSnapshotCount = computed(() => {
    const counts = report.value?.statusCounts ?? {};
    return Object.entries(counts)
        .filter(([key]) => key !== "ok")
        .reduce((sum, [, count]) => sum + count, 0);
});
function formatRatio(value) {
    if (value === null || value === undefined || Number.isNaN(value))
        return "-";
    return value.toFixed(4);
}
function formatMoney(value) {
    if (value === null || value === undefined || Number.isNaN(value))
        return "-";
    const decimals = Math.abs(value) < 1 ? 3 : 2;
    return `CNY ${value.toFixed(decimals)}`;
}
function formatPct(value) {
    if (value === null || value === undefined || Number.isNaN(value))
        return "-";
    return `${value.toFixed(2)}%`;
}
function formatInt(value) {
    if (value === null || value === undefined || Number.isNaN(value))
        return "-";
    return Math.round(value).toLocaleString("zh-CN");
}
function formatTime(value) {
    if (!value)
        return "-";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime()))
        return value;
    return parsed.toLocaleString("zh-CN", { hour12: false });
}
function ratioColor(value) {
    if (value === null || value === undefined || Number.isNaN(value))
        return "#6f7d8c";
    if (value <= 0.7)
        return "#2f7d5b";
    if (value <= 0.75)
        return "#3f7f94";
    if (value <= 0.8)
        return "#a77b2f";
    return "#b5534b";
}
function chooseType(key) {
    typeFilter.value = key;
    selected.value = "";
}
function chooseItem(item) {
    selected.value = item.marketHashName;
}
function timelineStyle(segment) {
    return {
        left: `${Math.max(0, Math.min(100, segment.leftPct))}%`,
        width: `${Math.max(0.25, Math.min(100, segment.widthPct))}%`,
        background: ratioColor(segment.ratio),
    };
}
async function loadReport() {
    loading.value = true;
    error.value = "";
    try {
        const response = await fetch("/guadao_case_ratio_report.json", { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        report.value = (await response.json());
    }
    catch (exc) {
        report.value = null;
        error.value = exc instanceof Error ? exc.message : String(exc);
    }
    finally {
        loading.value = false;
    }
}
onMounted(loadReport);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['case-monitor-page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['case-report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['case-report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['segment-button']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['item-name']} */ ;
/** @type {__VLS_StyleScopedClasses['focus-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['focus-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['case-focus-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['price-source-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['case-focus-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['price-source-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['threshold-card']} */ ;
/** @type {__VLS_StyleScopedClasses['threshold-card']} */ ;
/** @type {__VLS_StyleScopedClasses['case-ratio-table']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-page']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['case-report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['case-focus-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['price-source-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['threshold-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-page']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['case-report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['case-focus-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['price-source-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['threshold-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['bucket-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['case-report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['case-filter-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['segmented-control']} */ ;
/** @type {__VLS_StyleScopedClasses['segment-button']} */ ;
/** @type {__VLS_StyleScopedClasses['segment-button']} */ ;
/** @type {__VLS_StyleScopedClasses['segment-button']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['recommendation-list']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rank']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['item-name']} */ ;
/** @type {__VLS_StyleScopedClasses['focus-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['case-focus-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['price-source-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['threshold-card']} */ ;
/** @type {__VLS_StyleScopedClasses['threshold-card']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['bar-track']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline-segment']} */ ;
/** @type {__VLS_StyleScopedClasses['bar-fill']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page case-monitor-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "page-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.loadReport) },
    ...{ class: "primary-button" },
    type: "button",
});
if (__VLS_ctx.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel empty-panel" },
    });
}
else if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel empty-panel" },
    });
    (__VLS_ctx.error);
}
else if (__VLS_ctx.report) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "metrics-grid compact" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "metric-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.report.rangeHours);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "metric-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.okSnapshotCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "metric-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.missingSnapshotCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "metric-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.report.legacySteamMinorUnitCorrectedCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel report-meta case-report-meta" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "soft-label" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatTime(__VLS_ctx.report.startUtc));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "soft-label" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatTime(__VLS_ctx.report.endUtc));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "soft-label" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatTime(__VLS_ctx.report.generatedAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "soft-label" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.report.steamLiquidityStatus || "-");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "soft-label" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        type: "search",
        placeholder: "market hash name",
    });
    (__VLS_ctx.keyword);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel case-filter-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "segmented-control" },
        'aria-label': "箱子类别",
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.typeOptions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.loading))
                        return;
                    if (!!(__VLS_ctx.error))
                        return;
                    if (!(__VLS_ctx.report))
                        return;
                    __VLS_ctx.chooseType(option.key);
                } },
            key: (option.key),
            type: "button",
            ...{ class: "segment-button" },
            ...{ class: ({ active: __VLS_ctx.typeFilter === option.key }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (option.label);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (option.count);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "panel-title-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "soft-label" },
    });
    (__VLS_ctx.visibleItems.length);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "recommendation-list" },
    });
    for (const [item, index] of __VLS_getVForSourceType((__VLS_ctx.rankedItems.slice(0, 10)))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.loading))
                        return;
                    if (!!(__VLS_ctx.error))
                        return;
                    if (!(__VLS_ctx.report))
                        return;
                    __VLS_ctx.chooseItem(item);
                } },
            key: (item.marketHashName),
            ...{ class: "recommendation-row case-recommendation-row" },
            ...{ class: ({ active: __VLS_ctx.selectedItem?.marketHashName === item.marketHashName }) },
            type: "button",
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "rank" },
        });
        (index + 1);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "item-name" },
        });
        (item.marketHashName);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (item.crateTypeLabel);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.formatRatio(item.effectiveRecommendedMaxListingRatio ?? item.recommendedMaxListingRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (item.steamReferenceSourceLabel ?? "20墙");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.formatInt(item.steamVolume24h));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (item.liquidityLabel ?? "-");
    }
    if (__VLS_ctx.selectedItem) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
            ...{ class: "panel focus-panel" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "focus-heading" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "eyebrow" },
        });
        (__VLS_ctx.selectedItem.crateTypeLabel);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
        (__VLS_ctx.selectedItem.marketHashName);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatRatio(__VLS_ctx.selectedItem.effectiveRecommendedMaxListingRatio ?? __VLS_ctx.selectedItem.recommendedMaxListingRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "focus-grid case-focus-grid" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatRatio(__VLS_ctx.selectedItem.effectiveRecommendedMaxListingRatio ?? __VLS_ctx.selectedItem.recommendedMaxListingRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.selectedItem.steamReferenceSourceLabel ?? "20墙挂价");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem.steamReferencePrice ?? __VLS_ctx.selectedItem.latestSteamListPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatInt(__VLS_ctx.selectedItem.steamVolume24h));
        (__VLS_ctx.selectedItem.liquidityLabel ?? "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem.latestC5SellPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem.sellerWallListPrice ?? __VLS_ctx.selectedItem.latestSteamListPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "price-source-grid" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem.sellerFloorPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem.sellerWallListPrice ?? __VLS_ctx.selectedItem.latestSteamListPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem.buyerMaxPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "soft-label" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatInt(__VLS_ctx.selectedItem.steamAvgDailyVolume7d));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "threshold-grid" },
        });
        for (const [threshold] of __VLS_getVForSourceType((__VLS_ctx.selectedItem.ratioThresholds))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
                key: (threshold.key),
                ...{ class: "threshold-card" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (threshold.label);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (__VLS_ctx.formatRatio(threshold.ratio));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
            (threshold.durationLabel);
            (__VLS_ctx.formatPct(threshold.coveragePct));
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "ratio-timeline" },
            'aria-label': "比例时间线",
        });
        for (const [segment] of __VLS_getVForSourceType((__VLS_ctx.selectedItem.timelineSegments))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
                key: (`${segment.startedAt}-${segment.ratio}`),
                ...{ class: "timeline-segment" },
                ...{ style: (__VLS_ctx.timelineStyle(segment)) },
                title: (`${__VLS_ctx.formatTime(segment.startedAt)} - ${__VLS_ctx.formatTime(segment.endedAt)} | ${__VLS_ctx.formatRatio(segment.ratio)} | ${segment.durationLabel}`),
            });
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "bucket-bars detailed-buckets" },
        });
        for (const [bucket] of __VLS_getVForSourceType((__VLS_ctx.selectedItem.buckets))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                key: (bucket.bucket),
                ...{ class: "bucket-row detailed-bucket-row" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (bucket.bucket);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "bar-track" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div)({
                ...{ class: "bar-fill" },
                ...{ style: ({ width: `${Math.max(1, bucket.coveragePct)}%`, background: __VLS_ctx.ratioColor(bucket.lower) }) },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (bucket.durationLabel);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
            (__VLS_ctx.formatPct(bucket.coveragePct));
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "table-wrap" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({
        ...{ class: "data-table case-ratio-table" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.thead, __VLS_intrinsicElements.thead)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
    for (const [item] of __VLS_getVForSourceType((__VLS_ctx.visibleItems))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.loading))
                        return;
                    if (!!(__VLS_ctx.error))
                        return;
                    if (!(__VLS_ctx.report))
                        return;
                    __VLS_ctx.chooseItem(item);
                } },
            key: (item.marketHashName),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (item.crateTypeLabel);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (item.marketHashName);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatRatio(item.effectiveRecommendedMaxListingRatio ?? item.recommendedMaxListingRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (item.steamReferenceSourceLabel ?? "20墙");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatInt(item.steamVolume24h));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (item.liquidityLabel ?? "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatRatio(item.recommendedMaxListingRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatRatio(item.latestRatio));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatRatio(item.minRatio));
        (item.minRatioDurationLabel);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatRatio(item.maxRatio));
        (item.maxRatioDurationLabel);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatMoney(item.sellerFloorPrice));
        (__VLS_ctx.formatMoney(item.buyerMaxPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatMoney(item.latestC5SellPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatMoney(item.sellerWallListPrice ?? item.latestSteamListPrice));
    }
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-page']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-card']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['case-report-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['case-filter-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['segmented-control']} */ ;
/** @type {__VLS_StyleScopedClasses['segment-button']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['recommendation-list']} */ ;
/** @type {__VLS_StyleScopedClasses['recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-recommendation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rank']} */ ;
/** @type {__VLS_StyleScopedClasses['item-name']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['focus-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['focus-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['focus-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['case-focus-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['price-source-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['threshold-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['threshold-card']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline-segment']} */ ;
/** @type {__VLS_StyleScopedClasses['bucket-bars']} */ ;
/** @type {__VLS_StyleScopedClasses['detailed-buckets']} */ ;
/** @type {__VLS_StyleScopedClasses['bucket-row']} */ ;
/** @type {__VLS_StyleScopedClasses['detailed-bucket-row']} */ ;
/** @type {__VLS_StyleScopedClasses['bar-track']} */ ;
/** @type {__VLS_StyleScopedClasses['bar-fill']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['case-ratio-table']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            loading: loading,
            error: error,
            report: report,
            keyword: keyword,
            typeFilter: typeFilter,
            typeOptions: typeOptions,
            visibleItems: visibleItems,
            rankedItems: rankedItems,
            selectedItem: selectedItem,
            okSnapshotCount: okSnapshotCount,
            missingSnapshotCount: missingSnapshotCount,
            formatRatio: formatRatio,
            formatMoney: formatMoney,
            formatPct: formatPct,
            formatInt: formatInt,
            formatTime: formatTime,
            ratioColor: ratioColor,
            chooseType: chooseType,
            chooseItem: chooseItem,
            timelineStyle: timelineStyle,
            loadReport: loadReport,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
