import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatLocal, responseError, unwrapPayload } from "./guadao_shared";
const route = useRoute();
const logs = ref([]);
const meta = ref({});
const selectedId = ref(null);
const loading = ref(false);
const error = ref("");
const paused = ref(false);
const buffered = ref([]);
const streamConnected = ref(false);
const hasMore = ref(false);
const total = ref(0);
const startAt = ref("");
const endAt = ref("");
const level = ref("");
const service = ref("");
const operation = ref("");
const httpStatus = ref(String(route.query.httpStatus || ""));
const operationId = ref(String(route.query.operationId || ""));
const account = ref(String(route.query.account || ""));
const marketHashName = ref(String(route.query.marketHashName || ""));
const keyword = ref(String(route.query.q || ""));
const includeScheduler = ref(false);
const page = ref(1);
const pageSize = ref(20);
let source = null;
const seenEventIds = new Set();
const selected = computed(() => logs.value.find(row => (row.id ?? `${row.timestamp}-${row.requestId}`) === selectedId.value) || logs.value[0] || null);
const services = computed(() => [...new Set(logs.value.map(row => row.service).filter(Boolean))]);
const operations = computed(() => [...new Set(logs.value.map(row => row.operation).filter(Boolean))]);
const selectedDetail = computed(() => selected.value?.detail && typeof selected.value.detail === "object" ? selected.value.detail : {});
const requestFrequency = computed(() => { const value = selectedDetail.value.request_frequency || selectedDetail.value.requestFrequency; return value && typeof value === "object" ? value : {}; });
const relatedOperationQuery = computed(() => String(selected.value?.operationId || selected.value?.tradeNo || selected.value?.marketHashName || "").replace(/^GD-/i, ""));
const redactedError = computed(() => { const detail = selectedDetail.value; const result = detail.result && typeof detail.result === "object" ? detail.result : {}; const value = detail.error || detail.lastError || result.lastError; return String(value || ((selected.value?.httpStatus || 0) >= 400 ? selected.value?.message || "HTTP 请求失败" : "")); });
function rowId(row) { return row.id ?? `${row.timestamp}-${row.requestId}-${row.operation}`; }
function eventId(row) { return String(row.id || "").trim(); }
function rememberRows(rows) { for (const row of rows) {
    const id = eventId(row);
    if (id)
        seenEventIds.add(id);
} }
function normalizeLog(raw) { const candidate = raw.safe_context && typeof raw.safe_context === "object" ? raw.safe_context : raw.detail && typeof raw.detail === "object" ? raw.detail : {}; const context = candidate; return { id: String(raw.event_id || raw.id || ""), timestamp: String(raw.timestamp_utc || raw.timestamp || ""), level: String(raw.level || ""), service: String(raw.component || raw.provider || raw.service || ""), operation: String(raw.operation || ""), marketHashName: String(raw.market_hash_name || raw.marketHashName || context.marketHashName || "") || null, accountName: String(raw.account_id || raw.steam_id64 || raw.accountName || "") || null, httpStatus: raw.status_code == null ? raw.httpStatus : Number(raw.status_code), durationMs: raw.elapsed_ms == null ? raw.durationMs : Number(raw.elapsed_ms), message: String(raw.message || ""), requestId: String(raw.request_id || raw.requestId || "") || null, operationId: (raw.operationId || raw.trade_id || context.operationId || context.businessOperationId), tradeNo: String(raw.tradeNo || raw.trade_no || "") || null, caller: String(raw.caller || context.source || raw.source || "guadao"), endpoint: String(raw.endpoint || "") || null, retryAfter: (raw.retry_after ?? raw.retryAfter), detail: context }; }
function buildParams() { const params = new URLSearchParams({ page: String(page.value), pageSize: String(pageSize.value), includeSteamScheduler: String(includeScheduler.value) }); if (startAt.value)
    params.set("startAt", new Date(startAt.value).toISOString()); if (endAt.value)
    params.set("endAt", new Date(endAt.value).toISOString()); if (level.value)
    params.set("level", level.value); if (service.value)
    params.set("service", service.value); if (operation.value)
    params.set("operation", operation.value); if (httpStatus.value)
    params.set("httpStatus", httpStatus.value); if (operationId.value.trim())
    params.set("operationId", operationId.value.trim()); if (account.value.trim())
    params.set("account", account.value.trim()); if (marketHashName.value.trim())
    params.set("marketHashName", marketHashName.value.trim()); if (keyword.value.trim())
    params.set("q", keyword.value.trim()); return params; }
