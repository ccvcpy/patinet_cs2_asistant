import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
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
import { formatClock, formatDuration, recommendedRatio, } from "../components/case-monitor/format";
import "../components/case-monitor/case-monitor.css";
const typeOrder = ["weapon_case", "capsule", "souvenir_package"];
const typeLabels = {
    weapon_case: "武器箱",
    capsule: "胶囊",
    souvenir_package: "纪念包",
};
const reportWindows = [
    { value: "24", label: "24小时" },
    { value: "168", label: "7天" },
    { value: "720", label: "30天" },
    { value: "custom", label: "自定义" },
];
const report = ref(null);
const runtime = ref(null);
const loading = ref(true);
const reportError = ref("");
const statusError = ref("");
const action = ref("");
const intervalMinutes = ref(5);
const reportWindow = ref("24");
const refreshLiquidity = ref(true);
const customStart = ref("");
const customEnd = ref("");
const keyword = ref("");
const typeFilter = ref("all");
const currentPage = ref(1);
const pageSize = ref(10);
const selectedName = ref("");
const drawerItem = ref(null);
const notice = ref(null);
const initializedStatus = ref(false);
const lastSeenCompletedJob = ref("");
let pollTimer;
async function readJson(url, init) {
    const response = await fetch(url, {
        cache: "no-store",
        ...init,
        headers: {
            "Content-Type": "application/json",
            ...(init?.headers || {}),
        },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(String(payload.error || `HTTP ${response.status}`));
    }
    return payload;
}
async function loadReport() {
    reportError.value = "";
    try {
        const payload = await readJson("/api/case-monitor/report/latest");
        report.value = payload.report;
        return;
    }
    catch (apiError) {
        try {
            const response = await fetch("/guadao_case_ratio_report.json", { cache: "no-store" });
            if (!response.ok)
                throw new Error(`HTTP ${response.status}`);
            report.value = await response.json();
        }
        catch {
            report.value = null;
            reportError.value = apiError instanceof Error ? apiError.message : String(apiError);
        }
    }
}
function maybeHandleCompletedJob(job) {
    if (!job || job.status !== "completed")
        return;
    if (!initializedStatus.value) {
        lastSeenCompletedJob.value = job.jobId;
        return;
    }
    if (lastSeenCompletedJob.value === job.jobId)
        return;
    lastSeenCompletedJob.value = job.jobId;
    if (job.jobType === "report") {
        void loadReport();
        notice.value = {
            tone: "success",
            message: "全量箱子报告已生成，网页数据已刷新。",
            showExports: true,
        };
    }
    else {
        const okCount = Number(job.result?.okCount || 0);
        const missingCount = Number(job.result?.missingC5Count || 0) +
            Number(job.result?.missingSteamCount || 0);
        notice.value = {
            tone: "success",
            message: `采集完成：成功 ${okCount}，缺价 ${missingCount}`,
        };
    }
}
async function loadStatus() {
    try {
        const payload = await readJson("/api/case-monitor/status");
        runtime.value = payload;
        statusError.value = "";
        if (!payload.runtime.enabled || !initializedStatus.value) {
            intervalMinutes.value = payload.runtime.intervalMinutes || intervalMinutes.value;
        }
        maybeHandleCompletedJob(payload.latestJob);
        initializedStatus.value = true;
    }
    catch (error) {
        runtime.value = null;
        statusError.value = error instanceof Error ? error.message : String(error);
        initializedStatus.value = true;
    }
}
async function postAction(name, path, body = {}) {
    action.value = name;
    try {
        const payload = await readJson(path, {
            method: "POST",
            body: JSON.stringify(body),
        });
        await loadStatus();
        return payload;
    }
    catch (error) {
        notice.value = {
            tone: "error",
            message: error instanceof Error ? error.message : String(error),
        };
        throw error;
    }
    finally {
        action.value = "";
    }
}
async function collectOnce() {
    try {
        await postAction("collect", "/api/case-monitor/collect");
        notice.value = { tone: "success", message: "采集任务已进入后台队列。" };
    }
    catch {
        // The shared action handler already exposes the error.
    }
}
async function generateReport() {
    const body = {
        refreshLiquidity: refreshLiquidity.value,
    };
    if (reportWindow.value === "custom") {
        if (!customStart.value || !customEnd.value) {
            notice.value = { tone: "error", message: "自定义报告窗口需要同时选择开始和结束时间。" };
            return;
        }
        body.dateFrom = new Date(customStart.value).toISOString();
        body.dateTo = new Date(customEnd.value).toISOString();
    }
    else {
        body.hours = Number(reportWindow.value);
    }
    try {
        await postAction("report", "/api/case-monitor/report", body);
        notice.value = { tone: "success", message: "全量箱子报告正在后台生成。" };
    }
    catch {
        // The shared action handler already exposes the error.
    }
}
async function toggleMonitor() {
    const enabled = runtime.value?.runtime.enabled ?? false;
    try {
        if (enabled) {
            await postAction("pause", "/api/case-monitor/pause");
            notice.value = { tone: "success", message: "监控已暂停，当前任务会安全完成。" };
        }
        else {
            await postAction("start", "/api/case-monitor/start", {
                intervalMinutes: intervalMinutes.value,
            });
            notice.value = {
                tone: "success",
                message: `监控已启动，每 ${intervalMinutes.value} 分钟采集一次。`,
            };
        }
    }
    catch {
        // The shared action handler already exposes the error.
    }
}
const runtimeStatus = computed(() => {
    if (statusError.value)
        return "offline";
    const current = runtime.value?.currentJob;
    if (current?.status === "running" || current?.status === "queued") {
        return current.jobType === "report" ? "reporting" : "collecting";
    }
    return runtime.value?.runtime.enabled ? "running" : "paused";
});
const runtimeLabel = computed(() => {
    const job = runtime.value?.currentJob;
    if (job?.status === "running" || job?.status === "queued") {
        const total = Number(job.progressTotal || 0);
        const progress = total > 0 ? ` ${job.progressCurrent}/${total}` : "";
        return job.jobType === "report" ? `正在生成报告${progress}` : `正在采集${progress}`;
    }
    if (statusError.value)
        return "后端离线";
    return runtime.value?.runtime.enabled ? "监控运行中" : "监控已暂停";
});
const busy = computed(() => Boolean(runtime.value?.runtime.busy || action.value));
const categoryOptions = computed(() => {
    const counts = report.value?.crateTypeCounts || {};
    const options = typeOrder.map((key) => ({
        key,
        label: typeLabels[key],
        count: Number(counts[key] || 0),
    }));
    const known = options.reduce((sum, option) => sum + option.count, 0);
    const all = Number(report.value?.itemCount || 0);
    return [
        { key: "all", label: "全部", count: all },
        ...options,
        { key: "other", label: "其他箱类", count: Math.max(0, all - known) },
    ];
});
const filteredItems = computed(() => {
    const source = report.value?.items || [];
    const needle = keyword.value.trim().toLocaleLowerCase("zh-CN");
    return source.filter((item) => {
        const categoryMatched = typeFilter.value === "all" ||
            (typeFilter.value === "other"
                ? !typeOrder.includes(item.crateType)
                : item.crateType === typeFilter.value);
        const text = `${item.marketHashName} ${item.name || ""}`.toLocaleLowerCase("zh-CN");
        return categoryMatched && (!needle || text.includes(needle));
    });
});
const rankedItems = computed(() => [...filteredItems.value].sort((left, right) => {
    const leftRatio = recommendedRatio(left);
    const rightRatio = recommendedRatio(right);
    const leftSane = leftRatio >= 0.3 && leftRatio <= 0.95 ? 1 : 0;
    const rightSane = rightRatio >= 0.3 && rightRatio <= 0.95 ? 1 : 0;
    return (rightSane - leftSane ||
        Number(right.recommendationScore || 0) - Number(left.recommendationScore || 0) ||
        Number(right.steamVolume24h || 0) - Number(left.steamVolume24h || 0) ||
        left.marketHashName.localeCompare(right.marketHashName));
}));
const totalPages = computed(() => Math.max(1, Math.ceil(rankedItems.value.length / pageSize.value)));
const pagedItems = computed(() => {
    const start = (currentPage.value - 1) * pageSize.value;
    return rankedItems.value.slice(start, start + pageSize.value);
});
const validSnapshots = computed(() => Number(report.value?.statusCounts?.ok || 0));
const averageCoverage = computed(() => {
    const items = report.value?.items || [];
    if (!items.length)
        return 0;
    return items.reduce((sum, item) => sum + Number(item.coveragePct || 0), 0) / items.length;
});
const lastCollection = computed(() => runtime.value?.runtime.lastCollectionResult || {});
const missingCount = computed(() => Number(lastCollection.value.missingC5Count || 0) +
    Number(lastCollection.value.missingSteamCount || 0));
function openDetail(item) {
    selectedName.value = item.marketHashName;
    drawerItem.value = item;
}
function chooseCategory(value) {
    typeFilter.value = value;
    currentPage.value = 1;
    selectedName.value = "";
}
function exportUrl(format) {
    const reportId = runtime.value?.latestReport.reportId;
    const query = new URLSearchParams({ format });
    if (reportId)
        query.set("reportId", reportId);
    return `/api/case-monitor/report/export?${query.toString()}`;
}
watch(keyword, () => {
    currentPage.value = 1;
    selectedName.value = "";
});
watch(pageSize, () => {
    currentPage.value = 1;
});
watch(rankedItems, () => {
    currentPage.value = Math.min(currentPage.value, totalPages.value);
    if (!selectedName.value && rankedItems.value.length) {
        selectedName.value = rankedItems.value[0].marketHashName;
    }
}, { immediate: true });
onMounted(async () => {
    await Promise.all([loadStatus(), loadReport()]);
    loading.value = false;
    pollTimer = window.setInterval(() => void loadStatus(), 2500);
});
onBeforeUnmount(() => {
    if (pollTimer !== undefined)
        window.clearInterval(pollTimer);
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['case-monitor-title']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-title']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['case-custom-range']} */ ;
/** @type {__VLS_StyleScopedClasses['case-time-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-ranking-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['case-table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['case-table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-header']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-safety']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-controls']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-ranking-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['case-table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-header']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-title']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon']} */ ;
/** @type {__VLS_StyleScopedClasses['case-ranking-toolbar']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "cm-surface case-monitor-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "case-monitor-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-monitor-title" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
/** @type {[typeof CaseMonitorStatusChip, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(CaseMonitorStatusChip, new CaseMonitorStatusChip({
    status: (__VLS_ctx.runtimeStatus),
    label: (__VLS_ctx.runtimeLabel),
}));
const __VLS_1 = __VLS_0({
    status: (__VLS_ctx.runtimeStatus),
    label: (__VLS_ctx.runtimeLabel),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "case-monitor-safety" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-monitor-actions" },
});
/** @type {[typeof CaseMonitorButton, typeof CaseMonitorButton, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(CaseMonitorButton, new CaseMonitorButton({
    ...{ 'onClick': {} },
    tone: "primary",
    icon: "refresh",
    loading: (__VLS_ctx.action === 'collect'),
    disabled: (__VLS_ctx.busy),
}));
const __VLS_4 = __VLS_3({
    ...{ 'onClick': {} },
    tone: "primary",
    icon: "refresh",
    loading: (__VLS_ctx.action === 'collect'),
    disabled: (__VLS_ctx.busy),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
let __VLS_6;
let __VLS_7;
let __VLS_8;
const __VLS_9 = {
    onClick: (__VLS_ctx.collectOnce)
};
__VLS_5.slots.default;
var __VLS_5;
/** @type {[typeof CaseMonitorButton, typeof CaseMonitorButton, ]} */ ;
// @ts-ignore
const __VLS_10 = __VLS_asFunctionalComponent(CaseMonitorButton, new CaseMonitorButton({
    ...{ 'onClick': {} },
    tone: "primary",
    icon: "download",
    loading: (__VLS_ctx.action === 'report'),
    disabled: (__VLS_ctx.busy),
}));
const __VLS_11 = __VLS_10({
    ...{ 'onClick': {} },
    tone: "primary",
    icon: "download",
    loading: (__VLS_ctx.action === 'report'),
    disabled: (__VLS_ctx.busy),
}, ...__VLS_functionalComponentArgsRest(__VLS_10));
let __VLS_13;
let __VLS_14;
let __VLS_15;
const __VLS_16 = {
    onClick: (__VLS_ctx.generateReport)
};
__VLS_12.slots.default;
var __VLS_12;
/** @type {[typeof CaseMonitorButton, typeof CaseMonitorButton, ]} */ ;
// @ts-ignore
const __VLS_17 = __VLS_asFunctionalComponent(CaseMonitorButton, new CaseMonitorButton({
    ...{ 'onClick': {} },
    tone: (__VLS_ctx.runtime?.runtime.enabled ? 'quiet' : 'success'),
    icon: (__VLS_ctx.runtime?.runtime.enabled ? 'pause' : 'play'),
    loading: (__VLS_ctx.action === 'pause' || __VLS_ctx.action === 'start'),
    disabled: (Boolean(__VLS_ctx.runtime?.runtime.busy)),
}));
const __VLS_18 = __VLS_17({
    ...{ 'onClick': {} },
    tone: (__VLS_ctx.runtime?.runtime.enabled ? 'quiet' : 'success'),
    icon: (__VLS_ctx.runtime?.runtime.enabled ? 'pause' : 'play'),
    loading: (__VLS_ctx.action === 'pause' || __VLS_ctx.action === 'start'),
    disabled: (Boolean(__VLS_ctx.runtime?.runtime.busy)),
}, ...__VLS_functionalComponentArgsRest(__VLS_17));
let __VLS_20;
let __VLS_21;
let __VLS_22;
const __VLS_23 = {
    onClick: (__VLS_ctx.toggleMonitor)
};
__VLS_19.slots.default;
(__VLS_ctx.runtime?.runtime.enabled ? "暂停监控" : "开始监控");
var __VLS_19;
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "case-monitor-controls" },
    'aria-label': "监控与报告控制",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-control-group" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "case-control-label" },
});
/** @type {[typeof CaseMonitorIntervalPicker, ]} */ ;
// @ts-ignore
const __VLS_24 = __VLS_asFunctionalComponent(CaseMonitorIntervalPicker, new CaseMonitorIntervalPicker({
    modelValue: (__VLS_ctx.intervalMinutes),
    disabled: (Boolean(__VLS_ctx.runtime?.runtime.enabled || __VLS_ctx.busy)),
}));
const __VLS_25 = __VLS_24({
    modelValue: (__VLS_ctx.intervalMinutes),
    disabled: (Boolean(__VLS_ctx.runtime?.runtime.enabled || __VLS_ctx.busy)),
}, ...__VLS_functionalComponentArgsRest(__VLS_24));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
    ...{ class: "case-control-divider" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-control-group" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "case-control-label" },
});
/** @type {[typeof CaseMonitorSegmented, ]} */ ;
// @ts-ignore
const __VLS_27 = __VLS_asFunctionalComponent(CaseMonitorSegmented, new CaseMonitorSegmented({
    modelValue: (__VLS_ctx.reportWindow),
    options: (__VLS_ctx.reportWindows),
    disabled: (__VLS_ctx.busy),
}));
const __VLS_28 = __VLS_27({
    modelValue: (__VLS_ctx.reportWindow),
    options: (__VLS_ctx.reportWindows),
    disabled: (__VLS_ctx.busy),
}, ...__VLS_functionalComponentArgsRest(__VLS_27));
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_30 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "calendar",
    size: (14),
    ...{ class: "case-custom-calendar" },
}));
const __VLS_31 = __VLS_30({
    name: "calendar",
    size: (14),
    ...{ class: "case-custom-calendar" },
}, ...__VLS_functionalComponentArgsRest(__VLS_30));
if (__VLS_ctx.reportWindow === 'custom') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "case-custom-range" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        type: "datetime-local",
        'aria-label': "报告开始时间",
    });
    (__VLS_ctx.customStart);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        type: "datetime-local",
        'aria-label': "报告结束时间",
    });
    (__VLS_ctx.customEnd);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
    ...{ class: "case-control-divider" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-control-group" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "case-control-label" },
});
/** @type {[typeof CaseMonitorToggle, ]} */ ;
// @ts-ignore
const __VLS_33 = __VLS_asFunctionalComponent(CaseMonitorToggle, new CaseMonitorToggle({
    modelValue: (__VLS_ctx.refreshLiquidity),
    disabled: (__VLS_ctx.busy),
}));
const __VLS_34 = __VLS_33({
    modelValue: (__VLS_ctx.refreshLiquidity),
    disabled: (__VLS_ctx.busy),
}, ...__VLS_functionalComponentArgsRest(__VLS_33));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
    ...{ class: "case-control-divider" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-cycle-summary" },
});
(__VLS_ctx.lastCollection.targetCount || 0);
(__VLS_ctx.lastCollection.okCount || 0);
(__VLS_ctx.lastCollection.missingC5Count || 0);
(__VLS_ctx.lastCollection.missingSteamCount || 0);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
    ...{ class: "case-control-divider" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-time-stat" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formatClock(__VLS_ctx.runtime?.runtime.lastCollectionAt));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
    ...{ class: "case-control-divider" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-time-stat" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formatClock(__VLS_ctx.runtime?.runtime.nextRunAt));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
    ...{ class: "case-control-divider" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-time-stat" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formatClock(__VLS_ctx.runtime?.runtime.lastReportAt || __VLS_ctx.report?.generatedAt));
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "case-monitor-metrics" },
    'aria-label': "监控概览",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "case-metric__icon case-metric__icon--green" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_36 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "shield",
    size: (31),
}));
const __VLS_37 = __VLS_36({
    name: "shield",
    size: (31),
}, ...__VLS_functionalComponentArgsRest(__VLS_36));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.validSnapshots.toLocaleString("zh-CN"));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "case-metric__icon case-metric__icon--blue" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_39 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "case",
    size: (31),
}));
const __VLS_40 = __VLS_39({
    name: "case",
    size: (31),
}, ...__VLS_functionalComponentArgsRest(__VLS_39));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
    ...{ class: "is-blue" },
});
(__VLS_ctx.report?.itemCount || 0);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "case-metric__icon case-metric__icon--amber" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_42 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "report",
    size: (31),
}));
const __VLS_43 = __VLS_42({
    name: "report",
    size: (31),
}, ...__VLS_functionalComponentArgsRest(__VLS_42));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
    ...{ class: "is-amber" },
});
(__VLS_ctx.averageCoverage.toFixed(2));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "case-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "case-metric__icon case-metric__icon--green" },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_45 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "clock",
    size: (31),
}));
const __VLS_46 = __VLS_45({
    name: "clock",
    size: (31),
}, ...__VLS_functionalComponentArgsRest(__VLS_45));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.runtime?.runtime.enabled ? __VLS_ctx.formatDuration(__VLS_ctx.runtime.runtime.runningSeconds) : "已暂停");
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "case-ranking-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "case-ranking-toolbar" },
});
/** @type {[typeof CaseMonitorCategoryTabs, ]} */ ;
// @ts-ignore
const __VLS_48 = __VLS_asFunctionalComponent(CaseMonitorCategoryTabs, new CaseMonitorCategoryTabs({
    ...{ 'onUpdate:modelValue': {} },
    modelValue: (__VLS_ctx.typeFilter),
    options: (__VLS_ctx.categoryOptions),
}));
const __VLS_49 = __VLS_48({
    ...{ 'onUpdate:modelValue': {} },
    modelValue: (__VLS_ctx.typeFilter),
    options: (__VLS_ctx.categoryOptions),
}, ...__VLS_functionalComponentArgsRest(__VLS_48));
let __VLS_51;
let __VLS_52;
let __VLS_53;
const __VLS_54 = {
    'onUpdate:modelValue': (__VLS_ctx.chooseCategory)
};
var __VLS_50;
/** @type {[typeof CaseMonitorSearch, ]} */ ;
// @ts-ignore
const __VLS_55 = __VLS_asFunctionalComponent(CaseMonitorSearch, new CaseMonitorSearch({
    modelValue: (__VLS_ctx.keyword),
}));
const __VLS_56 = __VLS_55({
    modelValue: (__VLS_ctx.keyword),
}, ...__VLS_functionalComponentArgsRest(__VLS_55));
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
if (__VLS_ctx.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "cm-empty" },
    });
}
else if (__VLS_ctx.reportError) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "cm-empty" },
    });
    (__VLS_ctx.reportError);
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "case-table-scroll" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "cm-recommendation-header" },
        'aria-hidden': "true",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "cm-recommendation-header__info" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_58 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "info",
        size: (12),
    }));
    const __VLS_59 = __VLS_58({
        name: "info",
        size: (12),
    }, ...__VLS_functionalComponentArgsRest(__VLS_58));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "cm-recommendation-header__info" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_61 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "info",
        size: (12),
    }));
    const __VLS_62 = __VLS_61({
        name: "info",
        size: (12),
    }, ...__VLS_functionalComponentArgsRest(__VLS_61));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "cm-recommendation-header__info" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_64 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "info",
        size: (12),
    }));
    const __VLS_65 = __VLS_64({
        name: "info",
        size: (12),
    }, ...__VLS_functionalComponentArgsRest(__VLS_64));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    for (const [item, index] of __VLS_getVForSourceType((__VLS_ctx.pagedItems))) {
        /** @type {[typeof CaseMonitorRecommendationRow, ]} */ ;
        // @ts-ignore
        const __VLS_67 = __VLS_asFunctionalComponent(CaseMonitorRecommendationRow, new CaseMonitorRecommendationRow({
            ...{ 'onClick': {} },
            key: (item.marketHashName),
            item: (item),
            rank: ((__VLS_ctx.currentPage - 1) * __VLS_ctx.pageSize + index + 1),
            selected: (__VLS_ctx.selectedName === item.marketHashName),
        }));
        const __VLS_68 = __VLS_67({
            ...{ 'onClick': {} },
            key: (item.marketHashName),
            item: (item),
            rank: ((__VLS_ctx.currentPage - 1) * __VLS_ctx.pageSize + index + 1),
            selected: (__VLS_ctx.selectedName === item.marketHashName),
        }, ...__VLS_functionalComponentArgsRest(__VLS_67));
        let __VLS_70;
        let __VLS_71;
        let __VLS_72;
        const __VLS_73 = {
            onClick: (...[$event]) => {
                if (!!(__VLS_ctx.loading))
                    return;
                if (!!(__VLS_ctx.reportError))
                    return;
                __VLS_ctx.openDetail(item);
            }
        };
        var __VLS_69;
    }
    if (!__VLS_ctx.pagedItems.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "cm-empty" },
        });
    }
    /** @type {[typeof CaseMonitorPagination, ]} */ ;
    // @ts-ignore
    const __VLS_74 = __VLS_asFunctionalComponent(CaseMonitorPagination, new CaseMonitorPagination({
        ...{ 'onUpdate:pageSize': {} },
        modelValue: (__VLS_ctx.currentPage),
        totalItems: (__VLS_ctx.rankedItems.length),
        pageSize: (__VLS_ctx.pageSize),
    }));
    const __VLS_75 = __VLS_74({
        ...{ 'onUpdate:pageSize': {} },
        modelValue: (__VLS_ctx.currentPage),
        totalItems: (__VLS_ctx.rankedItems.length),
        pageSize: (__VLS_ctx.pageSize),
    }, ...__VLS_functionalComponentArgsRest(__VLS_74));
    let __VLS_77;
    let __VLS_78;
    let __VLS_79;
    const __VLS_80 = {
        'onUpdate:pageSize': (...[$event]) => {
            if (!!(__VLS_ctx.loading))
                return;
            if (!!(__VLS_ctx.reportError))
                return;
            __VLS_ctx.pageSize = $event;
        }
    };
    var __VLS_76;
}
if (__VLS_ctx.notice) {
    /** @type {[typeof CaseMonitorFeedback, typeof CaseMonitorFeedback, ]} */ ;
    // @ts-ignore
    const __VLS_81 = __VLS_asFunctionalComponent(CaseMonitorFeedback, new CaseMonitorFeedback({
        ...{ 'onClose': {} },
        ...{ class: "case-monitor-toast" },
        tone: (__VLS_ctx.notice.tone),
    }));
    const __VLS_82 = __VLS_81({
        ...{ 'onClose': {} },
        ...{ class: "case-monitor-toast" },
        tone: (__VLS_ctx.notice.tone),
    }, ...__VLS_functionalComponentArgsRest(__VLS_81));
    let __VLS_84;
    let __VLS_85;
    let __VLS_86;
    const __VLS_87 = {
        onClose: (...[$event]) => {
            if (!(__VLS_ctx.notice))
                return;
            __VLS_ctx.notice = null;
        }
    };
    __VLS_83.slots.default;
    (__VLS_ctx.notice.message);
    if (__VLS_ctx.notice.showExports) {
        {
            const { actions: __VLS_thisSlot } = __VLS_83.slots;
            __VLS_asFunctionalElement(__VLS_intrinsicElements.a, __VLS_intrinsicElements.a)({
                href: (__VLS_ctx.exportUrl('summary_csv')),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.a, __VLS_intrinsicElements.a)({
                href: (__VLS_ctx.exportUrl('buckets_csv')),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.a, __VLS_intrinsicElements.a)({
                href: (__VLS_ctx.exportUrl('markdown')),
            });
        }
    }
    var __VLS_83;
}
/** @type {[typeof CaseMonitorDetailDrawer, ]} */ ;
// @ts-ignore
const __VLS_88 = __VLS_asFunctionalComponent(CaseMonitorDetailDrawer, new CaseMonitorDetailDrawer({
    ...{ 'onClose': {} },
    open: (Boolean(__VLS_ctx.drawerItem)),
    item: (__VLS_ctx.drawerItem),
}));
const __VLS_89 = __VLS_88({
    ...{ 'onClose': {} },
    open: (Boolean(__VLS_ctx.drawerItem)),
    item: (__VLS_ctx.drawerItem),
}, ...__VLS_functionalComponentArgsRest(__VLS_88));
let __VLS_91;
let __VLS_92;
let __VLS_93;
const __VLS_94 = {
    onClose: (...[$event]) => {
        __VLS_ctx.drawerItem = null;
    }
};
var __VLS_90;
/** @type {__VLS_StyleScopedClasses['cm-surface']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-page']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-header']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-title']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-safety']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-controls']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-group']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-label']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-divider']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-group']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-label']} */ ;
/** @type {__VLS_StyleScopedClasses['case-custom-calendar']} */ ;
/** @type {__VLS_StyleScopedClasses['case-custom-range']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-divider']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-group']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-label']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-divider']} */ ;
/** @type {__VLS_StyleScopedClasses['case-cycle-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-divider']} */ ;
/** @type {__VLS_StyleScopedClasses['case-time-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-divider']} */ ;
/** @type {__VLS_StyleScopedClasses['case-time-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['case-control-divider']} */ ;
/** @type {__VLS_StyleScopedClasses['case-time-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon--green']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon--blue']} */ ;
/** @type {__VLS_StyleScopedClasses['is-blue']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon--amber']} */ ;
/** @type {__VLS_StyleScopedClasses['is-amber']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon']} */ ;
/** @type {__VLS_StyleScopedClasses['case-metric__icon--green']} */ ;
/** @type {__VLS_StyleScopedClasses['case-ranking-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['case-ranking-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['case-table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-recommendation-header']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-recommendation-header__info']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-recommendation-header__info']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-recommendation-header__info']} */ ;
/** @type {__VLS_StyleScopedClasses['cm-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['case-monitor-toast']} */ ;
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
            formatClock: formatClock,
            formatDuration: formatDuration,
            reportWindows: reportWindows,
            report: report,
            runtime: runtime,
            loading: loading,
            reportError: reportError,
            action: action,
            intervalMinutes: intervalMinutes,
            reportWindow: reportWindow,
            refreshLiquidity: refreshLiquidity,
            customStart: customStart,
            customEnd: customEnd,
            keyword: keyword,
            typeFilter: typeFilter,
            currentPage: currentPage,
            pageSize: pageSize,
            selectedName: selectedName,
            drawerItem: drawerItem,
            notice: notice,
            collectOnce: collectOnce,
            generateReport: generateReport,
            toggleMonitor: toggleMonitor,
            runtimeStatus: runtimeStatus,
            runtimeLabel: runtimeLabel,
            busy: busy,
            categoryOptions: categoryOptions,
            rankedItems: rankedItems,
            pagedItems: pagedItems,
            validSnapshots: validSnapshots,
            averageCoverage: averageCoverage,
            lastCollection: lastCollection,
            openDetail: openDetail,
            chooseCategory: chooseCategory,
            exportUrl: exportUrl,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
