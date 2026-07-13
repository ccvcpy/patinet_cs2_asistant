import { computed, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
const steps = [
    { key: "discovered", label: "发现机会", index: 0 }, { key: "audited", label: "审计", index: 1 },
    { key: "asset_locked", label: "锁定 A", index: 2 }, { key: "steam_bought", label: "买入 B", index: 3 },
    { key: "c5_listed", label: "C5 上架", index: 4 }, { key: "c5_sold", label: "C5 售出", index: 5 },
    { key: "settled", label: "收益结算", index: 6 },
];
const route = useRoute();
const rows = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;
const stepCounts = ref([]);
const loading = ref(false);
const error = ref("");
const routeTradeNo = typeof route.query.tradeNo === "string" ? route.query.tradeNo : "";
const keywordDraft = ref(routeTradeNo);
const keyword = ref(routeTradeNo);
const from = ref("");
const to = ref("");
const stepKey = ref("");
const status = ref("");
const acknowledged = ref("exclude");
const selected = ref(null);
const timeline = ref([]);
const timelineLoading = ref(false);
const timelineError = ref("");
const acknowledgeReason = ref("");
const actionBusy = ref(false);
const actionMessage = ref("");
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));
function stepCount(index) {
    return Number(stepCounts.value.find(item => Number(item.stepIndex) === index)?.count) || 0;
}
function statusLabel(value) {
    return { cancelled: "已取消", failed: "失败", manual_required: "需人工处理" }[value] || value;
}
function statusClass(value) {
    return value === "manual_required" ? "manual" : value === "failed" ? "failed" : "cancelled";
}
function time(value) {
    if (!value)
        return "-";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}