async function refresh() { loading.value = true; try {
    const response = await fetch(`/api/guadao/logs?${buildParams()}`, { cache: "no-store" });
    if (!response.ok)
        throw new Error(await responseError(response));
    const data = unwrapPayload(await response.json());
    const rawRows = data.events || data.items || [];
    logs.value = data.logs || rawRows.map(normalizeLog);
    rememberRows(logs.value);
    total.value = Number(data.total ?? logs.value.length);
    hasMore.value = Boolean(data.hasMore);
    const storage = data.storage || {};
    meta.value = data.meta || { retentionDays: Number(storage.retentionDays || 0) || undefined, diskUsageMb: storage.totalBytes == null ? undefined : Number(storage.totalBytes) / 1024 / 1024, fileCount: Number(storage.fileCount || 0), startAt: String(storage.earliestTimestamp || "") || undefined, endAt: String(storage.latestTimestamp || "") || undefined };
    if (selectedId.value == null || !logs.value.some(row => rowId(row) === selectedId.value))
        selectedId.value = logs.value[0] ? rowId(logs.value[0]) : null;
    error.value = "";
}
catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
}
finally {
    loading.value = false;
} }
function handleStreamEvent(event) { try {
    const row = normalizeLog(JSON.parse(event.data));
    const id = eventId(row);
    if (id && seenEventIds.has(id))
        return;
    if (id)
        seenEventIds.add(id);
    if (paused.value) {
        buffered.value.push(row);
        if (buffered.value.length > 500)
            buffered.value.shift();
        return;
    }
    if (page.value === 1)
        logs.value = [row, ...logs.value].slice(0, pageSize.value);
}
catch { } }
function connect() { disconnect(); const params = buildParams(); params.delete("page"); params.delete("pageSize"); const latestId = page.value === 1 && logs.value.length ? eventId(logs.value[0]) : ""; if (latestId)
    params.set("lastEventId", latestId); source = new EventSource(`/api/guadao/logs/stream?${params}`); source.onopen = () => { streamConnected.value = true; }; source.onerror = () => { streamConnected.value = false; }; source.addEventListener("guadao_log", handleStreamEvent); }
function disconnect() { source?.close(); source = null; streamConnected.value = false; }
function togglePause() { paused.value = !paused.value; if (!paused.value && buffered.value.length) {
    logs.value = [...buffered.value.reverse(), ...logs.value].slice(0, pageSize.value);
    buffered.value = [];
} }
async function query() { page.value = 1; disconnect(); await refresh(); connect(); }
function previousPage() { if (page.value <= 1)
    return; page.value -= 1; void refresh(); }
function followingPage() { if (!hasMore.value)
    return; page.value += 1; void refresh(); }
