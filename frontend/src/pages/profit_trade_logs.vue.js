import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
const route = useRoute();
const rows = ref([]);
const nextCursor = ref(null);
const hasMore = ref(false);
const storage = ref({ retentionDays: 90 });
const loading = ref(false);
const loadingMore = ref(false);
const error = ref("");
const connection = ref("connecting");
const paused = ref(false);
const queued = ref([]);
const selected = ref(null);
const detailLoading = ref(false);
const detailError = ref("");
const detailTab = ref("basic");
const from = ref("");
const to = ref("");
const level = ref("");
const provider = ref("");
const component = ref("");
const operation = ref("");
const steamId = ref("");
const tradeNo = ref(typeof route.query.tradeNo === "string" ? route.query.tradeNo : "");
const requestId = ref("");
const keywordDraft = ref("");
const keyword = ref("");
const pageSize = 100;
let stream = null;
const levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
const connectionLabel = computed(() => ({ connecting: "正在连接", online: "SSE 已连接", offline: "SSE 已断开", paused: "实时显示已暂停" })[connection.value]);
function pick(event, camel, snake) {
    return (event[camel] ?? event[snake]);
}
function id(event) { return String(pick(event, "eventId", "event_id") || "-"); }
function timestamp(event) { return String(pick(event, "timestampUtc", "timestamp_utc") || ""); }
function trade(event) { return String(pick(event, "tradeNo", "trade_no") || "-"); }
function request(event) { return String(pick(event, "requestId", "request_id") || "-"); }
function steam(event) { return String(pick(event, "steamId64", "steam_id64") || pick(event, "accountId", "account_id") || "-"); }
function statusCode(event) { return pick(event, "statusCode", "status_code"); }
function elapsed(event) { return pick(event, "elapsedMs", "elapsed_ms"); }
function context(event) { return pick(event, "safeContext", "safe_context") || {}; }
function frequency(event, key) {
    const value = context(event).request_frequency;
    return value && typeof value === "object" ? value[key] ?? "未记录" : "未记录";
}
function time(value) {
    if (!value)
        return "-";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime()))
        return value;
    return `${parsed.toLocaleString("zh-CN", { hour12: false })}.${String(parsed.getMilliseconds()).padStart(3, "0")}`;
}
function bytes(value) {
    const amount = Number(value) || 0;
    if (amount < 1024)
        return `${amount} B`;
    if (amount < 1024 ** 2)
        return `${(amount / 1024).toFixed(1)} KB`;
    if (amount < 1024 ** 3)
        return `${(amount / 1024 ** 2).toFixed(1)} MB`;
    return `${(amount / 1024 ** 3).toFixed(2)} GB`;
}
function levelClass(value) { return String(value || "INFO").toLowerCase(); }
function serviceLabel(value) { return { steam: "Steam", c5: "C5", local: "本地" }[String(value)] || String(value || "本地"); }
function apiTime(value) {
    if (!value)
        return "";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString();
}
function filters(includePaging = true) {
    const query = new URLSearchParams();
    const values = { from: apiTime(from.value), to: apiTime(to.value), level: level.value, provider: provider.value, component: component.value, operation: operation.value, steamId: steamId.value, tradeNo: tradeNo.value, requestId: requestId.value, keyword: keyword.value };
    Object.entries(values).forEach(([key, value]) => { if (value.trim())
        query.set(key, value.trim()); });
    if (includePaging)
        query.set("pageSize", String(pageSize));
    return query;
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
function normalize(payload) { return Array.isArray(payload.items) ? payload.items : Array.isArray(payload.events) ? payload.events : []; }
async function load(reset = true) {
    if (reset) {
        loading.value = true;
        error.value = "";
    }
    else
        loadingMore.value = true;
    const query = filters();
    if (!reset && nextCursor.value)
        query.set("cursor", nextCursor.value);
    try {
        const response = await fetch(`/api/profit-trade/logs?${query}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        const events = normalize(payload);
        rows.value = reset ? events : [...rows.value, ...events];
        nextCursor.value = payload.nextCursor ?? payload.next_cursor ?? null;
        hasMore.value = Boolean(payload.hasMore ?? payload.has_more ?? nextCursor.value);
        if (payload.storage)
            storage.value = { ...storage.value, ...payload.storage };
    }
    catch (cause) {
        if (reset)
            rows.value = [];
        error.value = `日志读取失败：${cause instanceof Error ? cause.message : String(cause)}`;
    }
    finally {
        loading.value = false;
        loadingMore.value = false;
    }
}
function apply() { keyword.value = keywordDraft.value.trim(); queued.value = []; void load(); connect(); }
function resetFilters() {
    from.value = "";
    to.value = "";
    level.value = "";
    provider.value = "";
    component.value = "";
    operation.value = "";
    steamId.value = "";
    tradeNo.value = "";
    requestId.value = "";
    keywordDraft.value = "";
    keyword.value = "";
    queued.value = [];
    void load();
    connect();
}
function onLog(event) {
    try {
        const parsed = JSON.parse(event.data);
        if (String(parsed.source || "profit_trade") !== "profit_trade")
            return;
        if (paused.value)
            queued.value.push(parsed);
        else {
            rows.value = [parsed, ...rows.value.filter(item => id(item) !== id(parsed))].slice(0, 1000);
            void nextTick(() => document.querySelector(".logs-table-wrap")?.scrollTo({ top: 0, behavior: "smooth" }));
        }
    }
    catch { /* A malformed event is ignored without breaking the stream. */ }
}
function connect() {
    stream?.close();
    stream = null;
    if (paused.value) {
        connection.value = "paused";
        return;
    }
    connection.value = "connecting";
    const query = filters(false);
    query.set("source", "profit_trade");
    stream = new EventSource(`/api/profit-trade/logs/stream?${query}`);
    stream.addEventListener("open", () => { connection.value = paused.value ? "paused" : "online"; });
    stream.addEventListener("log", onLog);
    stream.addEventListener("profit_trade_log", onLog);
    stream.onmessage = onLog;
    stream.addEventListener("heartbeat", () => { connection.value = paused.value ? "paused" : "online"; });
    stream.onerror = () => { connection.value = paused.value ? "paused" : "offline"; };
}
function togglePause() {
    paused.value = !paused.value;
    if (paused.value) {
        connection.value = "paused";
        return;
    }
    rows.value = [...queued.value.reverse(), ...rows.value].slice(0, 1000);
    queued.value = [];
    if (!stream || stream.readyState === EventSource.CLOSED)
        connect();
    else
        connection.value = stream.readyState === EventSource.OPEN ? "online" : "connecting";
}
function exportUrl(format) { const query = filters(false); query.set("format", format); return `/api/profit-trade/logs/export?${query}`; }
async function openDetail(event) {
    selected.value = event;
    detailTab.value = "basic";
    detailLoading.value = true;
    detailError.value = "";
    const eventId = id(event);
    if (eventId === "-") {
        detailLoading.value = false;
        return;
    }
    try {
        const response = await fetch(`/api/profit-trade/logs/event?eventId=${encodeURIComponent(eventId)}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        if (payload.event)
            selected.value = payload.event;
    }
    catch (cause) {
        detailError.value = `完整事件读取失败：${cause instanceof Error ? cause.message : String(cause)}`;
    }
    finally {
        detailLoading.value = false;
    }
}
watch(() => route.query.tradeNo, value => { if (typeof value === "string" && value !== tradeNo.value) {
    tradeNo.value = value;
    apply();
} });
onMounted(() => { void load(); connect(); });
onUnmounted(() => { stream?.close(); stream = null; });
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['logs-title']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-title']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-title']} */ ;
/** @type {__VLS_StyleScopedClasses['stream-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['connection']} */ ;
/** @type {__VLS_StyleScopedClasses['connection']} */ ;
/** @type {__VLS_StyleScopedClasses['connection']} */ ;
/** @type {__VLS_StyleScopedClasses['connection']} */ ;
/** @type {__VLS_StyleScopedClasses['connection']} */ ;
/** @type {__VLS_StyleScopedClasses['storage-strip']} */ ;
/** @type {__VLS_StyleScopedClasses['storage-strip']} */ ;
/** @type {__VLS_StyleScopedClasses['storage-strip']} */ ;
/** @type {__VLS_StyleScopedClasses['storage-strip']} */ ;
/** @type {__VLS_StyleScopedClasses['log-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['log-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['log-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['log-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['log-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['log-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['export-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['export-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['export-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['level']} */ ;
/** @type {__VLS_StyleScopedClasses['level']} */ ;
/** @type {__VLS_StyleScopedClasses['level']} */ ;
/** @type {__VLS_StyleScopedClasses['level']} */ ;
/** @type {__VLS_StyleScopedClasses['level']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "logs-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "logs-title" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.code, __VLS_intrinsicElements.code)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "stream-actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: (['connection', __VLS_ctx.connection]) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
(__VLS_ctx.connectionLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.togglePause) },
    ...{ class: "secondary-button" },
    type: "button",
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.paused ? 'play' : 'pause'),
    size: (15),
}));
const __VLS_1 = __VLS_0({
    name: (__VLS_ctx.paused ? 'play' : 'pause'),
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
(__VLS_ctx.paused ? `继续实时显示${__VLS_ctx.queued.length ? `（${__VLS_ctx.queued.length}）` : ''}` : "暂停实时显示");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.connect) },
    ...{ class: "secondary-button" },
    type: "button",
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "refresh",
    size: (15),
}));
const __VLS_4 = __VLS_3({
    name: "refresh",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "storage-strip" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.storage.retentionDays || 90);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.bytes(__VLS_ctx.storage.totalBytes));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.storage.fileCount ?? "-");
(__VLS_ctx.storage.compressedFileCount ?? "-");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.time(__VLS_ctx.storage.earliestTimestamp));
(__VLS_ctx.time(__VLS_ctx.storage.latestTimestamp));
__VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
    ...{ onSubmit: (__VLS_ctx.apply) },
    ...{ class: "log-filters" },
});
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
    value: (__VLS_ctx.level),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
for (const [item] of __VLS_getVForSourceType((__VLS_ctx.levels))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        key: (item),
    });
    (item);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.provider),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "steam",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "c5",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "local",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    value: (__VLS_ctx.component),
    type: "text",
    placeholder: "steam_market",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    value: (__VLS_ctx.operation),
    type: "text",
    placeholder: "search_listings",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    value: (__VLS_ctx.steamId),
    type: "text",
    placeholder: "SteamId64",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    value: (__VLS_ctx.tradeNo),
    type: "text",
    placeholder: "PT-...",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    value: (__VLS_ctx.requestId),
    type: "text",
    placeholder: "req_...",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "keyword" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    type: "search",
    placeholder: "摘要、异常、饰品名",
});
(__VLS_ctx.keywordDraft);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "primary-button" },
    type: "submit",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.resetFilters) },
    ...{ class: "secondary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "export-bar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_6 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "shield",
    size: (15),
}));
const __VLS_7 = __VLS_6({
    name: "shield",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_6));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.a, __VLS_intrinsicElements.a)({
    href: (__VLS_ctx.exportUrl('jsonl')),
    ...{ class: "secondary-button" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.a, __VLS_intrinsicElements.a)({
    href: (__VLS_ctx.exportUrl('log')),
    ...{ class: "secondary-button" },
});
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "log-error" },
    });
    (__VLS_ctx.error);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "logs-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "logs-table-wrap" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.table, __VLS_intrinsicElements.table)({});
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
if (__VLS_ctx.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
        colspan: "9",
        ...{ class: "empty" },
    });
}
else if (__VLS_ctx.rows.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
        colspan: "9",
        ...{ class: "empty" },
    });
}
else {
    for (const [event] of __VLS_getVForSourceType((__VLS_ctx.rows))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.loading))
                        return;
                    if (!!(__VLS_ctx.rows.length === 0))
                        return;
                    __VLS_ctx.openDetail(event);
                } },
            key: (__VLS_ctx.id(event)),
            ...{ class: ({ selected: __VLS_ctx.id(__VLS_ctx.selected || {}) === __VLS_ctx.id(event) }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "time" },
        });
        (__VLS_ctx.time(__VLS_ctx.timestamp(event)));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: (['level', __VLS_ctx.levelClass(event.level)]) },
        });
        (event.level || "INFO");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "provider" },
        });
        (__VLS_ctx.serviceLabel(event.provider));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (event.component || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (event.operation || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.trade(event));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "mono" },
        });
        (__VLS_ctx.steam(event));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: ({ httpError: (__VLS_ctx.statusCode(event) || 0) >= 400 }) },
        });
        (event.method || "");
        (__VLS_ctx.statusCode(event) ?? "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.elapsed(event) === undefined ? "-" : `${__VLS_ctx.elapsed(event)} ms`);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "message" },
        });
        (event.message || "-");
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.rows.length);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.load(false);
        } },
    ...{ class: "secondary-button" },
    type: "button",
    disabled: (!__VLS_ctx.hasMore || __VLS_ctx.loadingMore),
});
(__VLS_ctx.loadingMore ? "加载中" : __VLS_ctx.hasMore ? "加载更早日志" : "没有更多");
if (__VLS_ctx.selected) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "event-detail panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "eyebrow" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.selected.component || "-");
    (__VLS_ctx.selected.operation || "-");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.id(__VLS_ctx.selected));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    if (__VLS_ctx.trade(__VLS_ctx.selected) !== '-') {
        const __VLS_9 = {}.RouterLink;
        /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
        // @ts-ignore
        const __VLS_10 = __VLS_asFunctionalComponent(__VLS_9, new __VLS_9({
            to: ({ path: '/profit-trade/interruptions', query: { tradeNo: __VLS_ctx.trade(__VLS_ctx.selected) } }),
            ...{ class: "detail-link" },
        }));
        const __VLS_11 = __VLS_10({
            to: ({ path: '/profit-trade/interruptions', query: { tradeNo: __VLS_ctx.trade(__VLS_ctx.selected) } }),
            ...{ class: "detail-link" },
        }, ...__VLS_functionalComponentArgsRest(__VLS_10));
        __VLS_12.slots.default;
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_13 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "link",
            size: (14),
        }));
        const __VLS_14 = __VLS_13({
            name: "link",
            size: (14),
        }, ...__VLS_functionalComponentArgsRest(__VLS_13));
        var __VLS_12;
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.selected))
                    return;
                __VLS_ctx.selected = null;
            } },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.nav, __VLS_intrinsicElements.nav)({});
    for (const [tab] of __VLS_getVForSourceType(([{ key: 'basic', label: '基本信息' }, { key: 'request', label: '请求与响应' }, { key: 'frequency', label: '请求频率' }, { key: 'links', label: '关联链路与异常' }, { key: 'raw', label: '完整脱敏 JSON' }]))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.selected))
                        return;
                    __VLS_ctx.detailTab = tab.key;
                } },
            key: (tab.key),
            type: "button",
            ...{ class: ({ active: __VLS_ctx.detailTab === tab.key }) },
        });
        (tab.label);
    }
    if (__VLS_ctx.detailError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "detail-error" },
        });
        (__VLS_ctx.detailError);
    }
    if (__VLS_ctx.detailLoading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "detail-loading" },
        });
    }
    if (__VLS_ctx.detailTab === 'basic') {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "detail-grid" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.timestamp(__VLS_ctx.selected) || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.time(__VLS_ctx.timestamp(__VLS_ctx.selected)));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.selected.source || "profit_trade");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.serviceLabel(__VLS_ctx.selected.provider));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'runId', 'run_id') || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.trade(__VLS_ctx.selected));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'marketHashName', 'market_hash_name') || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'assetId', 'asset_id') || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
            ...{ class: "wide" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.selected.message || "-");
    }
    else if (__VLS_ctx.detailTab === 'request') {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "detail-grid" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.request(__VLS_ctx.selected));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'clientInstanceId', 'client_instance_id') || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.selected.method || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.selected.endpoint || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.statusCode(__VLS_ctx.selected) ?? "未返回");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.elapsed(__VLS_ctx.selected) === undefined ? "未记录" : `${__VLS_ctx.elapsed(__VLS_ctx.selected)} ms`);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.selected.attempt ?? "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'retryAfter', 'retry_after') ?? "未返回");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
            ...{ class: "wide" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.pre, __VLS_intrinsicElements.pre)({});
        (JSON.stringify(__VLS_ctx.context(__VLS_ctx.selected), null, 2));
    }
    else if (__VLS_ctx.detailTab === 'frequency') {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "detail-grid" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.frequency(__VLS_ctx.selected, "last_10_seconds"));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.frequency(__VLS_ctx.selected, "last_60_seconds"));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.frequency(__VLS_ctx.selected, "last_5_minutes"));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.frequency(__VLS_ctx.selected, "current_concurrent"));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.context(__VLS_ctx.selected).ms_since_previous_request ?? __VLS_ctx.context(__VLS_ctx.selected).msSincePreviousRequest ?? "未记录");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.steam(__VLS_ctx.selected));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "wide-note" },
        });
    }
    else if (__VLS_ctx.detailTab === 'links') {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "detail-grid" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'stateFrom', 'state_from') || "-");
        (__VLS_ctx.pick(__VLS_ctx.selected, 'stateTo', 'state_to') || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'stepFrom', 'step_from') || "-");
        (__VLS_ctx.pick(__VLS_ctx.selected, 'stepTo', 'step_to') || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'exceptionType', 'exception_type') || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.context(__VLS_ctx.selected).listing_id_obtained ?? __VLS_ctx.context(__VLS_ctx.selected).listingIdObtained ?? "未记录");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.context(__VLS_ctx.selected).purchase_request_sent ?? __VLS_ctx.context(__VLS_ctx.selected).purchaseRequestSent ?? "未记录");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'tradeId', 'trade_id') || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
            ...{ class: "wide" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.pre, __VLS_intrinsicElements.pre)({});
        (__VLS_ctx.pick(__VLS_ctx.selected, 'stackTrace', 'stack_trace') || "未记录");
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.pre, __VLS_intrinsicElements.pre)({
            ...{ class: "raw-json" },
        });
        (JSON.stringify(__VLS_ctx.selected, null, 2));
    }
}
/** @type {__VLS_StyleScopedClasses['logs-page']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-title']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['stream-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['storage-strip']} */ ;
/** @type {__VLS_StyleScopedClasses['log-filters']} */ ;
/** @type {__VLS_StyleScopedClasses['keyword']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['export-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['log-error']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['logs-table-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['time']} */ ;
/** @type {__VLS_StyleScopedClasses['provider']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['message']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['event-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-link']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-error']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-loading']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['wide']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['wide']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-note']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['wide']} */ ;
/** @type {__VLS_StyleScopedClasses['raw-json']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            FolioIcon: FolioIcon,
            rows: rows,
            hasMore: hasMore,
            storage: storage,
            loading: loading,
            loadingMore: loadingMore,
            error: error,
            connection: connection,
            paused: paused,
            queued: queued,
            selected: selected,
            detailLoading: detailLoading,
            detailError: detailError,
            detailTab: detailTab,
            from: from,
            to: to,
            level: level,
            provider: provider,
            component: component,
            operation: operation,
            steamId: steamId,
            tradeNo: tradeNo,
            requestId: requestId,
            keywordDraft: keywordDraft,
            levels: levels,
            connectionLabel: connectionLabel,
            pick: pick,
            id: id,
            timestamp: timestamp,
            trade: trade,
            request: request,
            steam: steam,
            statusCode: statusCode,
            elapsed: elapsed,
            context: context,
            frequency: frequency,
            time: time,
            bytes: bytes,
            levelClass: levelClass,
            serviceLabel: serviceLabel,
            load: load,
            apply: apply,
            resetFilters: resetFilters,
            connect: connect,
            togglePause: togglePause,
            exportUrl: exportUrl,
            openDetail: openDetail,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
