import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import FolioIcon from "./FolioIcon.vue";
import ProfitTradeManualExecutionDialog from "./ProfitTradeManualExecutionDialog.vue";
import ProfitTradeRoiHistoryDrawer from "./ProfitTradeRoiHistoryDrawer.vue";
import ProfitTradeRoiWatchCard from "./ProfitTradeRoiWatchCard.vue";
const props = withDefaults(defineProps(), {
    running: false,
    executorEnabled: false,
    allowRealExecution: false,
});
const pageSize = 12;
const inventory = reactive({
    rows: [], total: 0, page: 1, summary: {}, loading: false, error: "",
});
const selection = reactive({
    rows: [], total: 0, page: 1, summary: {}, loading: false, error: "",
});
const inventoryKeywordDraft = ref("");
const inventoryKeyword = ref("");
const inventoryStatus = ref("active");
const inventoryRoiSign = ref("all");
const inventorySort = ref("roi_desc");
const listingsCircuit = ref({ status: "closed", isBlocking: false });
const listingsCooling = computed(() => listingsCircuit.value.status === "open");
const selectionQuery = ref("");
const selectionSuggestions = ref([]);
const selectedCatalogItem = ref(null);
const selectionSearchOpen = ref(false);
const selectionSearching = ref(false);
const selectionSearchHasMore = ref(false);
const selectionSearchNextOffset = ref(0);
const selectionAdding = ref(false);
const selectionActionError = ref("");
let selectionSearchTimer = null;
const selectedManualExecution = ref(null);
const manualExecutionSubmitting = ref(false);
const manualExecutionError = ref("");
const manualExecutionMessage = ref("");
const manualExecutionBatch = ref(null);
const manualExecutionStatusError = ref("");
const manualExecutionStorageKey = "profitTrade.manualExecutionRequestId";
let manualExecutionPollTimer = null;
let manualExecutionStatusLoading = false;
let terminalBatchRefreshId = "";
const selectedHistory = ref(null);
const history = reactive({
    rows: [],
    total: 0,
    page: 1,
    loading: false,
    error: "",
    stats: null,
    trend: { totalValidPoints: 0, sampled: false, points: [] },
});
const historyRange = ref("7d");
let inventoryLoadSequence = 0;
let selectionLoadSequence = 0;
const inventoryPages = computed(() => Math.max(1, Math.ceil(inventory.total / pageSize)));
const selectionPages = computed(() => Math.max(1, Math.ceil(selection.total / pageSize)));
const historyPages = computed(() => Math.max(1, Math.ceil(history.total / 20)));
const inventoryItemCount = computed(() => numericOr(inventory.summary.activeItemCount, inventory.total));
const selectionItemCount = computed(() => numericOr(selection.summary.activeItemCount, selection.total));
const inventoryProfitTotal = computed(() => inventory.summary.currentExpectedProfitTotal ?? null);
const inventoryBuyOrderProfitTotal = computed(() => inventory.summary.buyOrderReferenceProfitTotal ?? null);
const inventoryLongBuyActiveOrders = computed(() => numericOr(inventory.summary.longBuyActiveOrders, 0));
function numericOr(value, fallback) {
    return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}
