import { ref } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
import CaseMonitorButton from "../components/case-monitor/CaseMonitorButton.vue";
import CaseMonitorCategoryTabs from "../components/case-monitor/CaseMonitorCategoryTabs.vue";
import CaseMonitorDetailDrawer from "../components/case-monitor/CaseMonitorDetailDrawer.vue";
import CaseMonitorFeedback from "../components/case-monitor/CaseMonitorFeedback.vue";
import CaseMonitorIntervalPicker from "../components/case-monitor/CaseMonitorIntervalPicker.vue";
import CaseMonitorPagination from "../components/case-monitor/CaseMonitorPagination.vue";
import CaseMonitorRecommendationRow from "../components/case-monitor/CaseMonitorRecommendationRow.vue";
import CaseMonitorSearch from "../components/case-monitor/CaseMonitorSearch.vue";
import CaseMonitorSegmented from "../components/case-monitor/CaseMonitorSegmented.vue";
import CaseMonitorStatusChip from "../components/case-monitor/CaseMonitorStatusChip.vue";
import CaseMonitorToggle from "../components/case-monitor/CaseMonitorToggle.vue";
import "../components/case-monitor/case-monitor.css";
const interval = ref(5);
const disabledInterval = ref(5);
const reportWindow = ref("24");
const refreshLiquidity = ref(true);
const searchEmpty = ref("");
const searchFilled = ref("Paris 2023");
const category = ref("all");
const page = ref(1);
const pageDisabled = ref(1);
const drawerOpen = ref(true);
const reportWindows = [
    { value: "24", label: "24小时" },
    { value: "168", label: "7天" },
    { value: "720", label: "30天" },
    { value: "custom", label: "自定义" },
];
const categories = [
    { key: "all", label: "全部", count: 439 },
    { key: "weapon_case", label: "武器箱", count: 42 },
    { key: "capsule", label: "胶囊", count: 231 },
    { key: "souvenir_package", label: "纪念包", count: 145 },
    { key: "other", label: "其他箱类", count: 21 },
];
const sampleTimelineRatios = [
    0.697, 0.698, 0.697, 0.699, 0.7, 0.699, 0.701, 0.699,
    0.698, 0.696, 0.697, 0.695, 0.694, 0.693, 0.69, 0.689,
    0.687, 0.686, 0.688, 0.69, 0.691, 0.692, 0.693, 0.694,
    0.693, 0.694, 0.695, 0.696, 0.697, 0.697, 0.698, 0.699,
];
const sampleItem = {
    marketHashName: "Horizon Case",
    name: "地平线武器箱",
    crateType: "weapon_case",
    crateTypeLabel: "武器箱",
    sampleCount: 288,
    okSampleCount: 288,
    latestRatio: 0.698,
    latestC5SellPrice: 4.72,
    latestSteamListPrice: 7.42,
    latestSteamAfterTaxPrice: 6.76,
    minRatio: 0.68,
    minRatioDurationLabel: "3h12m",
    maxRatio: 0.712,
    maxRatioDurationLabel: "1h05m",
    avgRatio: 0.698,
    p50Ratio: 0.696,
    p75Ratio: 0.701,
    p90Ratio: 0.708,
    conservativeMaxListingRatio: 0.68,
    recommendedMaxListingRatio: 0.698,
    aggressiveMaxListingRatio: 0.712,
    effectiveRecommendedMaxListingRatio: 0.698,
    selectedReferenceRatio: 0.698,
    steamReferenceSource: "seller_wall",
    steamReferenceSourceLabel: "20墙挂价",
    steamReferencePrice: 7.42,
    sellerFloorPrice: 7.35,
    sellerWallListPrice: 7.42,
    buyerMaxPrice: 7.21,
    steamVolume24h: 1284,
    steamVolume7d: 8972,
    steamAvgDailyVolume7d: 1281.7,
    liquidityLabel: "快",
    stddevRatio: 0.008,
    coveragePct: 97.01,
    recommendationScore: 0.98,
    legacySteamMinorUnitCorrectedCount: 0,
    buckets: [
        { bucket: "0.70-0.75", lower: 0.7, upper: 0.75, durationMinutes: 65, durationLabel: "1h05m", coveragePct: 19.4 },
        { bucket: "0.65-0.70", lower: 0.65, upper: 0.7, durationMinutes: 192, durationLabel: "3h12m", coveragePct: 47.1 },
        { bucket: "0.60-0.65", lower: 0.6, upper: 0.65, durationMinutes: 248, durationLabel: "4h08m", coveragePct: 30.4 },
        { bucket: "0.55-0.60", lower: 0.55, upper: 0.6, durationMinutes: 95, durationLabel: "1h35m", coveragePct: 11.5 },
        { bucket: "0.50-0.55", lower: 0.5, upper: 0.55, durationMinutes: 20, durationLabel: "20m", coveragePct: 1.6 },
    ],
    dominantBuckets: [],
    ratioThresholds: [],
    timelineSegments: sampleTimelineRatios.map((ratio, index) => ({
        startedAt: new Date(Date.UTC(2026, 6, 30, 3 + index * 0.75)).toISOString(),
        endedAt: new Date(Date.UTC(2026, 6, 30, 3.25 + index * 0.75)).toISOString(),
        ratio,
        bucket: "0.65-0.70",
        durationLabel: "45m",
        leftPct: index * 3.125,
        widthPct: 3.125,
    })),
};
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['case-atoms-page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--status']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-search']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-page-size']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--rows']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atoms-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atoms-page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atoms-layout']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "cm-surface case-atoms-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-atoms-layout" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "case-atoms-sheet" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell case-atom-cell--actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-atom-row" },
});
/** @type {[typeof CaseMonitorButton, typeof CaseMonitorButton, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(CaseMonitorButton, new CaseMonitorButton({
    tone: "primary",
    icon: "refresh",
}));
const __VLS_1 = __VLS_0({
    tone: "primary",
    icon: "refresh",
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_2.slots.default;
var __VLS_2;
/** @type {[typeof CaseMonitorButton, typeof CaseMonitorButton, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(CaseMonitorButton, new CaseMonitorButton({
    tone: "primary",
    icon: "download",
}));
const __VLS_4 = __VLS_3({
    tone: "primary",
    icon: "download",
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
__VLS_5.slots.default;
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_6 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "sparkles",
    size: (15),
}));
const __VLS_7 = __VLS_6({
    name: "sparkles",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_6));
var __VLS_5;
/** @type {[typeof CaseMonitorButton, typeof CaseMonitorButton, ]} */ ;
// @ts-ignore
const __VLS_9 = __VLS_asFunctionalComponent(CaseMonitorButton, new CaseMonitorButton({
    icon: "pause",
}));
const __VLS_10 = __VLS_9({
    icon: "pause",
}, ...__VLS_functionalComponentArgsRest(__VLS_9));
__VLS_11.slots.default;
var __VLS_11;
/** @type {[typeof CaseMonitorButton, typeof CaseMonitorButton, ]} */ ;
// @ts-ignore
const __VLS_12 = __VLS_asFunctionalComponent(CaseMonitorButton, new CaseMonitorButton({
    tone: "success",
    icon: "play",
}));
const __VLS_13 = __VLS_12({
    tone: "success",
    icon: "play",
}, ...__VLS_functionalComponentArgsRest(__VLS_12));
__VLS_14.slots.default;
var __VLS_14;
/** @type {[typeof CaseMonitorButton, typeof CaseMonitorButton, ]} */ ;
// @ts-ignore
const __VLS_15 = __VLS_asFunctionalComponent(CaseMonitorButton, new CaseMonitorButton({
    icon: "error",
    disabled: true,
}));
const __VLS_16 = __VLS_15({
    icon: "error",
    disabled: true,
}, ...__VLS_functionalComponentArgsRest(__VLS_15));
__VLS_17.slots.default;
var __VLS_17;
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell case-atom-cell--status" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-atom-row" },
});
/** @type {[typeof CaseMonitorStatusChip, ]} */ ;
// @ts-ignore
const __VLS_18 = __VLS_asFunctionalComponent(CaseMonitorStatusChip, new CaseMonitorStatusChip({
    status: "running",
}));
const __VLS_19 = __VLS_18({
    status: "running",
}, ...__VLS_functionalComponentArgsRest(__VLS_18));
/** @type {[typeof CaseMonitorStatusChip, ]} */ ;
// @ts-ignore
const __VLS_21 = __VLS_asFunctionalComponent(CaseMonitorStatusChip, new CaseMonitorStatusChip({
    status: "paused",
}));
const __VLS_22 = __VLS_21({
    status: "paused",
}, ...__VLS_functionalComponentArgsRest(__VLS_21));
/** @type {[typeof CaseMonitorStatusChip, ]} */ ;
// @ts-ignore
const __VLS_24 = __VLS_asFunctionalComponent(CaseMonitorStatusChip, new CaseMonitorStatusChip({
    status: "collecting",
    label: "正在采集 128/439",
}));
const __VLS_25 = __VLS_24({
    status: "collecting",
    label: "正在采集 128/439",
}, ...__VLS_functionalComponentArgsRest(__VLS_24));
/** @type {[typeof CaseMonitorStatusChip, ]} */ ;
// @ts-ignore
const __VLS_27 = __VLS_asFunctionalComponent(CaseMonitorStatusChip, new CaseMonitorStatusChip({
    status: "reporting",
}));
const __VLS_28 = __VLS_27({
    status: "reporting",
}, ...__VLS_functionalComponentArgsRest(__VLS_27));
/** @type {[typeof CaseMonitorStatusChip, ]} */ ;
// @ts-ignore
const __VLS_30 = __VLS_asFunctionalComponent(CaseMonitorStatusChip, new CaseMonitorStatusChip({
    status: "failed",
}));
const __VLS_31 = __VLS_30({
    status: "failed",
}, ...__VLS_functionalComponentArgsRest(__VLS_30));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
/** @type {[typeof CaseMonitorIntervalPicker, ]} */ ;
// @ts-ignore
const __VLS_33 = __VLS_asFunctionalComponent(CaseMonitorIntervalPicker, new CaseMonitorIntervalPicker({
    modelValue: (__VLS_ctx.interval),
    expanded: true,
}));
const __VLS_34 = __VLS_33({
    modelValue: (__VLS_ctx.interval),
    expanded: true,
}, ...__VLS_functionalComponentArgsRest(__VLS_33));
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    ...{ class: "case-atom-subtitle" },
});
/** @type {[typeof CaseMonitorIntervalPicker, ]} */ ;
// @ts-ignore
const __VLS_36 = __VLS_asFunctionalComponent(CaseMonitorIntervalPicker, new CaseMonitorIntervalPicker({
    modelValue: (__VLS_ctx.disabledInterval),
    expanded: true,
    disabled: true,
}));
const __VLS_37 = __VLS_36({
    modelValue: (__VLS_ctx.disabledInterval),
    expanded: true,
    disabled: true,
}, ...__VLS_functionalComponentArgsRest(__VLS_36));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
/** @type {[typeof CaseMonitorSegmented, ]} */ ;
// @ts-ignore
const __VLS_39 = __VLS_asFunctionalComponent(CaseMonitorSegmented, new CaseMonitorSegmented({
    modelValue: (__VLS_ctx.reportWindow),
    options: (__VLS_ctx.reportWindows),
}));
const __VLS_40 = __VLS_39({
    modelValue: (__VLS_ctx.reportWindow),
    options: (__VLS_ctx.reportWindows),
}, ...__VLS_functionalComponentArgsRest(__VLS_39));
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    ...{ class: "case-atom-subtitle" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-toggle-samples" },
});
/** @type {[typeof CaseMonitorToggle, ]} */ ;
// @ts-ignore
const __VLS_42 = __VLS_asFunctionalComponent(CaseMonitorToggle, new CaseMonitorToggle({
    modelValue: (__VLS_ctx.refreshLiquidity),
}));
const __VLS_43 = __VLS_42({
    modelValue: (__VLS_ctx.refreshLiquidity),
}, ...__VLS_functionalComponentArgsRest(__VLS_42));
/** @type {[typeof CaseMonitorToggle, ]} */ ;
// @ts-ignore
const __VLS_45 = __VLS_asFunctionalComponent(CaseMonitorToggle, new CaseMonitorToggle({
    ...{ 'onUpdate:modelValue': {} },
    modelValue: (false),
}));
const __VLS_46 = __VLS_45({
    ...{ 'onUpdate:modelValue': {} },
    modelValue: (false),
}, ...__VLS_functionalComponentArgsRest(__VLS_45));
let __VLS_48;
let __VLS_49;
let __VLS_50;
const __VLS_51 = {
    'onUpdate:modelValue': (...[$event]) => {
        __VLS_ctx.refreshLiquidity = $event;
    }
};
var __VLS_47;
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
/** @type {[typeof CaseMonitorSearch, ]} */ ;
// @ts-ignore
const __VLS_52 = __VLS_asFunctionalComponent(CaseMonitorSearch, new CaseMonitorSearch({
    modelValue: (__VLS_ctx.searchEmpty),
}));
const __VLS_53 = __VLS_52({
    modelValue: (__VLS_ctx.searchEmpty),
}, ...__VLS_functionalComponentArgsRest(__VLS_52));
/** @type {[typeof CaseMonitorSearch, ]} */ ;
// @ts-ignore
const __VLS_55 = __VLS_asFunctionalComponent(CaseMonitorSearch, new CaseMonitorSearch({
    modelValue: (__VLS_ctx.searchFilled),
}));
const __VLS_56 = __VLS_55({
    modelValue: (__VLS_ctx.searchFilled),
}, ...__VLS_functionalComponentArgsRest(__VLS_55));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
/** @type {[typeof CaseMonitorCategoryTabs, ]} */ ;
// @ts-ignore
const __VLS_58 = __VLS_asFunctionalComponent(CaseMonitorCategoryTabs, new CaseMonitorCategoryTabs({
    modelValue: (__VLS_ctx.category),
    options: (__VLS_ctx.categories),
}));
const __VLS_59 = __VLS_58({
    modelValue: (__VLS_ctx.category),
    options: (__VLS_ctx.categories),
}, ...__VLS_functionalComponentArgsRest(__VLS_58));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-pagination-samples" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-pagination-stack" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
/** @type {[typeof CaseMonitorPagination, ]} */ ;
// @ts-ignore
const __VLS_61 = __VLS_asFunctionalComponent(CaseMonitorPagination, new CaseMonitorPagination({
    ...{ 'onUpdate:pageSize': {} },
    modelValue: (__VLS_ctx.page),
    totalItems: (439),
    pageSize: (10),
    compact: true,
}));
const __VLS_62 = __VLS_61({
    ...{ 'onUpdate:pageSize': {} },
    modelValue: (__VLS_ctx.page),
    totalItems: (439),
    pageSize: (10),
    compact: true,
}, ...__VLS_functionalComponentArgsRest(__VLS_61));
let __VLS_64;
let __VLS_65;
let __VLS_66;
const __VLS_67 = {
    'onUpdate:pageSize': (() => undefined)
};
var __VLS_63;
/** @type {[typeof CaseMonitorPagination, ]} */ ;
// @ts-ignore
const __VLS_68 = __VLS_asFunctionalComponent(CaseMonitorPagination, new CaseMonitorPagination({
    ...{ 'onUpdate:pageSize': {} },
    modelValue: (__VLS_ctx.pageDisabled),
    totalItems: (439),
    pageSize: (10),
    compact: true,
    disabled: true,
}));
const __VLS_69 = __VLS_68({
    ...{ 'onUpdate:pageSize': {} },
    modelValue: (__VLS_ctx.pageDisabled),
    totalItems: (439),
    pageSize: (10),
    compact: true,
    disabled: true,
}, ...__VLS_functionalComponentArgsRest(__VLS_68));
let __VLS_71;
let __VLS_72;
let __VLS_73;
const __VLS_74 = {
    'onUpdate:pageSize': (() => undefined)
};
var __VLS_70;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    ...{ class: "cm-page-size" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-atoms-bottom" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell case-atom-cell--rows" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
/** @type {[typeof CaseMonitorRecommendationRow, ]} */ ;
// @ts-ignore
const __VLS_75 = __VLS_asFunctionalComponent(CaseMonitorRecommendationRow, new CaseMonitorRecommendationRow({
    item: (__VLS_ctx.sampleItem),
    rank: (1),
}));
const __VLS_76 = __VLS_75({
    item: (__VLS_ctx.sampleItem),
    rank: (1),
}, ...__VLS_functionalComponentArgsRest(__VLS_75));
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    ...{ class: "case-atom-subtitle" },
});
/** @type {[typeof CaseMonitorRecommendationRow, ]} */ ;
// @ts-ignore
const __VLS_78 = __VLS_asFunctionalComponent(CaseMonitorRecommendationRow, new CaseMonitorRecommendationRow({
    item: (__VLS_ctx.sampleItem),
    rank: (1),
    selected: true,
}));
const __VLS_79 = __VLS_78({
    item: (__VLS_ctx.sampleItem),
    rank: (1),
    selected: true,
}, ...__VLS_functionalComponentArgsRest(__VLS_78));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell case-atom-cell--feedback" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
/** @type {[typeof CaseMonitorFeedback, typeof CaseMonitorFeedback, ]} */ ;
// @ts-ignore
const __VLS_81 = __VLS_asFunctionalComponent(CaseMonitorFeedback, new CaseMonitorFeedback({
    tone: "success",
}));
const __VLS_82 = __VLS_81({
    tone: "success",
}, ...__VLS_functionalComponentArgsRest(__VLS_81));
__VLS_83.slots.default;
var __VLS_83;
/** @type {[typeof CaseMonitorFeedback, typeof CaseMonitorFeedback, ]} */ ;
// @ts-ignore
const __VLS_84 = __VLS_asFunctionalComponent(CaseMonitorFeedback, new CaseMonitorFeedback({
    tone: "error",
}));
const __VLS_85 = __VLS_84({
    tone: "error",
}, ...__VLS_functionalComponentArgsRest(__VLS_84));
__VLS_86.slots.default;
var __VLS_86;
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-atom-cell case-atom-cell--trigger" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.br)({});
/** @type {[typeof CaseMonitorDetailDrawer, ]} */ ;
// @ts-ignore
const __VLS_87 = __VLS_asFunctionalComponent(CaseMonitorDetailDrawer, new CaseMonitorDetailDrawer({
    ...{ 'onClose': {} },
    open: (__VLS_ctx.drawerOpen),
    item: (__VLS_ctx.sampleItem),
    embedded: true,
}));
const __VLS_88 = __VLS_87({
    ...{ 'onClose': {} },
    open: (__VLS_ctx.drawerOpen),
    item: (__VLS_ctx.sampleItem),
    embedded: true,
}, ...__VLS_functionalComponentArgsRest(__VLS_87));
let __VLS_90;
let __VLS_91;
let __VLS_92;
const __VLS_93 = {
    onClose: (...[$event]) => {
        __VLS_ctx.drawerOpen = false;
    }
};
var __VLS_89;
/** @type {__VLS_StyleScopedClasses['cm-surface']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atoms-page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atoms-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atoms-sheet']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--actions']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--status']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-row']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-subtitle']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-subtitle']} */ ;
/** @type {__VLS_StyleScopedClasses['case-toggle-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-samples']} */ ;
/** @type {__VLS_StyleScopedClasses['case-pagination-stack']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-page-size']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atoms-bottom']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--rows']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-subtitle']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--feedback']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['case-atom-cell--trigger']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            CaseMonitorButton: CaseMonitorButton,
            CaseMonitorCategoryTabs: CaseMonitorCategoryTabs,
            CaseMonitorDetailDrawer: CaseMonitorDetailDrawer,
            CaseMonitorFeedback: CaseMonitorFeedback,
            CaseMonitorIntervalPicker: CaseMonitorIntervalPicker,
            CaseMonitorPagination: CaseMonitorPagination,
            CaseMonitorRecommendationRow: CaseMonitorRecommendationRow,
            CaseMonitorSearch: CaseMonitorSearch,
            CaseMonitorSegmented: CaseMonitorSegmented,
            CaseMonitorStatusChip: CaseMonitorStatusChip,
            CaseMonitorToggle: CaseMonitorToggle,
            interval: interval,
            disabledInterval: disabledInterval,
            reportWindow: reportWindow,
            refreshLiquidity: refreshLiquidity,
            searchEmpty: searchEmpty,
            searchFilled: searchFilled,
            category: category,
            page: page,
            pageDisabled: pageDisabled,
            drawerOpen: drawerOpen,
            reportWindows: reportWindows,
            categories: categories,
            sampleItem: sampleItem,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
