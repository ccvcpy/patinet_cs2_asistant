import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from "vue";
import { RouterLink } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatLocal, responseError, unwrapPayload } from "./guadao_shared";
const issues = ref([]);
const summary = ref({});
const runtime = ref({});
const selectedId = ref(null);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const statusFilter = ref("");
const severityFilter = ref("");
const accountFilter = ref("");
const typeFilter = ref("");
const keyword = ref("");
const showAcknowledged = ref(false);
const ackOpen = ref(false);
const ackReason = ref("");
const reviewOpen = ref(false);
const reviewBusy = ref(false);
const notice = ref("");
let timer = null;
const filtered = computed(() => issues.value.filter(issue => {
    if (!showAcknowledged.value && issue.acknowledged)
        return false;
    if (statusFilter.value && issue.status !== statusFilter.value)
        return false;
    if (severityFilter.value && issue.severity !== severityFilter.value)
        return false;
    if (accountFilter.value && issue.accountName !== accountFilter.value)
        return false;
    if (typeFilter.value && issue.issueType !== typeFilter.value)
        return false;
    const haystack = `${issue.title || ""} ${issue.summary || ""} ${issue.marketHashName || ""} ${issue.accountName || ""}`.toLowerCase();
    return !keyword.value.trim() || haystack.includes(keyword.value.trim().toLowerCase());
}));
const selected = computed(() => filtered.value.find(issue => issue.id === selectedId.value) || filtered.value[0] || null);
const accounts = computed(() => [...new Set(issues.value.map(row => row.accountName).filter(Boolean))]);
const types = computed(() => [...new Set(issues.value.map(row => row.issueType).filter(Boolean))]);
const metrics = computed(() => [["全部问题", summary.value.total ?? issues.value.filter(i => !i.acknowledged).length], ["待安全复核", summary.value.pendingReview ?? 0], ["Steam 待处理", summary.value.steam ?? 0], ["C5 待处理", summary.value.c5 ?? 0], ["本地状态异常", summary.value.local ?? 0]]);
const runtimeText = computed(() => { const value = String(runtime.value?.runtimeStatus || runtime.value?.status || ""); if (value === "closing_only")
    return "存量闭环中"; if (value === "preparing")
    return "启动准备中"; return runtime.value?.enabled ? "运行中" : "已关闭"; });