function exportLogs(format) { const params = buildParams(); params.set("format", format); params.delete("page"); params.delete("pageSize"); window.location.href = `/api/guadao/logs/export?${params}`; }
onMounted(() => { void (async () => { await refresh(); connect(); })(); });
onActivated(connect);
onDeactivated(disconnect);
onUnmounted(disconnect);
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['logs-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['stream-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['stream-state']} */ ;
/** @type {__VLS_StyleScopedClasses['stream-state']} */ ;
/** @type {__VLS_StyleScopedClasses['stream-state']} */ ;
/** @type {__VLS_StyleScopedClasses['online']} */ ;
/** @type {__VLS_StyleScopedClasses['log-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['log-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['log-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-check']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table']} */ ;
/** @type {__VLS_StyleScopedClasses['level-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['warn']} */ ;
/** @type {__VLS_StyleScopedClasses['level-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['relation-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['error-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['relation-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['frequency-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['frequency-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['error-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['error-panel']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page logs-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "logs-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "stream-actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['stream-state', { online: __VLS_ctx.streamConnected }]) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
(__VLS_ctx.streamConnected ? "实时连接正常" : "实时连接断开");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.togglePause) },
    ...{ class: "secondary-button" },
    type: "button",
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.paused ? 'play' : 'pause'),
    size: (14),
}));
const __VLS_1 = __VLS_0({
    name: (__VLS_ctx.paused ? 'play' : 'pause'),
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
(__VLS_ctx.paused ? `继续显示（${__VLS_ctx.buffered.length}）` : "暂停显示");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.connect) },
    ...{ class: "secondary-button" },
    type: "button",
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "refresh",
    size: (14),
}));
const __VLS_4 = __VLS_3({
    name: "refresh",
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "api-error" },
    });
    (__VLS_ctx.error);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "log-metrics" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.meta?.retentionDays ? `${__VLS_ctx.meta.retentionDays} 天 · 闭日压缩` : "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.meta?.diskUsageMb == null ? "—" : `${__VLS_ctx.meta.diskUsageMb.toFixed(1)} MB`);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.meta?.fileCount ?? "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.meta?.startAt ? `${__VLS_ctx.formatLocal(__VLS_ctx.meta.startAt)} — ${__VLS_ctx.formatLocal(__VLS_ctx.meta.endAt)}` : "—");
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel log-filters" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "filter-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "datetime-local",
});
(__VLS_ctx.startAt);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "datetime-local",
});
(__VLS_ctx.endAt);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.level),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.httpStatus),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "2xx",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "4xx",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "5xx",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "429",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "error",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.operation),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
for (const [value] of __VLS_getVForSourceType((__VLS_ctx.operations))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        key: (value),
    });
    (value);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "filter-row second" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    placeholder: "GD-123 / 123",
});
(__VLS_ctx.operationId);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    placeholder: "输入账号",
});
(__VLS_ctx.account);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    placeholder: "输入物品名",
});
(__VLS_ctx.marketHashName);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.service),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
for (const [value] of __VLS_getVForSourceType((__VLS_ctx.services))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        key: (value),
    });
    (value);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onKeyup: (__VLS_ctx.query) },
    placeholder: "请求 ID / 错误摘要",
});
(__VLS_ctx.keyword);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "filter-row third" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "scheduler-check" },
    title: "只增加跨执行器 Steam 请求调度元数据，不混入 Profit/Notify 业务日志",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onChange: (__VLS_ctx.query) },
    type: "checkbox",
});
(__VLS_ctx.includeScheduler);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "filter-actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.query) },
    ...{ class: "primary-button" },
    disabled: (__VLS_ctx.loading),
});
(__VLS_ctx.loading ? "查询中…" : "查询日志");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.exportLogs('jsonl');
        } },
    ...{ class: "secondary-button" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.exportLogs('csv');
        } },
    ...{ class: "secondary-button" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "log-workbench" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "panel log-table-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "table-wrap" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({
    ...{ class: "data-table log-table" },
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
__VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
for (const [row] of __VLS_getVForSourceType((__VLS_ctx.logs))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.selectedId = __VLS_ctx.rowId(row);
            } },
        key: (__VLS_ctx.rowId(row)),
        ...{ class: ([{ selected: __VLS_ctx.selected && __VLS_ctx.rowId(__VLS_ctx.selected) === __VLS_ctx.rowId(row) }, row.level?.toLowerCase()]) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (__VLS_ctx.formatLocal(row.timestamp));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: (['level-pill', row.level?.toLowerCase()]) },
    });
    (row.level || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (row.service || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (row.operation || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (row.marketHashName || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (row.accountName || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
        ...{ class: ({ 'http-error': (row.httpStatus || 0) >= 400 }) },
    });
    (row.httpStatus || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (row.durationMs == null ? "—" : `${row.durationMs} ms`);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (row.message || "—");
}
if (!__VLS_ctx.logs.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
    (__VLS_ctx.loading ? "正在读取日志…" : "当前筛选没有日志记录。");
}
if (__VLS_ctx.logs.length || __VLS_ctx.page > 1) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "pagination" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.page);
    (__VLS_ctx.logs.length);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        ...{ onChange: (__VLS_ctx.query) },
        value: (__VLS_ctx.pageSize),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: (20),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: (50),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: (100),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.previousPage) },
        disabled: (__VLS_ctx.page <= 1),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.followingPage) },
        disabled: (!__VLS_ctx.hasMore),
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
    ...{ class: "panel log-detail" },
});
if (__VLS_ctx.selected) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.selected.level || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.caller || "guadao");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.tradeNo || __VLS_ctx.selected.operationId || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.accountName || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.endpoint || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({
        ...{ class: ({ 'http-error': (__VLS_ctx.selected.httpStatus || 0) >= 400 }) },
    });
    (__VLS_ctx.selected.httpStatus || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.durationMs == null ? "—" : `${__VLS_ctx.selected.durationMs} ms`);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.retryAfter ?? "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.requestId || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "relation-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (__VLS_ctx.selected.marketHashName || "未绑定具体物品");
    (__VLS_ctx.selected.caller || "guadao");
    (__VLS_ctx.selected.operation || "未知操作");
    if (__VLS_ctx.relatedOperationQuery) {
        const __VLS_6 = {}.RouterLink;
        /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
        // @ts-ignore
        const __VLS_7 = __VLS_asFunctionalComponent(__VLS_6, new __VLS_6({
            to: ({ path: '/guadao/operations', query: { q: __VLS_ctx.relatedOperationQuery } }),
        }));
        const __VLS_8 = __VLS_7({
            to: ({ path: '/guadao/operations', query: { q: __VLS_ctx.relatedOperationQuery } }),
        }, ...__VLS_functionalComponentArgsRest(__VLS_7));
        __VLS_9.slots.default;
        var __VLS_9;
    }
    if (Object.keys(__VLS_ctx.requestFrequency).length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
            ...{ class: "frequency-panel" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        for (const [value, key] of __VLS_getVForSourceType((__VLS_ctx.requestFrequency))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                key: (key),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            (key);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (value);
        }
    }
    if (__VLS_ctx.redactedError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
            ...{ class: "error-panel" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        (__VLS_ctx.redactedError);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.pre, __VLS_intrinsicElements.pre)({});
    (JSON.stringify(__VLS_ctx.selected.detail || {}, null, 2));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "privacy-note" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_10 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "shield",
        size: (15),
    }));
    const __VLS_11 = __VLS_10({
        name: "shield",
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_10));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-page']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['stream-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['api-error']} */ ;
/** @type {__VLS_StyleScopedClasses['log-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['log-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['second']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-row']} */ ;
/** @type {__VLS_StyleScopedClasses['third']} */ ;
/** @type {__VLS_StyleScopedClasses['scheduler-check']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['log-workbench']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['data-table']} */ ;
/** @type {__VLS_StyleScopedClasses['log-table']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['log-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['relation-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['frequency-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['error-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['privacy-note']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            FolioIcon: FolioIcon,
            formatLocal: formatLocal,
            logs: logs,
            meta: meta,
            selectedId: selectedId,
            loading: loading,
            error: error,
            paused: paused,
            buffered: buffered,
            streamConnected: streamConnected,
            hasMore: hasMore,
            startAt: startAt,
            endAt: endAt,
            level: level,
            service: service,
            operation: operation,
            httpStatus: httpStatus,
            operationId: operationId,
            account: account,
            marketHashName: marketHashName,
            keyword: keyword,
            includeScheduler: includeScheduler,
            page: page,
            pageSize: pageSize,
            selected: selected,
            services: services,
            operations: operations,
            requestFrequency: requestFrequency,
            relatedOperationQuery: relatedOperationQuery,
            redactedError: redactedError,
            rowId: rowId,
            connect: connect,
            togglePause: togglePause,
            query: query,
            previousPage: previousPage,
            followingPage: followingPage,
            exportLogs: exportLogs,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