function apiTime(value) {
    if (!value)
        return "";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString();
}
function pct(value) {
    if (typeof value !== "number" || !Number.isFinite(value))
        return "-";
    return `${(Math.abs(value) <= 1 ? value * 100 : value).toFixed(2)}%`;
}
function money(value) {
    return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "-";
}
function reason(trade) {
    return trade.cancelReason || trade.error || String(trade.note?.cancelReason || trade.note?.manualReviewReason || "未记录明确原因");
}
function note(trade, key) {
    const value = trade.note?.[key];
    return value === undefined || value === null || value === "" ? "-" : String(value);
}
function evidenceBoolean(value) {
    if (typeof value === "boolean")
        return value;
    if (typeof value === "number" && (value === 0 || value === 1))
        return value === 1;
    if (typeof value === "string") {
        const normalized = value.trim().toLowerCase();
        if (["true", "1", "yes", "sent", "obtained"].includes(normalized))
            return true;
        if (["false", "0", "no", "not_sent", "not_obtained"].includes(normalized))
            return false;
    }
    return null;
}
function evidenceLabel(trade, field) {
    const topLevel = evidenceBoolean(trade[field]);
    const resolved = topLevel ?? evidenceBoolean(trade.note?.[field]);
    if (resolved === null)
        return "未记录";
    if (field === "purchaseRequestSent")
        return resolved ? "已发送" : "未发送";
    return resolved ? "已取得" : "未取得";
}
async function responseError(response) {
    try {
        const payload = await response.json();
        return payload.error || payload.detail || response.statusText;
    }
    catch {
        return response.statusText;
    }
}
async function load() {
    loading.value = true;
    error.value = "";
    const query = new URLSearchParams({ page: String(page.value), pageSize: String(pageSize), acknowledged: acknowledged.value });
    if (keyword.value)
        query.set("keyword", keyword.value);
    if (from.value)
        query.set("from", apiTime(from.value));
    if (to.value)
        query.set("to", apiTime(to.value));
    if (stepKey.value)
        query.set("stepKey", stepKey.value);
    if (status.value)
        query.set("status", status.value);
    try {
        const response = await fetch(`/api/profit-trade/interruptions?${query}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        rows.value = Array.isArray(payload.items) ? payload.items : [];
        total.value = Number(payload.total) || 0;
        stepCounts.value = payload.summary?.stepCounts || payload.stepCounts || [];
        const queryTradeNo = typeof route.query.tradeNo === "string" ? route.query.tradeNo : "";
        const nextSelected = rows.value.find(item => item.tradeNo === queryTradeNo)
            || rows.value.find(item => item.id === selected.value?.id) || rows.value[0] || null;
        if (nextSelected)
            await selectTrade(nextSelected);
        else {
            selected.value = null;
            timeline.value = [];
        }
    }
    catch (cause) {
        rows.value = [];
        total.value = 0;
        stepCounts.value = [];
        selected.value = null;
        timeline.value = [];
        error.value = `中断记录读取失败：${cause instanceof Error ? cause.message : String(cause)}`;
    }
    finally {
        loading.value = false;
    }
}
function search() { keyword.value = keywordDraft.value.trim(); page.value = 1; void load(); }
function reset() { keywordDraft.value = ""; keyword.value = ""; from.value = ""; to.value = ""; stepKey.value = ""; status.value = ""; acknowledged.value = "exclude"; page.value = 1; void load(); }
function turn(direction) { const next = page.value + direction; if (next >= 1 && next <= totalPages.value) {
    page.value = next;
    void load();
} }
async function selectTrade(trade) {
    selected.value = trade;
    timelineLoading.value = true;
    timelineError.value = "";
    actionMessage.value = "";
    try {
        const response = await fetch(`/api/profit-trade/interruptions/timeline?tradeId=${encodeURIComponent(trade.id)}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        selected.value = payload.trade || trade;
        timeline.value = Array.isArray(payload.events) ? payload.events : [];
    }
    catch (cause) {
        timeline.value = [];
        timelineError.value = `时间线读取失败：${cause instanceof Error ? cause.message : String(cause)}`;
    }
    finally {
        timelineLoading.value = false;
    }
}
async function setAcknowledged(action) {
    if (!selected.value || actionBusy.value)
        return;
    actionBusy.value = true;
    actionMessage.value = "";
    try {
        const response = await fetch("/api/profit-trade/interruptions/acknowledge", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tradeId: selected.value.id, action, reason: acknowledgeReason.value.trim() }) });
        if (!response.ok)
            throw new Error(await responseError(response));
        const successMessage = action === "acknowledge" ? "已知晓；审计记录仍保留。" : "已恢复到默认问题列表。";
        acknowledgeReason.value = "";
        await load();
        actionMessage.value = successMessage;
    }
    catch (cause) {
        actionMessage.value = `操作未完成：${cause instanceof Error ? cause.message : String(cause)}`;
    }
    finally {
        actionBusy.value = false;
    }
}
watch([from, to, stepKey, status, acknowledged], () => { page.value = 1; void load(); });
watch(() => route.query.tradeNo, value => {
    if (typeof value !== "string" || value === keyword.value)
        return;
    keywordDraft.value = value;
    keyword.value = value;
    page.value = 1;
    void load();
});
onMounted(() => void load());
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['page-title']} */ ;
/** @type {__VLS_StyleScopedClasses['page-title']} */ ;
/** @type {__VLS_StyleScopedClasses['step-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['step-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['step-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['step-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['step-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['step-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['step-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-list']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-list']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-list']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-list']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-list']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-item']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-item']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-item']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-item']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-item']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-item']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-item']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-line']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-line']} */ ;
/** @type {__VLS_StyleScopedClasses['status']} */ ;
/** @type {__VLS_StyleScopedClasses['status']} */ ;
/** @type {__VLS_StyleScopedClasses['status']} */ ;
/** @type {__VLS_StyleScopedClasses['list-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['list-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['detail']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-badges']} */ ;
/** @type {__VLS_StyleScopedClasses['status']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['log-link']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['done']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-evidence']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-evidence']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-evidence']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-evidence']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-evidence']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['acknowledge']} */ ;
/** @type {__VLS_StyleScopedClasses['acknowledge']} */ ;
/** @type {__VLS_StyleScopedClasses['acknowledge']} */ ;
// CSS variable injection
// CSS variable injection end
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "interruptions-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "page-title" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.load) },
    ...{ class: "secondary-button refresh" },
    type: "button",
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "refresh",
    size: (15),
}));
const __VLS_1 = __VLS_0({
    name: "refresh",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "step-summary" },
    'aria-label': "各步骤中断数量",
});
for (const [step] of __VLS_getVForSourceType((__VLS_ctx.steps))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.stepKey = __VLS_ctx.stepKey === step.key ? '' : step.key;
            } },
        key: (step.key),
        type: "button",
        ...{ class: ({ active: __VLS_ctx.stepKey === step.key }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (step.index + 1);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.stepCount(step.index));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (step.label);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
    ...{ onSubmit: (__VLS_ctx.search) },
    ...{ class: "filter-bar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "keyword" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    type: "search",
    placeholder: "tradeNo / 中文名 / marketHashName",
});
(__VLS_ctx.keywordDraft);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    type: "datetime-local",
});
(__VLS_ctx.from);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    type: "datetime-local",
});
(__VLS_ctx.to);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.status),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "cancelled",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "failed",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "manual_required",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.acknowledged),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "exclude",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "include",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "only",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "primary-button" },
    type: "submit",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.reset) },
    ...{ class: "secondary-button" },
    type: "button",
});
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "page-error" },
    });
    (__VLS_ctx.error);
}
if (__VLS_ctx.actionMessage) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "action-message global-message" },
    });
    (__VLS_ctx.actionMessage);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "master-detail" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
    ...{ class: "trade-list panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.total);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
if (__VLS_ctx.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty" },
    });
}
else if (__VLS_ctx.rows.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty" },
    });
}
else {
    for (const [trade] of __VLS_getVForSourceType((__VLS_ctx.rows))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.loading))
                        return;
                    if (!!(__VLS_ctx.rows.length === 0))
                        return;
                    __VLS_ctx.selectTrade(trade);
                } },
            key: (trade.id),
            type: "button",
            ...{ class: (['trade-item', { selected: __VLS_ctx.selected?.id === trade.id }]) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "trade-item-head" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: (['status', __VLS_ctx.statusClass(trade.status)]) },
        });
        (__VLS_ctx.statusLabel(trade.status));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
        (__VLS_ctx.time(trade.completedAt || trade.updatedAt));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (trade.name || trade.marketHashName);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (trade.tradeNo);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "stop-line" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.steps[trade.stepIndex]?.label || trade.stepKey);
        if (trade.acknowledged) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        (__VLS_ctx.reason(trade));
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
    ...{ class: "list-pagination" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.turn(-1);
        } },
    type: "button",
    disabled: (__VLS_ctx.page <= 1),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.page);