const canQueueSafeReview = computed(() => Boolean(selected.value?.canQueueSafeReview));
function severityText(value) { return value === "high" || value === "critical" ? "高" : value === "medium" ? "中" : value === "low" ? "低" : value || "未分级"; }
function normalizeIssue(raw) {
    const status = String(raw.status || "manual_required");
    const titleByStatus = { manual_required: "需要人工安全复核", listing_failed: "Steam 上架状态异常", failed: "挂刀流水执行失败" };
    const fallbackEvidence = [
        ["operationId", raw.operationId], ["assetId", raw.assetId], ["listingId", raw.listingId], ["SteamID", raw.steamId],
    ].filter((row) => row[1] != null && String(row[1]).trim()).map((row) => ({ label: String(row[0]), value: String(row[1]) }));
    const evidence = Array.isArray(raw.evidence) ? raw.evidence : fallbackEvidence;
    const timeline = Array.isArray(raw.timeline)
        ? raw.timeline
        : raw.createdAt
            ? [{ at: String(raw.createdAt), label: "问题进入待处理", detail: String(raw.reason || status) }]
            : [];
    return {
        id: String(raw.issueId || raw.id || raw.operationId || ""),
        issueType: String(raw.issueType || status),
        title: String(raw.title || titleByStatus[status] || status),
        severity: String(raw.severity || (status === "manual_required" ? "high" : "medium")),
        status,
        accountName: String(raw.accountName || raw.accountId || "") || null,
        marketHashName: String(raw.marketHashName || raw.nameCn || "") || null,
        summary: String(raw.summary || raw.reason || "") || null,
        detail: String(raw.detail || raw.reason || "") || null,
        firstSeenAt: String(raw.firstSeenAt || raw.createdAt || "") || null,
        lastSeenAt: String(raw.lastSeenAt || raw.createdAt || "") || null,
        repeatCount: Number(raw.repeatCount || 1),
        acknowledged: Boolean(raw.acknowledged),
        evidence,
        timeline,
        recommendation: typeof raw.recommendation === "string" ? raw.recommendation : null,
        accountId: String(raw.accountId || "") || null,
        operationId: raw.operationId ?? null,
        assetId: String(raw.assetId || "") || null,
        listingId: String(raw.listingId || "") || null,
        steamId: String(raw.steamId || "") || null,
        category: String(raw.category || "") || null,
        rawStatus: String(raw.rawStatus || "") || null,
        canQueueSafeReview: Boolean(raw.canQueueSafeReview),
        safeReviewBlockReason: String(raw.safeReviewBlockReason || "") || null,
    };
}
async function refresh() { loading.value = true; try {
    const response = await fetch("/api/guadao/issues?acknowledged=all", { cache: "no-store" });
    if (!response.ok)
        throw new Error(await responseError(response));
    const data = unwrapPayload(await response.json());
    const rows = (data.items || data.issues || []);
    issues.value = rows.map(normalizeIssue);
    summary.value = data.summary || {};
    runtime.value = data.runtime || {};
    if (selectedId.value == null || !issues.value.some(i => i.id === selectedId.value))
        selectedId.value = issues.value[0]?.id ?? null;
    error.value = "";
}
catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
}
finally {
    loading.value = false;
} }
async function acknowledge(value) { if (!selected.value)
    return; busy.value = true; try {
    const response = await fetch("/api/guadao/issues/ack", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issueId: selected.value.id, acknowledged: value, reason: ackReason.value.trim() || null }) });
    if (!response.ok)
        throw new Error(await responseError(response));
    ackOpen.value = false;
    ackReason.value = "";
    await refresh();
}
catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
}
finally {
    busy.value = false;
} }
function relatedLogsTo(issue) { return { path: "/guadao/logs", query: { operationId: String(issue.operationId || issue.id), marketHashName: issue.marketHashName || "", account: issue.accountName || "" } }; }
async function confirmSafeReview() { if (!selected.value)
    return; reviewBusy.value = true; notice.value = ""; try {
    const response = await fetch("/api/guadao/issues/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issueId: selected.value.id }) });
    if (!response.ok)
        throw new Error(await responseError(response));
    const payload = await response.json();
    reviewOpen.value = false;
    notice.value = payload.message || "安全复核已进入统一到期任务队列。";
    await refresh();
}
catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
}
finally {
    reviewBusy.value = false;
} }
function startPolling() { if (timer === null)
    timer = setInterval(() => void refresh(), 15000); }
function stopPolling() { if (timer !== null)
    clearInterval(timer); timer = null; }
onMounted(() => { void refresh(); startPolling(); });
onActivated(startPolling);
onDeactivated(stopPolling);
onUnmounted(stopPolling);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['issues-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['executor-state']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-filter']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-filter']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['high']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['critical']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['executor-state']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-actions']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page issues-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "issues-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
const __VLS_0 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    ...{ class: "executor-state" },
    to: "/guadao/overview",
}));
const __VLS_2 = __VLS_1({
    ...{ class: "executor-state" },
    to: "/guadao/overview",
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
__VLS_3.slots.default;
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.runtimeText);
var __VLS_3;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "policy-banner" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_4 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "success",
    size: (15),
}));
const __VLS_5 = __VLS_4({
    name: "success",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_4));
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "api-error" },
    });
    (__VLS_ctx.error);
}
else if (__VLS_ctx.notice) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "review-notice" },
    });
    (__VLS_ctx.notice);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "issue-metrics" },
});
for (const [metric] of __VLS_getVForSourceType((__VLS_ctx.metrics))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        key: (String(metric[0])),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (metric[0]);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (metric[1]);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "issue-filters" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.statusFilter),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "open",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "monitoring",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.severityFilter),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "critical",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "high",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "medium",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "low",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.accountFilter),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
for (const [name] of __VLS_getVForSourceType((__VLS_ctx.accounts))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        key: (name),
        value: (name),
    });
    (name);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.typeFilter),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
