import { computed, onMounted, ref } from "vue";
import FolioDateTimeRange from "../components/FolioDateTimeRange.vue";
function startOfMonth() {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0);
}
const dateRange = ref([startOfMonth(), new Date()]);
const itemName = ref("");
const includeDetails = ref(false);
const loading = ref(false);
const error = ref("");
const report = ref(null);
const showTimeExplanation = ref(false);
const itemSortDescending = ref(true);
const detailPage = ref(1);
const detailPageSize = 50;
const emptySummary = () => ({
    count: 0,
    steamGross: 0,
    steamNet: 0,
    cash: 0,
    totalDiscountRatio: null,
});
const reconciliationRows = computed(() => {
    if (!report.value)
        return [];
    return [
        { key: "closed", label: "本期已闭环", tone: "success", summary: report.value.steamSoldReconciliation.closed, historical: false },
        { key: "unclosed", label: "本期未闭环", tone: "warning", summary: report.value.steamSoldReconciliation.unclosed, historical: false },
        { key: "history-closed", label: "本期历史补仓", tone: "neutral", summary: report.value.closedFromSellOutsideRange, historical: true },
        { key: "history-unclosed", label: "本期历史未闭环", tone: "neutral", summary: report.value.historicalUnclosedBeforeRange, historical: true },
    ];
});
const currentWalletSummary = computed(() => {
    if (!report.value)
        return emptySummary();
    const closed = report.value.steamSoldReconciliation.closed;
    const unclosed = report.value.steamSoldReconciliation.unclosed;
    const steamNet = closed.steamNet + unclosed.steamNet;
    const cash = closed.cash + unclosed.cash;
    return {
        count: closed.count + unclosed.count,
        steamGross: closed.steamGross + unclosed.steamGross,
        steamNet,
        cash,
        totalDiscountRatio: steamNet > 0 ? cash / steamNet : null,
    };
});
const sortedItems = computed(() => {
    const items = [...(report.value?.items ?? [])];
    return items.sort((a, b) => itemSortDescending.value ? b.cash - a.cash : a.cash - b.cash);
});
const missingOfficialTime = computed(() => report.value?.steamSoldMissingSoldAt.summary ?? emptySummary());
const detailPageCount = computed(() => Math.max(1, Math.ceil((report.value?.details.length ?? 0) / detailPageSize)));
const visibleDetails = computed(() => {
    const start = (detailPage.value - 1) * detailPageSize;
    return (report.value?.details ?? []).slice(start, start + detailPageSize);
});
const apiState = computed(() => {
    if (loading.value && !report.value)
        return { label: "正在连接报表 API", tone: "pending" };
    if (error.value)
        return { label: "报表 API 异常", tone: "danger" };
    if (report.value)
        return { label: "报表 API 已连接", tone: "success" };
    return { label: "等待查询", tone: "pending" };
});
function pad(value) {
    return String(value).padStart(2, "0");
}
function toLocalInput(value) {
    return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}