(__VLS_ctx.totalPages);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.turn(1);
        } },
    type: "button",
    disabled: (__VLS_ctx.page >= __VLS_ctx.totalPages),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "detail panel" },
});
if (!__VLS_ctx.selected) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty large" },
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "detail-head" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "detail-badges" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: (['status', __VLS_ctx.statusClass(__VLS_ctx.selected.status)]) },
    });
    (__VLS_ctx.statusLabel(__VLS_ctx.selected.status));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.selected.stepIndex + 1);
    if (__VLS_ctx.selected.acknowledged) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.selected.name || __VLS_ctx.selected.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (__VLS_ctx.selected.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.selected.tradeNo);
    const __VLS_3 = {}.RouterLink;
    /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
    // @ts-ignore
    const __VLS_4 = __VLS_asFunctionalComponent(__VLS_3, new __VLS_3({
        to: ({ path: '/profit-trade/logs', query: { tradeNo: __VLS_ctx.selected.tradeNo } }),
        ...{ class: "log-link" },
    }));
    const __VLS_5 = __VLS_4({
        to: ({ path: '/profit-trade/logs', query: { tradeNo: __VLS_ctx.selected.tradeNo } }),
        ...{ class: "log-link" },
    }, ...__VLS_functionalComponentArgsRest(__VLS_4));
    __VLS_6.slots.default;
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_7 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "link",
        size: (15),
    }));
    const __VLS_8 = __VLS_7({
        name: "link",
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_7));
    var __VLS_6;
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "process" },
    });
    for (const [step] of __VLS_getVForSourceType((__VLS_ctx.steps))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (step.key),
            ...{ class: (step.index < __VLS_ctx.selected.stepIndex ? 'done' : step.index === __VLS_ctx.selected.stepIndex ? 'stopped' : 'pending') },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (step.index + 1);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (step.label);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (step.index < __VLS_ctx.selected.stepIndex ? "已完成" : step.index === __VLS_ctx.selected.stepIndex ? "停止位置" : "未开始");
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "stop-evidence" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.reason(__VLS_ctx.selected));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.cancelSource || __VLS_ctx.note(__VLS_ctx.selected, "cancelSource"));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.time(__VLS_ctx.selected.completedAt || __VLS_ctx.selected.updatedAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.aAssetId || "-");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.aSteamId || "-");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.bAssetId || "未获得");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.selected.steamBuyPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.pct(__VLS_ctx.selected.expectedRoiPct));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.evidenceLabel(__VLS_ctx.selected, "listingIdObtained"));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.evidenceLabel(__VLS_ctx.selected, "purchaseRequestSent"));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "timeline" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "section-heading" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.timeline.length);
    if (__VLS_ctx.timelineError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "page-error" },
        });
        (__VLS_ctx.timelineError);
    }
    if (__VLS_ctx.timelineLoading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "empty" },
        });
    }
    else if (__VLS_ctx.timeline.length === 0) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "empty" },
        });
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.ol, __VLS_intrinsicElements.ol)({});
        for (const [event, index] of __VLS_getVForSourceType((__VLS_ctx.timeline))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
                key: (event.id || index),
                ...{ class: ({ snapshot: event.isSnapshot }) },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (event.eventType || (event.isSnapshot ? "历史快照" : "状态迁移"));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
            (__VLS_ctx.time(event.createdAt));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
            (event.statusFrom || "-");
            (event.statusTo || "-");
            (event.stepKeyFrom || "-");
            (event.stepKeyTo || "-");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (event.reason || "未记录补充原因");
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "acknowledge" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    (__VLS_ctx.selected.acknowledged ? "恢复问题记录" : "知晓并隐藏");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    if (!__VLS_ctx.selected.acknowledged) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
            value: (__VLS_ctx.acknowledgeReason),
            type: "text",
            placeholder: "知晓原因（可选）",
        });
    }
    if (__VLS_ctx.selected.acknowledged) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(!__VLS_ctx.selected))
                        return;
                    if (!(__VLS_ctx.selected.acknowledged))
                        return;
                    __VLS_ctx.setAcknowledged('restore');
                } },
            ...{ class: "secondary-button" },
            type: "button",
            disabled: (__VLS_ctx.actionBusy),
        });
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(!__VLS_ctx.selected))
                        return;
                    if (!!(__VLS_ctx.selected.acknowledged))
                        return;
                    __VLS_ctx.setAcknowledged('acknowledge');
                } },
            ...{ class: "secondary-button danger" },
            type: "button",
            disabled: (__VLS_ctx.actionBusy),
        });
    }
}
/** @type {__VLS_StyleScopedClasses['interruptions-page']} */ ;
/** @type {__VLS_StyleScopedClasses['page-title']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['step-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['keyword']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['page-error']} */ ;
/** @type {__VLS_StyleScopedClasses['action-message']} */ ;
/** @type {__VLS_StyleScopedClasses['global-message']} */ ;
/** @type {__VLS_StyleScopedClasses['master-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-list']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-item-head']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-line']} */ ;
/** @type {__VLS_StyleScopedClasses['list-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['detail']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['large']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-badges']} */ ;
/** @type {__VLS_StyleScopedClasses['log-link']} */ ;
/** @type {__VLS_StyleScopedClasses['process']} */ ;
/** @type {__VLS_StyleScopedClasses['stop-evidence']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['section-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['page-error']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['acknowledge']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['danger']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            FolioIcon: FolioIcon,
            steps: steps,
            rows: rows,
            total: total,
            page: page,
            loading: loading,
            error: error,
            keywordDraft: keywordDraft,
            from: from,
            to: to,
            stepKey: stepKey,
            status: status,
            acknowledged: acknowledged,
            selected: selected,
            timeline: timeline,
            timelineLoading: timelineLoading,
            timelineError: timelineError,
            acknowledgeReason: acknowledgeReason,
            actionBusy: actionBusy,
            actionMessage: actionMessage,
            totalPages: totalPages,
            stepCount: stepCount,
            statusLabel: statusLabel,
            statusClass: statusClass,
            time: time,
            pct: pct,
            money: money,
            reason: reason,
            note: note,
            evidenceLabel: evidenceLabel,
            load: load,
            search: search,
            reset: reset,
            turn: turn,
            selectTrade: selectTrade,
            setAcknowledged: setAcknowledged,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