for (const [type] of __VLS_getVForSourceType((__VLS_ctx.types))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        key: (type),
        value: (type),
    });
    (type);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "ack-filter" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "checkbox",
});
(__VLS_ctx.showAcknowledged);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "keyword" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    placeholder: "订单号 / 物品名 / 账号 / 消息",
});
(__VLS_ctx.keyword);
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "issue-workbench" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "issue-list" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
(__VLS_ctx.filtered.length);
for (const [issue] of __VLS_getVForSourceType((__VLS_ctx.filtered))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.selectedId = issue.id;
            } },
        key: (issue.id),
        ...{ class: (['issue-card', issue.severity, { selected: __VLS_ctx.selected?.id === issue.id, acknowledged: issue.acknowledged }]) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "issue-card-head" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "issue-icon" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_7 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (issue.severity === 'critical' || issue.severity === 'high' ? 'shield' : 'warning'),
        size: (18),
    }));
    const __VLS_8 = __VLS_7({
        name: (issue.severity === 'critical' || issue.severity === 'high' ? 'shield' : 'warning'),
        size: (18),
    }, ...__VLS_functionalComponentArgsRest(__VLS_7));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (issue.title || issue.issueType || "未命名问题");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.severityText(issue.severity));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (issue.marketHashName || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (issue.accountName || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (issue.summary || issue.detail || "后端未提供问题摘要。");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.formatLocal(issue.firstSeenAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.formatLocal(issue.lastSeenAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (issue.repeatCount || 1);
}
if (!__VLS_ctx.filtered.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
    (__VLS_ctx.loading ? "正在读取异常…" : "当前筛选没有需要人工处理的问题。");
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
    ...{ class: "panel issue-detail" },
});
if (__VLS_ctx.selected) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "issue-icon" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_10 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "shield",
        size: (17),
    }));
    const __VLS_11 = __VLS_10({
        name: "shield",
        size: (17),
    }, ...__VLS_functionalComponentArgsRest(__VLS_10));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.selected.title || __VLS_ctx.selected.issueType);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.severityText(__VLS_ctx.selected.severity));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    if (__VLS_ctx.selected.evidence?.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.ul, __VLS_intrinsicElements.ul)({});
        for (const [row, index] of __VLS_getVForSourceType((__VLS_ctx.selected.evidence))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
                key: (index),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (row.label || "证据");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (row.value || "—");
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        (__VLS_ctx.selected.detail || __VLS_ctx.selected.summary || "后端暂未提供证据详情。");
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    if (__VLS_ctx.selected.timeline?.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "issue-timeline" },
        });
        for (const [event, index] of __VLS_getVForSourceType((__VLS_ctx.selected.timeline))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
                key: (index),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (event.label || "状态更新");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (event.detail);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
            (__VLS_ctx.formatLocal(event.at));
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "recommend" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (__VLS_ctx.selected.recommendation || "请结合远端 Steam/C5 终态证据完成安全复核，避免误推进或重复交易。");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "detail-actions" },
    });
    if (__VLS_ctx.canQueueSafeReview) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    if (!(__VLS_ctx.canQueueSafeReview))
                        return;
                    __VLS_ctx.reviewOpen = true;
                } },
            ...{ class: "primary-button" },
            type: "button",
        });
    }
    const __VLS_13 = {}.RouterLink;
    /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
    // @ts-ignore
    const __VLS_14 = __VLS_asFunctionalComponent(__VLS_13, new __VLS_13({
        ...{ class: "secondary-button" },
        to: (__VLS_ctx.relatedLogsTo(__VLS_ctx.selected)),
    }));
    const __VLS_15 = __VLS_14({
        ...{ class: "secondary-button" },
        to: (__VLS_ctx.relatedLogsTo(__VLS_ctx.selected)),
    }, ...__VLS_functionalComponentArgsRest(__VLS_14));
    __VLS_16.slots.default;
    var __VLS_16;
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.ackOpen = true;
            } },
        ...{ class: "secondary-button" },
        type: "button",
    });
    (__VLS_ctx.selected.acknowledged ? "修改知晓记录" : "知晓并隐藏");
    if (!__VLS_ctx.canQueueSafeReview) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "ack-note" },
        });
        (__VLS_ctx.selected.safeReviewBlockReason || "该问题不能自动发起 Steam 复核，请按推荐操作核对远端终态证据。");
    }
    if (__VLS_ctx.selected.acknowledged) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "ack-note" },
        });
    }
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
}
if (__VLS_ctx.ackOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.ackOpen))
                    return;
                __VLS_ctx.ackOpen = false;
            } },
        ...{ class: "modal-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "ack-dialog" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.selected?.acknowledged ? "更新知晓状态" : "知晓并隐藏问题");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.textarea, __VLS_intrinsicElements.textarea)({
        value: (__VLS_ctx.ackReason),
        rows: "3",
        placeholder: "记录判断依据，便于后续审计",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.ackOpen))
                    return;
                __VLS_ctx.ackOpen = false;
            } },
        ...{ class: "secondary-button" },
    });
    if (__VLS_ctx.selected?.acknowledged) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.ackOpen))
                        return;
                    if (!(__VLS_ctx.selected?.acknowledged))
                        return;
                    __VLS_ctx.acknowledge(false);
                } },
            ...{ class: "secondary-button" },
            disabled: (__VLS_ctx.busy),
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.ackOpen))
                    return;
                __VLS_ctx.acknowledge(true);
            } },
        ...{ class: "primary-button" },
        disabled: (__VLS_ctx.busy),
    });
    (__VLS_ctx.busy ? "保存中…" : "确认知晓");
}
if (__VLS_ctx.reviewOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.reviewOpen))
                    return;
                __VLS_ctx.reviewOpen = false;
            } },
        ...{ class: "modal-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "ack-dialog" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.reviewOpen))
                    return;
                __VLS_ctx.reviewOpen = false;
            } },
        ...{ class: "secondary-button" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.confirmSafeReview) },
        ...{ class: "primary-button" },
        disabled: (__VLS_ctx.reviewBusy),
    });
    (__VLS_ctx.reviewBusy ? "排队中…" : "确认排队复核");
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['issues-page']} */ ;
/** @type {__VLS_StyleScopedClasses['issues-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['executor-state']} */ ;
/** @type {__VLS_StyleScopedClasses['policy-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['api-error']} */ ;
/** @type {__VLS_StyleScopedClasses['review-notice']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-filter']} */ ;
/** @type {__VLS_StyleScopedClasses['keyword']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-workbench']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-list']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['issue-timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['recommend']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-note']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-note']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['ack-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            FolioIcon: FolioIcon,
            formatLocal: formatLocal,
            selectedId: selectedId,
            loading: loading,
            busy: busy,
            error: error,
            statusFilter: statusFilter,
            severityFilter: severityFilter,
            accountFilter: accountFilter,
            typeFilter: typeFilter,
            keyword: keyword,
            showAcknowledged: showAcknowledged,
            ackOpen: ackOpen,
            ackReason: ackReason,
            reviewOpen: reviewOpen,
            reviewBusy: reviewBusy,
            notice: notice,
            filtered: filtered,
            selected: selected,
            accounts: accounts,
            types: types,
            metrics: metrics,
            runtimeText: runtimeText,
            canQueueSafeReview: canQueueSafeReview,
            severityText: severityText,
            acknowledge: acknowledge,
            relatedLogsTo: relatedLogsTo,
            confirmSafeReview: confirmSafeReview,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