function money(value) {
    return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "—";
}
function time(value) {
    if (!value)
        return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
        ? value
        : parsed.toLocaleString("zh-CN", { hour12: false });
}
function manualExecutionStatusLabel(status) {
    const labels = {
        pending: "排队中",
        retry: "等待重试",
        running: "执行中",
        completed: "已完成",
        failed: "执行失败",
        cancelled: "已取消",
    };
    return labels[status] || status || "状态未知";
}
function clearManualExecutionPoll() {
    if (!manualExecutionPollTimer)
        return;
    clearTimeout(manualExecutionPollTimer);
    manualExecutionPollTimer = null;
}
function rememberManualExecutionRequest(requestId) {
    if (typeof window === "undefined")
        return;
    window.localStorage.setItem(manualExecutionStorageKey, requestId);
}
function scheduleManualExecutionStatus(requestId, delay = 1500) {
    clearManualExecutionPoll();
    manualExecutionPollTimer = setTimeout(() => {
        void loadManualExecutionStatus(requestId);
    }, delay);
}
async function loadManualExecutionStatus(requestId) {
    const normalized = requestId.trim();
    if (!normalized || manualExecutionStatusLoading)
        return;
    manualExecutionStatusLoading = true;
    try {
        const query = new URLSearchParams({ requestId: normalized });
        const response = await fetch(`/api/profit-trade/manual-execution/status?${query}`, { cache: "no-store" });
        if (!response.ok) {
            const detail = await responseError(response);
            if (response.status === 404) {
                clearManualExecutionPoll();
                manualExecutionBatch.value = null;
                manualExecutionStatusError.value = `上一次一键执行批次已不存在：${detail}`;
                if (typeof window !== "undefined") {
                    window.localStorage.removeItem(manualExecutionStorageKey);
                }
                return;
            }
            throw new Error(detail);
        }
        const payload = await response.json();
        manualExecutionBatch.value = payload;
        manualExecutionMessage.value = "";
        manualExecutionStatusError.value = "";
        rememberManualExecutionRequest(payload.requestId);
        if (payload.terminal) {
            clearManualExecutionPoll();
            if (terminalBatchRefreshId !== payload.requestId) {
                terminalBatchRefreshId = payload.requestId;
                await loadInventory();
                window.dispatchEvent(new CustomEvent("profit-trade:refresh-observability"));
            }
        }
        else {
            scheduleManualExecutionStatus(payload.requestId);
        }
    }
    catch (reason) {
        manualExecutionStatusError.value = (`一键执行状态读取失败：${reason instanceof Error ? reason.message : String(reason)}`);
        scheduleManualExecutionStatus(normalized, 5000);
    }
    finally {
        manualExecutionStatusLoading = false;
    }
}
function restoreManualExecutionStatus() {
    if (typeof window === "undefined")
        return;
    const requestId = window.localStorage.getItem(manualExecutionStorageKey)?.trim();
    if (requestId)
        void loadManualExecutionStatus(requestId);
}
function dismissManualExecutionStatus() {
    clearManualExecutionPoll();
    manualExecutionBatch.value = null;
    manualExecutionStatusError.value = "";
    manualExecutionMessage.value = "";
    if (typeof window !== "undefined") {
        window.localStorage.removeItem(manualExecutionStorageKey);
    }
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
function assignPoolPayload(pool, payload) {
    pool.rows = Array.isArray(payload.items) ? payload.items : [];
    pool.total = numericOr(payload.total, 0);
    pool.summary = payload.summary || {};
}
async function loadInventory() {
    const requestSequence = ++inventoryLoadSequence;
    inventory.loading = true;
    inventory.error = "";
    const active = inventoryStatus.value === "all"
        ? "all"
        : inventoryStatus.value === "exited" ? "false" : "true";
    const query = new URLSearchParams({
        page: String(inventory.page),
        pageSize: String(pageSize),
        active,
        sort: inventorySort.value,
        roiSign: inventoryRoiSign.value,
    });
    if (inventoryKeyword.value)
        query.set("keyword", inventoryKeyword.value);
    try {
        const response = await fetch(`/api/profit-trade/roi-watch?${query}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        if (requestSequence !== inventoryLoadSequence)
            return;
        assignPoolPayload(inventory, payload);
        listingsCircuit.value = payload.listingsCircuit || { status: "closed", isBlocking: false };
    }
    catch (reason) {
        if (requestSequence !== inventoryLoadSequence)
            return;
        inventory.error = `库存做T观察读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        if (requestSequence === inventoryLoadSequence)
            inventory.loading = false;
    }
}
async function loadSelection() {
    const requestSequence = ++selectionLoadSequence;
    selection.loading = true;
    selection.error = "";
    const query = new URLSearchParams({
        page: String(selection.page),
        pageSize: String(pageSize),
        active: "true",
        sort: "roi_desc",
    });
    try {
        const response = await fetch(`/api/profit-trade/selection-watch?${query}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        if (requestSequence !== selectionLoadSequence)
            return;
        assignPoolPayload(selection, payload);
    }
    catch (reason) {
        if (requestSequence !== selectionLoadSequence)
            return;
        selection.error = `全市场选品观察读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        if (requestSequence === selectionLoadSequence)
            selection.loading = false;
    }
}
function loadAll() {
    void loadInventory();
    void loadSelection();
}
function searchInventory() {
    inventoryKeyword.value = inventoryKeywordDraft.value.trim();
    inventory.page = 1;
    void loadInventory();
}
function setInventoryRoiSign(sign) {
    if (inventoryRoiSign.value === sign)
        return;
    inventoryRoiSign.value = sign;
    inventory.page = 1;
    void loadInventory();
}
function turnInventory(direction) {
    const next = inventory.page + direction;
    if (next < 1 || next > inventoryPages.value)
        return;
    inventory.page = next;
    void loadInventory();
}
function turnSelection(direction) {
    const next = selection.page + direction;
    if (next < 1 || next > selectionPages.value)
        return;
    selection.page = next;
    void loadSelection();
}
function manualExecutionMaxQuantity(row) {
    const saved = row.manualExecutableQuantity;
    const tradable = row.tradableCount;
    const value = typeof saved === "number" && Number.isFinite(saved)
        ? saved
        : typeof tradable === "number" && Number.isFinite(tradable) ? tradable : 0;
    return Math.max(0, Math.min(20, Math.floor(value)));
}
function manualExecutionDisabledReason(row) {
    if (!props.executorEnabled)
        return "请先开启 Profit Trade 执行器";
    if (!props.allowRealExecution)
        return "请先开放 Profit Trade 真实执行";
    if (row.active === false)
        return "已退出观察池，不能执行";
    if (typeof row.expectedRoi !== "number" || row.expectedRoi <= 0)
        return "当前没有可人工批准的正 ROI";
    const allowedStatuses = new Set([
        "executable",
        "below_min_roi",
        "listings_cooldown",
        "listings_probe_ready",
    ]);
    const status = row.executionStatusCode || row.executionStatus || "";
    if (!allowedStatuses.has(status)) {
        return row.executionReason || row.riskReason || "当前被非 ROI 风控阻断";
    }
    if (manualExecutionMaxQuantity(row) <= 0)
        return "当前没有未锁定、可执行的资产";
    return "";
}
function openManualExecution(row) {
    const disabledReason = manualExecutionDisabledReason(row);
    manualExecutionMessage.value = "";
    if (disabledReason) {
        manualExecutionError.value = disabledReason;
        return;
    }
    manualExecutionError.value = "";
    selectedManualExecution.value = row;
}
function closeManualExecution() {
    if (manualExecutionSubmitting.value)
        return;
    selectedManualExecution.value = null;
    manualExecutionError.value = "";
}
async function confirmManualExecution(quantity) {
    const row = selectedManualExecution.value;
    if (!row || manualExecutionSubmitting.value)
        return;
    manualExecutionSubmitting.value = true;
    manualExecutionError.value = "";
    try {
        const response = await fetch("/api/profit-trade/roi-watch/execute", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                marketHashName: row.marketHashName,
                quantity,
                confirmed: true,
                expectedRoi: row.expectedRoi,
                scanId: row.scanId,
                observedAt: row.lastObservedAt,
            }),
        });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        if (!payload.requestId)
            throw new Error("后台未返回一键执行批次号");
        const requestedQuantity = payload.quantity ?? quantity;
        manualExecutionBatch.value = {
            requestId: payload.requestId,
            taskKey: payload.taskKey || `profit-manual:${payload.requestId}`,
            marketHashName: payload.marketHashName || row.marketHashName,
            name: row.name || row.marketHashName,
            requestedQuantity,
            status: "pending",
            terminal: false,
            summary: "一键执行已排队，等待后台领取",
            queuedAt: payload.requestedAt || new Date().toISOString(),
            counts: { created: 0, bought: 0, listed: 0, failed: 0 },
            trades: [],
        };
        rememberManualExecutionRequest(payload.requestId);
        terminalBatchRefreshId = "";
        manualExecutionMessage.value = "";
        selectedManualExecution.value = null;
        await loadInventory();
        window.dispatchEvent(new CustomEvent("profit-trade:manual-execution-queued", { detail: payload }));
        await loadManualExecutionStatus(payload.requestId);
    }
    catch (reason) {
        manualExecutionError.value = `一键执行提交失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        manualExecutionSubmitting.value = false;
    }
}
async function searchSelectionCatalog(append = false) {
    const query = selectionQuery.value.trim();
    selectionActionError.value = "";
    selectedCatalogItem.value = null;
    if (!query) {
        selectionSuggestions.value = [];
        selectionSearchOpen.value = true;
        selectionActionError.value = "请输入中文名或英文名后再搜索。";
        return;
    }
    selectionSearching.value = true;
    try {
        const offset = append ? selectionSearchNextOffset.value : 0;
        const response = await fetch(`/api/profit-trade/items/search?query=${encodeURIComponent(query)}&limit=50&offset=${offset}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        if (query !== selectionQuery.value.trim())
            return;
        const incoming = Array.isArray(payload.items) ? payload.items : [];
        if (append) {
            const merged = new Map(selectionSuggestions.value.map((item) => [item.marketHashName, item]));
            for (const item of incoming)
                merged.set(item.marketHashName, item);
            selectionSuggestions.value = [...merged.values()];
        }
        else {
            selectionSuggestions.value = incoming;
        }
        selectionSearchHasMore.value = Boolean(payload.pagination?.hasMore);
        selectionSearchNextOffset.value = Number(payload.pagination?.nextOffset ?? 0);
        selectionSearchOpen.value = true;
    }
    catch (reason) {
        selectionSuggestions.value = [];
        selectionSearchOpen.value = true;
        selectionActionError.value = `物品搜索失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        selectionSearching.value = false;
    }
}
function onSelectionQueryInput() {
    selectedCatalogItem.value = null;
    selectionActionError.value = "";
    selectionSearchHasMore.value = false;
    selectionSearchNextOffset.value = 0;
    if (selectionSearchTimer)
        clearTimeout(selectionSearchTimer);
    if (!selectionQuery.value.trim()) {
        selectionSuggestions.value = [];
        selectionSearchHasMore.value = false;
        selectionSearchNextOffset.value = 0;
        selectionSearchOpen.value = false;
        return;
    }
    selectionSearchTimer = setTimeout(() => void searchSelectionCatalog(), 260);
}
function chooseSelectionItem(item) {
    selectedCatalogItem.value = item;
    selectionQuery.value = item.name === item.marketHashName
        ? item.marketHashName
        : `${item.name} / ${item.marketHashName}`;
    selectionSearchOpen.value = false;
    selectionActionError.value = "";
}
async function updateSelectionWatch(action, marketHashName) {
    const response = await fetch("/api/profit-trade/selection-watch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, marketHashName }),
    });
    if (!response.ok)
        throw new Error(await responseError(response));
    return true;
}
async function addSelectionWatch() {
    if (!selectedCatalogItem.value?.marketHashName) {
        selectionActionError.value = "请先从搜索结果中选择准确饰品，再加入选品观察。";
        return;
    }
    selectionAdding.value = true;
    selectionActionError.value = "";
    try {
        await updateSelectionWatch("add", selectedCatalogItem.value.marketHashName);
        selectionQuery.value = "";
        selectionSuggestions.value = [];
        selectedCatalogItem.value = null;
        selectionSearchOpen.value = false;
        selection.page = 1;
        await loadSelection();
    }
    catch (reason) {
        selectionActionError.value = `加入选品观察失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        selectionAdding.value = false;
    }
}
async function removeSelectionWatch(row) {
    selectionActionError.value = "";
    try {
        await updateSelectionWatch("remove", row.marketHashName);
        await loadSelection();
    }
    catch (reason) {
        selectionActionError.value = `移出选品观察失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
}
function historyRangeFrom(range) {
    const days = { "7d": 7, "30d": 30, "90d": 90 };
    if (range === "all")
        return undefined;
    return new Date(Date.now() - days[range] * 24 * 60 * 60 * 1000).toISOString();
}
async function openHistory(pool, row) {
    selectedHistory.value = { pool, row };
    history.page = 1;
    historyRange.value = "7d";
    history.rows = [];
    history.total = 0;
    history.stats = null;
    history.trend = { totalValidPoints: 0, sampled: false, points: [] };
    await loadHistory(true);
}
async function loadHistory(refreshOverview = true) {
    if (!selectedHistory.value)
        return;
    history.loading = true;
    history.error = "";
    const query = new URLSearchParams({
        marketHashName: selectedHistory.value.row.marketHashName,
        page: String(history.page),
        pageSize: "20",
    });
    const from = historyRangeFrom(historyRange.value);
    if (from)
        query.set("from", from);
    const endpoint = selectedHistory.value.pool === "selection"
        ? "/api/profit-trade/selection-watch/history"
        : "/api/profit-trade/roi-watch/history";
    try {
        const response = await fetch(`${endpoint}?${query}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const payload = await response.json();
        history.rows = Array.isArray(payload.items) ? payload.items : [];
        history.total = numericOr(payload.total, 0);
        if (refreshOverview) {
            history.stats = payload.stats || null;
            history.trend = payload.trend || { totalValidPoints: 0, sampled: false, points: [] };
        }
    }
    catch (reason) {
        history.rows = [];
        history.total = 0;
        if (refreshOverview) {
            history.stats = null;
            history.trend = { totalValidPoints: 0, sampled: false, points: [] };
        }
        history.error = `历史读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
    }
    finally {
        history.loading = false;
    }
}
function changeHistoryRange(range) {
    if (historyRange.value === range)
        return;
    historyRange.value = range;
    history.page = 1;
    history.rows = [];
    history.total = 0;
    history.stats = null;
    history.trend = { totalValidPoints: 0, sampled: false, points: [] };
    void loadHistory(true);
}
function turnHistory(direction) {
    const next = history.page + direction;
    if (next < 1 || next > historyPages.value)
        return;
    history.page = next;
    void loadHistory(false);
}
watch([inventoryStatus, inventorySort], () => {
    inventory.page = 1;
    void loadInventory();
});
onMounted(() => {
    loadAll();
    restoreManualExecutionStatus();
    window.addEventListener("profit-trade:refresh-observability", loadAll);
});
onUnmounted(() => {
    if (selectionSearchTimer)
        clearTimeout(selectionSearchTimer);
    clearManualExecutionPoll();
    window.removeEventListener("profit-trade:refresh-observability", loadAll);
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_withDefaultsArg = (function (t) { return t; })({
    running: false,
    executorEnabled: false,
    allowRealExecution: false,
});
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['watch-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-running']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-running']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-running']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-circuit-note']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-header']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-header']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-header']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-search']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-add-button']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['dual-watch-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-card-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-card-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-search']} */ ;
/** @type {__VLS_StyleScopedClasses['search-button']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['is-pending']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['is-retry']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['is-failed']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['is-cancelled']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-trades']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-trades']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-trades']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-refresh-error']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-sign-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-sign-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-sign-switch']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "roi-watch panel" },
    'aria-labelledby': "roi-watch-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "watch-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
    id: "roi-watch-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.loadAll) },
    ...{ class: "secondary-button refresh-all" },
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
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "watch-summary" },
    'aria-label': "ROI 观察汇总",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.inventoryItemCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.selectionItemCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.inventoryLongBuyActiveOrders);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.money(__VLS_ctx.inventoryProfitTotal));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.money(__VLS_ctx.inventoryBuyOrderProfitTotal));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
if (__VLS_ctx.running) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "watch-running" },
        role: "status",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
}
if (__VLS_ctx.manualExecutionBatch) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "manual-execution-status" },
        ...{ class: (`is-${__VLS_ctx.manualExecutionBatch.status}`) },
        role: "status",
        'aria-live': "polite",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.manualExecutionBatch.name || __VLS_ctx.manualExecutionBatch.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.manualExecutionStatusLabel(__VLS_ctx.manualExecutionBatch.status));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (__VLS_ctx.manualExecutionBatch.summary);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.manualExecutionBatch.requestedQuantity);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.manualExecutionBatch.counts.created);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.manualExecutionBatch.counts.bought);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.manualExecutionBatch.counts.listed);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.manualExecutionBatch.counts.failed);
    if (__VLS_ctx.manualExecutionBatch.status === 'retry' && __VLS_ctx.manualExecutionBatch.nextAttemptAt) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "batch-next" },
        });
        (__VLS_ctx.time(__VLS_ctx.manualExecutionBatch.nextAttemptAt));
    }
    if (__VLS_ctx.manualExecutionBatch.trades.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.ul, __VLS_intrinsicElements.ul)({
            ...{ class: "batch-trades" },
        });
        for (const [trade] of __VLS_getVForSourceType((__VLS_ctx.manualExecutionBatch.trades))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
                key: (trade.id),
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (trade.tradeNo || `流水 ${trade.id}`);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (trade.status);
            if (trade.error) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
                (trade.error);
            }
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.manualExecutionBatch.requestId);
    (__VLS_ctx.time(__VLS_ctx.manualExecutionBatch.updatedAt || __VLS_ctx.manualExecutionBatch.queuedAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.dismissManualExecutionStatus) },
        type: "button",
    });
    if (__VLS_ctx.manualExecutionStatusError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "batch-refresh-error" },
        });
        (__VLS_ctx.manualExecutionStatusError);
    }
}
if (__VLS_ctx.manualExecutionMessage) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "manual-execution-message" },
        role: "status",
    });
    (__VLS_ctx.manualExecutionMessage);
}
else if (__VLS_ctx.manualExecutionError && !__VLS_ctx.selectedManualExecution) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "watch-error" },
        role: "alert",
    });
    (__VLS_ctx.manualExecutionError);
}
if (__VLS_ctx.listingsCooling) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "watch-circuit-note" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.time(__VLS_ctx.listingsCircuit.cooldownUntil));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "dual-watch-layout" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "watch-pool inventory-pool" },
    'aria-labelledby': "inventory-watch-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "roi-sign-switch" },
    role: "group",
    'aria-label': "ROI 筛选",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setInventoryRoiSign('all');
        } },
    type: "button",
    ...{ class: ({ active: __VLS_ctx.inventoryRoiSign === 'all' }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setInventoryRoiSign('positive');
        } },
    type: "button",
    ...{ class: ({ active: __VLS_ctx.inventoryRoiSign === 'positive' }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setInventoryRoiSign('negative');
        } },
    type: "button",
    ...{ class: ({ active: __VLS_ctx.inventoryRoiSign === 'negative' }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "pool-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({
    id: "inventory-watch-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.inventoryItemCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
    ...{ onSubmit: (__VLS_ctx.searchInventory) },
    ...{ class: "inventory-toolbar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    type: "search",
    placeholder: "中文名或 marketHashName",
});
(__VLS_ctx.inventoryKeywordDraft);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.inventoryStatus),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "active",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "all",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "exited",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.inventorySort),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "roi_desc",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "updated_desc",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "price_desc",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "secondary-button" },
    type: "submit",
});
if (__VLS_ctx.inventory.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "watch-error" },
    });
    (__VLS_ctx.inventory.error);
}
if (__VLS_ctx.inventory.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "watch-empty" },
    });
}
else if (!__VLS_ctx.inventory.error && __VLS_ctx.inventory.rows.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "watch-empty" },
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pool-card-grid" },
    });
    for (const [row] of __VLS_getVForSourceType((__VLS_ctx.inventory.rows))) {
        /** @type {[typeof ProfitTradeRoiWatchCard, ]} */ ;
        // @ts-ignore
        const __VLS_3 = __VLS_asFunctionalComponent(ProfitTradeRoiWatchCard, new ProfitTradeRoiWatchCard({
            ...{ 'onOpenHistory': {} },
            ...{ 'onManualExecute': {} },
            key: (row.marketHashName),
            row: (row),
            pool: "inventory",
            listingsCooling: (__VLS_ctx.listingsCooling),
            manualExecutionDisabledReason: (__VLS_ctx.manualExecutionDisabledReason(row)),
        }));
        const __VLS_4 = __VLS_3({
            ...{ 'onOpenHistory': {} },
            ...{ 'onManualExecute': {} },
            key: (row.marketHashName),
            row: (row),
            pool: "inventory",
            listingsCooling: (__VLS_ctx.listingsCooling),
            manualExecutionDisabledReason: (__VLS_ctx.manualExecutionDisabledReason(row)),
        }, ...__VLS_functionalComponentArgsRest(__VLS_3));
        let __VLS_6;
        let __VLS_7;
        let __VLS_8;
        const __VLS_9 = {
            onOpenHistory: (...[$event]) => {
                if (!!(__VLS_ctx.inventory.loading))
                    return;
                if (!!(!__VLS_ctx.inventory.error && __VLS_ctx.inventory.rows.length === 0))
                    return;
                __VLS_ctx.openHistory('inventory', $event);
            }
        };
        const __VLS_10 = {
            onManualExecute: (__VLS_ctx.openManualExecution)
        };
        var __VLS_5;
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
    ...{ class: "pagination" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.inventory.page);
(__VLS_ctx.inventoryPages);
(__VLS_ctx.inventory.total);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.turnInventory(-1);
        } },
    ...{ class: "mini-action" },
    type: "button",
    disabled: (__VLS_ctx.inventory.page <= 1),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.turnInventory(1);
        } },
    ...{ class: "mini-action" },
    type: "button",
    disabled: (__VLS_ctx.inventory.page >= __VLS_ctx.inventoryPages),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "watch-pool selection-pool" },
    'aria-labelledby': "selection-watch-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "pool-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({
    id: "selection-watch-title",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.selectionItemCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
    ...{ onSubmit: (__VLS_ctx.addSelectionWatch) },
    ...{ class: "selection-toolbar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    for: "selection-item-search",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "selection-input-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "selection-search" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    ...{ onInput: (__VLS_ctx.onSelectionQueryInput) },
    ...{ onFocus: (...[$event]) => {
            __VLS_ctx.selectionQuery.trim() && __VLS_ctx.searchSelectionCatalog(false);
        } },
    id: "selection-item-search",
    type: "search",
    autocomplete: "off",
    placeholder: "输入中文名或英文名",
});
(__VLS_ctx.selectionQuery);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.searchSelectionCatalog(false);
        } },
    ...{ class: "secondary-button search-button" },
    type: "button",
    disabled: (__VLS_ctx.selectionSearching),
});
(__VLS_ctx.selectionSearching ? "搜索中…" : "搜索");
if (__VLS_ctx.selectionSearchOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "selection-suggestions" },
    });
    for (const [item] of __VLS_getVForSourceType((__VLS_ctx.selectionSuggestions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.selectionSearchOpen))
                        return;
                    __VLS_ctx.chooseSelectionItem(item);
                } },
            key: (item.marketHashName),
            type: "button",
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (item.name);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (item.marketHashName);
    }
    if (__VLS_ctx.selectionSearchHasMore) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.selectionSearchOpen))
                        return;
                    if (!(__VLS_ctx.selectionSearchHasMore))
                        return;
                    __VLS_ctx.searchSelectionCatalog(true);
                } },
            ...{ class: "catalog-load-more" },
            type: "button",
            disabled: (__VLS_ctx.selectionSearching),
        });
        (__VLS_ctx.selectionSearching ? "加载中…" : "加载更多结果");
    }
    if (!__VLS_ctx.selectionSearching && __VLS_ctx.selectionSuggestions.length === 0) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "primary-add-button" },
    type: "submit",
    disabled: (!__VLS_ctx.selectedCatalogItem || __VLS_ctx.selectionAdding),
});
(__VLS_ctx.selectionAdding ? "加入中…" : "加入选品观察");
if (__VLS_ctx.selectedCatalogItem) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
        ...{ class: "selected-item" },
    });
    (__VLS_ctx.selectedCatalogItem.name);
    (__VLS_ctx.selectedCatalogItem.marketHashName);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "selection-safety" },
});
if (__VLS_ctx.selectionActionError) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "watch-error" },
    });
    (__VLS_ctx.selectionActionError);
}
if (__VLS_ctx.selection.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "watch-error" },
    });
    (__VLS_ctx.selection.error);
}
if (__VLS_ctx.selection.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "watch-empty" },
    });
}
else if (!__VLS_ctx.selection.error && __VLS_ctx.selection.rows.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "watch-empty" },
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pool-card-grid" },
    });
    for (const [row] of __VLS_getVForSourceType((__VLS_ctx.selection.rows))) {
        /** @type {[typeof ProfitTradeRoiWatchCard, ]} */ ;
        // @ts-ignore
        const __VLS_11 = __VLS_asFunctionalComponent(ProfitTradeRoiWatchCard, new ProfitTradeRoiWatchCard({
            ...{ 'onOpenHistory': {} },
            ...{ 'onRemoveSelection': {} },
            key: (row.marketHashName),
            row: (row),
            pool: "selection",
        }));
        const __VLS_12 = __VLS_11({
            ...{ 'onOpenHistory': {} },
            ...{ 'onRemoveSelection': {} },
            key: (row.marketHashName),
            row: (row),
            pool: "selection",
        }, ...__VLS_functionalComponentArgsRest(__VLS_11));
        let __VLS_14;
        let __VLS_15;
        let __VLS_16;
        const __VLS_17 = {
            onOpenHistory: (...[$event]) => {
                if (!!(__VLS_ctx.selection.loading))
                    return;
                if (!!(!__VLS_ctx.selection.error && __VLS_ctx.selection.rows.length === 0))
                    return;
                __VLS_ctx.openHistory('selection', $event);
            }
        };
        const __VLS_18 = {
            onRemoveSelection: (__VLS_ctx.removeSelectionWatch)
        };
        var __VLS_13;
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
    ...{ class: "pagination" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.selection.page);
(__VLS_ctx.selectionPages);
(__VLS_ctx.selection.total);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.turnSelection(-1);
        } },
    ...{ class: "mini-action" },
    type: "button",
    disabled: (__VLS_ctx.selection.page <= 1),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.turnSelection(1);
        } },
    ...{ class: "mini-action" },
    type: "button",
    disabled: (__VLS_ctx.selection.page >= __VLS_ctx.selectionPages),
});
/** @type {[typeof ProfitTradeRoiHistoryDrawer, ]} */ ;
// @ts-ignore
const __VLS_19 = __VLS_asFunctionalComponent(ProfitTradeRoiHistoryDrawer, new ProfitTradeRoiHistoryDrawer({
    ...{ 'onClose': {} },
    ...{ 'onChangeRange': {} },
    ...{ 'onChangePage': {} },
    selected: (__VLS_ctx.selectedHistory?.row || null),
    pool: (__VLS_ctx.selectedHistory?.pool || 'inventory'),
    history: (__VLS_ctx.history.rows),
    total: (__VLS_ctx.history.total),
    page: (__VLS_ctx.history.page),
    pages: (__VLS_ctx.historyPages),
    loading: (__VLS_ctx.history.loading),
    error: (__VLS_ctx.history.error),
    stats: (__VLS_ctx.history.stats),
    trend: (__VLS_ctx.history.trend),
    range: (__VLS_ctx.historyRange),
}));
const __VLS_20 = __VLS_19({
    ...{ 'onClose': {} },
    ...{ 'onChangeRange': {} },
    ...{ 'onChangePage': {} },
    selected: (__VLS_ctx.selectedHistory?.row || null),
    pool: (__VLS_ctx.selectedHistory?.pool || 'inventory'),
    history: (__VLS_ctx.history.rows),
    total: (__VLS_ctx.history.total),
    page: (__VLS_ctx.history.page),
    pages: (__VLS_ctx.historyPages),
    loading: (__VLS_ctx.history.loading),
    error: (__VLS_ctx.history.error),
    stats: (__VLS_ctx.history.stats),
    trend: (__VLS_ctx.history.trend),
    range: (__VLS_ctx.historyRange),
}, ...__VLS_functionalComponentArgsRest(__VLS_19));
let __VLS_22;
let __VLS_23;
let __VLS_24;
const __VLS_25 = {
    onClose: (...[$event]) => {
        __VLS_ctx.selectedHistory = null;
    }
};
const __VLS_26 = {
    onChangeRange: (__VLS_ctx.changeHistoryRange)
};
const __VLS_27 = {
    onChangePage: (__VLS_ctx.turnHistory)
};
var __VLS_21;
/** @type {[typeof ProfitTradeManualExecutionDialog, ]} */ ;
// @ts-ignore
const __VLS_28 = __VLS_asFunctionalComponent(ProfitTradeManualExecutionDialog, new ProfitTradeManualExecutionDialog({
    ...{ 'onClose': {} },
    ...{ 'onConfirm': {} },
    row: (__VLS_ctx.selectedManualExecution),
    maxQuantity: (__VLS_ctx.selectedManualExecution ? __VLS_ctx.manualExecutionMaxQuantity(__VLS_ctx.selectedManualExecution) : 0),
    submitting: (__VLS_ctx.manualExecutionSubmitting),
    error: (__VLS_ctx.manualExecutionError),
}));
const __VLS_29 = __VLS_28({
    ...{ 'onClose': {} },
    ...{ 'onConfirm': {} },
    row: (__VLS_ctx.selectedManualExecution),
    maxQuantity: (__VLS_ctx.selectedManualExecution ? __VLS_ctx.manualExecutionMaxQuantity(__VLS_ctx.selectedManualExecution) : 0),
    submitting: (__VLS_ctx.manualExecutionSubmitting),
    error: (__VLS_ctx.manualExecutionError),
}, ...__VLS_functionalComponentArgsRest(__VLS_28));
let __VLS_31;
let __VLS_32;
let __VLS_33;
const __VLS_34 = {
    onClose: (__VLS_ctx.closeManualExecution)
};
const __VLS_35 = {
    onConfirm: (__VLS_ctx.confirmManualExecution)
};
var __VLS_30;
/** @type {__VLS_StyleScopedClasses['roi-watch']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['refresh-all']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-running']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-status']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-next']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-trades']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-refresh-error']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-execution-message']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-error']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-circuit-note']} */ ;
/** @type {__VLS_StyleScopedClasses['dual-watch-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-pool']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-pool']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-sign-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-header']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-error']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-card-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-pool']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-pool']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-header']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-search']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['search-button']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-add-button']} */ ;
/** @type {__VLS_StyleScopedClasses['selected-item']} */ ;
/** @type {__VLS_StyleScopedClasses['selection-safety']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-error']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-error']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['watch-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['pool-card-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            ProfitTradeManualExecutionDialog: ProfitTradeManualExecutionDialog,
            ProfitTradeRoiHistoryDrawer: ProfitTradeRoiHistoryDrawer,
            ProfitTradeRoiWatchCard: ProfitTradeRoiWatchCard,
            inventory: inventory,
            selection: selection,
            inventoryKeywordDraft: inventoryKeywordDraft,
            inventoryStatus: inventoryStatus,
            inventoryRoiSign: inventoryRoiSign,
            inventorySort: inventorySort,
            listingsCircuit: listingsCircuit,
            listingsCooling: listingsCooling,
            selectionQuery: selectionQuery,
            selectionSuggestions: selectionSuggestions,
            selectedCatalogItem: selectedCatalogItem,
            selectionSearchOpen: selectionSearchOpen,
            selectionSearching: selectionSearching,
            selectionSearchHasMore: selectionSearchHasMore,
            selectionAdding: selectionAdding,
            selectionActionError: selectionActionError,
            selectedManualExecution: selectedManualExecution,
            manualExecutionSubmitting: manualExecutionSubmitting,
            manualExecutionError: manualExecutionError,
            manualExecutionMessage: manualExecutionMessage,
            manualExecutionBatch: manualExecutionBatch,
            manualExecutionStatusError: manualExecutionStatusError,
            selectedHistory: selectedHistory,
            history: history,
            historyRange: historyRange,
            inventoryPages: inventoryPages,
            selectionPages: selectionPages,
            historyPages: historyPages,
            inventoryItemCount: inventoryItemCount,
            selectionItemCount: selectionItemCount,
            inventoryProfitTotal: inventoryProfitTotal,
            inventoryBuyOrderProfitTotal: inventoryBuyOrderProfitTotal,
            inventoryLongBuyActiveOrders: inventoryLongBuyActiveOrders,
            money: money,
            time: time,
            manualExecutionStatusLabel: manualExecutionStatusLabel,
            dismissManualExecutionStatus: dismissManualExecutionStatus,
            loadAll: loadAll,
            searchInventory: searchInventory,
            setInventoryRoiSign: setInventoryRoiSign,
            turnInventory: turnInventory,
            turnSelection: turnSelection,
            manualExecutionMaxQuantity: manualExecutionMaxQuantity,
            manualExecutionDisabledReason: manualExecutionDisabledReason,
            openManualExecution: openManualExecution,
            closeManualExecution: closeManualExecution,
            confirmManualExecution: confirmManualExecution,
            searchSelectionCatalog: searchSelectionCatalog,
            onSelectionQueryInput: onSelectionQueryInput,
            chooseSelectionItem: chooseSelectionItem,
            addSelectionWatch: addSelectionWatch,
            removeSelectionWatch: removeSelectionWatch,
            openHistory: openHistory,
            changeHistoryRange: changeHistoryRange,
            turnHistory: turnHistory,
        };
    },
    __typeProps: {},
    props: {},
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeProps: {},
    props: {},
});
; /* PartiallyEnd: #4569/main.vue */