function formatMoney(value) {
    return `CNY ${Number(value || 0).toFixed(2)}`;
}
function formatPct(value) {
    return value == null ? "—" : `${(value * 100).toFixed(2)}%`;
}
function formatRangeLabel() {
    if (!report.value)
        return "尚未查询";
    return `${report.value.startLocal.replace("T", " ").slice(0, 16)} 至 ${report.value.endLocal.replace("T", " ").slice(0, 16)}（北京时间）`;
}
async function queryReport() {
    if (dateRange.value.length !== 2 || !dateRange.value[0] || !dateRange.value[1]) {
        error.value = "请选择完整的开始和结束时间";
        return;
    }
    loading.value = true;
    error.value = "";
    try {
        const response = await fetch("/api/guadao-report/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                dateFrom: toLocalInput(dateRange.value[0]),
                dateTo: toLocalInput(dateRange.value[1]),
                marketHashName: itemName.value.trim() || null,
                includeDetails: includeDetails.value,
            }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok)
            throw new Error(payload.error || `HTTP ${response.status}`);
        report.value = payload.report;
        detailPage.value = 1;
    }
    catch (reason) {
        error.value = reason instanceof Error ? reason.message : "挂刀报表查询失败";
    }
    finally {
        loading.value = false;
    }
}
onMounted(queryReport);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['api-state']} */ ;
/** @type {__VLS_StyleScopedClasses['api-state']} */ ;
/** @type {__VLS_StyleScopedClasses['api-state']} */ ;
/** @type {__VLS_StyleScopedClasses['api-state']} */ ;
/** @type {__VLS_StyleScopedClasses['query-field']} */ ;
/** @type {__VLS_StyleScopedClasses['query-field']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['query-error']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['report-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['report-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['report-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['report-table']} */ ;
/** @type {__VLS_StyleScopedClasses['report-table']} */ ;
/** @type {__VLS_StyleScopedClasses['report-table']} */ ;
/** @type {__VLS_StyleScopedClasses['historical-row']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['success']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['time-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['time-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['time-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['report-loading']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page guadao-report-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "page-header report-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "api-state" },
    ...{ class: (__VLS_ctx.apiState.tone) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.apiState.label);
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel report-query-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "report-query-grid" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "query-field date-field" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
/** @type {[typeof FolioDateTimeRange, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioDateTimeRange, new FolioDateTimeRange({
    modelValue: (__VLS_ctx.dateRange),
}));
const __VLS_1 = __VLS_0({
    modelValue: (__VLS_ctx.dateRange),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "query-field" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onKeyup: (__VLS_ctx.queryReport) },
    value: (__VLS_ctx.itemName),
    type: "text",
    placeholder: "精确 market hash name（可选）",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "detail-toggle" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "checkbox",
});
(__VLS_ctx.includeDetails);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.queryReport) },
    ...{ class: "primary-button query-button" },
    type: "button",
    disabled: (__VLS_ctx.loading),
});
(__VLS_ctx.loading ? "查询中…" : "查询报表");
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "query-error" },
    });
    (__VLS_ctx.error);
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "query-caption" },
    });
    (__VLS_ctx.formatRangeLabel());
}
if (__VLS_ctx.report) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "report-section" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "section-heading" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "eyebrow" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "report-metrics" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.currentWalletSummary.count);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.currentWalletSummary.steamGross));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.currentWalletSummary.steamNet));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.currentWalletSummary.cash));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatPct(__VLS_ctx.currentWalletSummary.totalDiscountRatio));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "section-note" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel reconciliation-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "section-heading compact" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "eyebrow" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "table-wrap" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({
        ...{ class: "data-table report-table" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.thead, __VLS_intrinsicElements.thead)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
    for (const [row] of __VLS_getVForSourceType((__VLS_ctx.reconciliationRows))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
            key: (row.key),
            ...{ class: ({ 'historical-row': row.historical }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "status-pill" },
            ...{ class: (row.tone) },
        });
        (row.label);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (row.summary.count);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatMoney(row.summary.steamGross));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatMoney(row.summary.steamNet));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (row.summary.cash > 0 ? __VLS_ctx.formatMoney(row.summary.cash) : "—");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.formatPct(row.summary.totalDiscountRatio));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "panel-footnote" },
    });
    if (__VLS_ctx.missingOfficialTime.count) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
            ...{ class: "time-warning" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.missingOfficialTime.count);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.missingOfficialTime.steamNet));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.report))
                        return;
                    if (!(__VLS_ctx.missingOfficialTime.count))
                        return;
                    __VLS_ctx.showTimeExplanation = !__VLS_ctx.showTimeExplanation;
                } },
            type: "button",
        });
        (__VLS_ctx.showTimeExplanation ? "收起说明" : "查看说明");
        if (__VLS_ctx.showTimeExplanation) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel item-summary-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "section-heading compact" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "eyebrow" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.report))
                    return;
                __VLS_ctx.itemSortDescending = !__VLS_ctx.itemSortDescending;
            } },
        ...{ class: "secondary-button" },
        type: "button",
    });
    (__VLS_ctx.itemSortDescending ? "降序" : "升序");
    if (__VLS_ctx.sortedItems.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "table-wrap" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({
            ...{ class: "data-table report-table item-table" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.thead, __VLS_intrinsicElements.thead)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
        for (const [row] of __VLS_getVForSourceType((__VLS_ctx.sortedItems))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
                key: (row.marketHashName),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
            (row.marketHashName);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
            (row.count);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
            (__VLS_ctx.formatMoney(row.steamNet));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
            (__VLS_ctx.formatMoney(row.cash));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
            (__VLS_ctx.formatPct(row.totalDiscountRatio));
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "empty-report" },
        });
    }
    if (__VLS_ctx.includeDetails) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
            ...{ class: "panel detail-panel" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "section-heading compact" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "eyebrow" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.report.details.length);
        if (__VLS_ctx.report.details.length) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "table-wrap" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({
                ...{ class: "data-table report-table detail-table" },
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
            __VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
            for (const [row] of __VLS_getVForSourceType((__VLS_ctx.visibleDetails))) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
                    key: (`${row.listingId}-${row.assetId}`),
                });
                __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
                (row.completedAtLocal);
                __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
                (row.marketHashName);
                __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
                (__VLS_ctx.formatMoney(row.steamGross));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
                (__VLS_ctx.formatMoney(row.steamNet));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
                (__VLS_ctx.formatMoney(row.cash));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
                (__VLS_ctx.formatPct(row.totalDiscountRatio));
                __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
                (row.assetId || "—");
                __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
                (row.listingId || "—");
            }
        }
        else {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "empty-report" },
            });
        }
        if (__VLS_ctx.report.details.length > __VLS_ctx.detailPageSize) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "detail-pagination" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.detailPage);
            (__VLS_ctx.detailPageCount);
            (__VLS_ctx.detailPageSize);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.report))
                            return;
                        if (!(__VLS_ctx.includeDetails))
                            return;
                        if (!(__VLS_ctx.report.details.length > __VLS_ctx.detailPageSize))
                            return;
                        __VLS_ctx.detailPage -= 1;
                    } },
                ...{ class: "secondary-button" },
                type: "button",
                disabled: (__VLS_ctx.detailPage <= 1),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.report))
                            return;
                        if (!(__VLS_ctx.includeDetails))
                            return;
                        if (!(__VLS_ctx.report.details.length > __VLS_ctx.detailPageSize))
                            return;
                        __VLS_ctx.detailPage += 1;
                    } },
                ...{ class: "secondary-button" },
                type: "button",
                disabled: (__VLS_ctx.detailPage >= __VLS_ctx.detailPageCount),
            });
        }
    }
}
else if (__VLS_ctx.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel report-loading" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['guadao-report-page']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['report-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['api-state']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['report-query-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['report-query-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['query-field']} */ ;
/** @type {__VLS_StyleScopedClasses['date-field']} */ ;
/** @type {__VLS_StyleScopedClasses['query-field']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['query-button']} */ ;
/** @type {__VLS_StyleScopedClasses['query-error']} */ ;
/** @type {__VLS_StyleScopedClasses['query-caption']} */ ;
/** @type {__VLS_StyleScopedClasses['report-section']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['report-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['section-note']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['reconciliation-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['report-table']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-footnote']} */ ;
/** @type {__VLS_StyleScopedClasses['time-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['item-summary-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['report-table']} */ ;
/** @type {__VLS_StyleScopedClasses['item-table']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-report']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['compact']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['report-table']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-table']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-report']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['report-loading']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioDateTimeRange: FolioDateTimeRange,
            dateRange: dateRange,
            itemName: itemName,
            includeDetails: includeDetails,
            loading: loading,
            error: error,
            report: report,
            showTimeExplanation: showTimeExplanation,
            itemSortDescending: itemSortDescending,
            detailPage: detailPage,
            detailPageSize: detailPageSize,
            reconciliationRows: reconciliationRows,
            currentWalletSummary: currentWalletSummary,
            sortedItems: sortedItems,
            missingOfficialTime: missingOfficialTime,
            detailPageCount: detailPageCount,
            visibleDetails: visibleDetails,
            apiState: apiState,
            formatMoney: formatMoney,
            formatPct: formatPct,
            formatRangeLabel: formatRangeLabel,
            queryReport: queryReport,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
