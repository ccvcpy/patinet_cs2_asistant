import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { Activity, ChartPie, ChevronDown, ChevronLeft, ChevronRight, Layers3, ListFilter, ScanLine, Search, ShoppingCart, Tag, Target, X, } from "@lucide/vue";
import { C5_RESEARCH_NO_WEAR_ID, adviceLabel, buildC5ResearchEstimatePayload, buildC5ResearchFilterPayload, compactRarityName, filterSelectionItems, finiteNumber, formatCount, formatDuration, formatMoney, formatPercent, formatSignedMoney, historyWindowStart, inventoryAdvice, isC5ResearchTerminalStatus, itemClassSupportsWear, normalizeC5ResearchTaxonomy, optionalNumber, qualityTone, recommendationLabel, recommendationTone, sortSelectionItems, taxonomyOptionsForContext, wearOptionsForItemClass, } from "./c5_t_monitor_shared";
const EMPTY_PAYLOAD = {
    researchOnly: true,
    canExecute: false,
    activeCount: 0,
    total: 0,
    page: 1,
    pageSize: 200,
    summary: {},
    items: [],
};
const EMPTY_RESEARCH_TAXONOMY = {
    itemClasses: [],
    subtypes: [],
    weapons: [],
    rarities: [],
    versions: [],
    wears: [],
    phases: [],
};
const EMPTY_RESEARCH_RESULTS = {
    items: [],
    total: 0,
    page: 1,
    pageSize: 20,
    sort: "roi_desc",
};
const RESEARCH_REQUEST_STORAGE_KEY = "c5-research:last-request-id";
function defaultResearchDraft() {
    return {
        itemClassId: "",
        subtypeId: "",
        weaponId: "",
        rarityId: "",
        versionId: "",
        wearId: "",
        wearMin: "0.00",
        wearMax: "1.00",
        phaseId: "",
        priceMin: "",
        priceMax: "",
        keyword: "",
    };
}
const payload = ref(EMPTY_PAYLOAD);
const loading = ref(true);
const scanning = ref(false);
const apiError = ref("");
const actionMessage = ref("");
const page = ref(1);
const pageSize = 10;
const selectedItem = ref(null);
const selectedHistory = ref(null);
const historyLoading = ref(false);
const drawerOpen = ref(false);
const statisticsDays = ref(1);
const market = ref("C5");
const itemType = ref("");
const quality = ref("");
const wearMin = ref("0.00");
const wearMax = ref("1.00");
const priceMin = ref("");
const priceMax = ref("");
const keyword = ref("");
const appliedFilters = ref({
    itemType: "",
    quality: "",
    wearMin: 0,
    wearMax: 1,
    priceMin: null,
    priceMax: null,
    keyword: "",
});
const activeMode = ref("condition");
const researchTaxonomy = ref(EMPTY_RESEARCH_TAXONOMY);
const researchTaxonomyLoading = ref(false);
const researchDraft = ref(defaultResearchDraft());
const researchAppliedFilters = ref(null);
const researchEstimate = ref(null);
const researchEstimating = ref(false);
const researchSubmitting = ref(false);
const researchPolling = ref(false);
const researchResultsLoading = ref(false);
const researchControlAction = ref("");
const researchAddingItem = ref("");
const researchAddedItems = ref(new Set());
const researchError = ref("");
const researchMessage = ref("");
const researchTask = ref(null);
const researchResults = ref(EMPTY_RESEARCH_RESULTS);
const researchResultPage = ref(1);
const researchResultPageSize = ref(20);
const researchResultSort = ref("roi_desc");
let pollTimer;
let refreshTimer;
let researchPollTimer;
let lastLoadedResearchResultCount = -1;
async function fetchJsonResponse(path, options) {
    const response = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok)
        throw new Error(data.error || `请求失败 (${response.status})`);
    return { httpStatus: response.status, data };
}
async function fetchJson(path, options) {
    return (await fetchJsonResponse(path, options)).data;
}
function researchRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
        ? value
        : {};
}
function researchText(record, keys, fallback = "") {
    for (const key of keys) {
        const value = record[key];
        if (value !== undefined && value !== null && String(value).trim())
            return String(value).trim();
    }
    return fallback;
}
function researchNumber(record, keys) {
    for (const key of keys) {
        const value = finiteNumber(record[key]);
        if (value !== null)
            return value;
    }
    return null;
}
function researchTaxonomyName(options, id, fallback = "") {
    return options.find((option) => option.id === id)?.name || fallback || id;
}
function normalizeResearchEstimate(value) {
    const envelope = researchRecord(value);
    const root = Object.keys(researchRecord(envelope.estimate)).length
        ? researchRecord(envelope.estimate)
        : envelope;
    const rawWarnings = root.warnings;
    return {
        catalogMatchedCount: researchNumber(root, ["catalogMatchedCount", "matchedCount", "total"]),
        requiresC5PriceCount: researchNumber(root, ["requiresC5PriceCount", "c5PriceCount"]),
        estimatedSeconds: researchNumber(root, ["estimatedSeconds", "estimatedDurationSeconds", "durationSeconds"]),
        warnings: Array.isArray(rawWarnings)
            ? rawWarnings.map((warning) => String(warning || "").trim()).filter(Boolean)
            : [],
    };
}
function normalizeResearchTask(value, requestIdFallback = "") {
    const envelope = researchRecord(value);
    const nested = researchRecord(envelope.scan);
    const root = Object.keys(nested).length ? nested : envelope;
    const requestId = researchText(root, ["requestId", "request_id"], requestIdFallback);
    const status = researchText(root, ["status", "state"], "queued").toLowerCase();
    const processedCount = Math.max(0, Math.round(researchNumber(root, ["processedCount", "scannedCount", "completedCount"]) || 0));
    const successCount = Math.max(0, Math.round(researchNumber(root, ["successCount", "succeededCount", "observedCount"]) || 0));
    const failedCount = Math.max(0, Math.round(researchNumber(root, ["failedCount", "errorCount"]) || 0));
    const resultCount = Math.max(0, Math.round(researchNumber(root, ["resultCount", "resultsCount", "availableResultCount", "observedCount"]) || 0));
    const catalogMatchedCount = researchNumber(root, ["catalogMatchedCount", "matchedCount", "total"]);
    const suppliedProgress = researchNumber(root, ["progressPct", "progressPercent", "progress"]);
    const calculatedProgress = catalogMatchedCount && catalogMatchedCount > 0
        ? processedCount / catalogMatchedCount
        : null;
    const normalizedProgress = suppliedProgress === null
        ? calculatedProgress
        : suppliedProgress > 1
            ? suppliedProgress / 100
            : suppliedProgress;
    return {
        requestId,
        status,
        terminal: root.terminal === true || isC5ResearchTerminalStatus(status),
        catalogMatchedCount,
        processedCount,
        successCount,
        failedCount,
        resultCount,
        progressPct: normalizedProgress === null ? null : Math.max(0, Math.min(1, normalizedProgress)),
        nextAttemptAt: researchText(root, ["nextAttemptAt", "next_attempt_at"]) || null,
        message: researchText(root, ["message", "summary"]) || null,
        error: researchText(root, ["error", "lastError", "last_error"]) || null,
        researchOnly: root.researchOnly !== false,
        canExecute: root.canExecute === true,
    };
}
function normalizeResearchResults(value) {
    const envelope = researchRecord(value);
    const nested = researchRecord(envelope.results);
    const root = Object.keys(nested).length ? nested : envelope;
    const rawItems = Array.isArray(root.items)
        ? root.items
        : Array.isArray(envelope.results)
            ? envelope.results
            : [];
    const items = rawItems
        .filter((item) => item && typeof item === "object")
        .map((item) => {
        const raw = researchRecord(item);
        const taxonomy = researchRecord(raw.taxonomy);
        const categoryId = researchText(taxonomy, ["categoryId", "category_id"]);
        const subtypeId = researchText(taxonomy, ["subtypeId", "subtype_id"]);
        const weaponId = researchText(taxonomy, ["weaponId", "weapon_id"]);
        const rarityId = researchText(taxonomy, ["rarityId", "rarity_id"]);
        const versionId = researchText(taxonomy, ["version", "versionId", "version_id"]);
        const wearId = researchText(taxonomy, ["wearId", "wear_id"]);
        const phaseId = researchText(taxonomy, ["phase", "phaseId", "phase_id"]);
        const c5Error = researchText(raw, ["c5Error", "c5_error"]);
        const steamError = researchText(raw, ["steamError", "steam_error"]);
        const steamSellPrice = researchNumber(raw, ["steamSellPrice", "steam_sell_price", "steamBuyPrice"]);
        return {
            ...raw,
            marketHashName: researchText(raw, ["marketHashName", "market_hash_name"]),
            name: researchText(raw, ["name", "nameCn", "name_cn", "marketHashName"]),
            imageUrl: researchText(raw, ["imageUrl", "image_url"])
                || researchText(taxonomy, ["imageUrl", "image_url"])
                || null,
            itemType: researchTaxonomyName(researchTaxonomy.value.itemClasses, categoryId),
            itemClassName: researchTaxonomyName(researchTaxonomy.value.itemClasses, categoryId),
            subtypeName: researchTaxonomyName(researchTaxonomy.value.subtypes, subtypeId),
            weaponName: researchTaxonomyName(researchTaxonomy.value.weapons, weaponId),
            rarityName: researchText(taxonomy, ["rarityName", "rarity_name"])
                || researchTaxonomyName(researchTaxonomy.value.rarities, rarityId),
            rarityColor: researchText(taxonomy, ["rarityColor", "rarity_color"]) || null,
            versionName: researchTaxonomyName(researchTaxonomy.value.versions, versionId),
            wearName: researchText(taxonomy, ["wearName", "wear_name"])
                || researchTaxonomyName(researchTaxonomy.value.wears, wearId, "无磨损"),
            phaseName: researchTaxonomyName(researchTaxonomy.value.phases, phaseId),
            minFloat: researchNumber(taxonomy, ["minFloat", "min_float"]),
            maxFloat: researchNumber(taxonomy, ["maxFloat", "max_float"]),
            steamSellPrice,
            steamBuyPrice: steamSellPrice,
            c5Error: c5Error || null,
            steamError: steamError || null,
            error: [c5Error, steamError].filter(Boolean).join("；") || null,
        };
    });
    return {
        items,
        total: Math.max(0, Math.round(researchNumber(root, ["total", "resultCount"]) || rawItems.length)),
        page: Math.max(1, Math.round(researchNumber(root, ["page"]) || researchResultPage.value)),
        pageSize: Math.max(1, Math.round(researchNumber(root, ["pageSize", "page_size"]) || researchResultPageSize.value)),
        sort: researchText(root, ["sort"], researchResultSort.value),
    };
}
function formatDailyChange(value) {
    const number = finiteNumber(value);
    if (number === null)
        return "—";
    return `${number >= 0 ? "+" : ""}${formatPercent(number)} ${number >= 0 ? "↑" : "↓"}`;
}
function formatSummaryMoney(value) {
    return formatMoney(value).replace("¥", "¥ ");
}
async function loadItems(silent = false) {
    if (!silent)
        loading.value = true;
    try {
        const data = await fetchJson("/api/profit-trade/selection-watch?active=1&page=1&pageSize=200&sort=roi_desc");
        payload.value = {
            ...EMPTY_PAYLOAD,
            ...data,
            summary: data.summary || {},
            items: Array.isArray(data.items) ? data.items : [],
        };
        apiError.value = "";
        const currentName = selectedItem.value?.marketHashName;
        const current = payload.value.items.find((item) => item.marketHashName === currentName);
        if (current) {
            selectedItem.value = current;
        }
        else if (payload.value.items.length) {
            await selectItem(payload.value.items[0], false);
        }
        else {
            selectedItem.value = null;
            selectedHistory.value = null;
        }
    }
    catch (error) {
        apiError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        loading.value = false;
    }
}
async function loadHistory(item) {
    historyLoading.value = true;
    try {
        const query = new URLSearchParams({
            marketHashName: item.marketHashName,
            from: historyWindowStart(statisticsDays.value),
            page: "1",
            pageSize: "500",
        });
        const data = await fetchJson(`/api/profit-trade/selection-watch/history?${query.toString()}`);
        selectedHistory.value = {
            ...data,
            summary: data.summary || {},
            trend: Array.isArray(data.trend)
                ? data.trend
                : Array.isArray(data.trend?.points)
                    ? data.trend.points
                    : [],
            items: Array.isArray(data.items) ? data.items : [],
        };
    }
    catch (error) {
        actionMessage.value = "";
        apiError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        historyLoading.value = false;
    }
}
async function selectItem(item, openDrawer) {
    selectedItem.value = item;
    if (openDrawer)
        drawerOpen.value = true;
    await loadHistory(item);
}
async function setStatisticsWindow(days) {
    statisticsDays.value = days;
    if (selectedItem.value)
        await loadHistory(selectedItem.value);
}
async function startScan() {
    if (scanning.value)
        return;
    scanning.value = true;
    apiError.value = "";
    actionMessage.value = "";
    try {
        const result = await fetchJson("/api/profit-trade/selection-watch/refresh", {
            method: "POST",
            body: JSON.stringify({}),
        });
        actionMessage.value = result.alreadyRunning
            ? "研究扫描已在运行，页面将自动刷新结果"
            : "研究扫描已排队，不会触发购买、锁仓或上架";
        window.clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(() => loadItems(true), 1800);
    }
    catch (error) {
        apiError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        scanning.value = false;
    }
}
function applyFilters() {
    appliedFilters.value = {
        itemType: itemType.value,
        quality: quality.value,
        wearMin: finiteNumber(wearMin.value),
        wearMax: finiteNumber(wearMax.value),
        priceMin: optionalNumber(priceMin.value),
        priceMax: optionalNumber(priceMax.value),
        keyword: keyword.value,
    };
    page.value = 1;
}
function resetFilters() {
    market.value = "C5";
    itemType.value = "";
    quality.value = "";
    wearMin.value = "0.00";
    wearMax.value = "1.00";
    priceMin.value = "";
    priceMax.value = "";
    keyword.value = "";
    applyFilters();
}
function researchOptionLabel(option) {
    return option.count === undefined
        ? option.name
        : `${option.name}（${formatCount(option.count)}）`;
}
function researchStatusLabel(status) {
    return {
        idle: "尚未创建",
        queued: "已排队",
        running: "扫描中",
        paused: "已暂停",
        completed: "已完成",
        completed_with_errors: "完成但有异常",
        failed: "失败",
        canceled: "已取消",
        cancelled: "已取消",
    }[String(status || "idle").toLowerCase()] || String(status || "未知");
}
function setActiveMode(mode) {
    activeMode.value = mode;
    drawerOpen.value = false;
    if (mode === "condition" && !researchTaxonomy.value.itemClasses.length) {
        void loadResearchTaxonomy();
    }
    if (mode === "watch")
        void loadItems(true);
}
async function loadResearchTaxonomy() {
    if (researchTaxonomyLoading.value)
        return;
    researchTaxonomyLoading.value = true;
    researchError.value = "";
    try {
        const data = await fetchJson("/api/c5-research/taxonomy");
        const normalized = normalizeC5ResearchTaxonomy(data);
        if (!normalized.itemClasses.length)
            throw new Error("完整分类接口没有返回任何饰品大类");
        researchTaxonomy.value = normalized;
        onResearchItemClassChange(false);
    }
    catch (error) {
        researchError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        researchTaxonomyLoading.value = false;
    }
}
function keepResearchSelection(field, options) {
    const current = researchDraft.value[field];
    if (current && !options.some((option) => option.id === current))
        researchDraft.value[field] = "";
}
function onResearchItemClassChange(announce = true) {
    researchDraft.value.subtypeId = "";
    researchDraft.value.weaponId = "";
    const supportsWear = researchSupportsWear.value;
    if (supportsWear === false) {
        const noWear = wearOptionsForItemClass(researchTaxonomy.value.wears, selectedResearchItemClass.value)[0];
        researchDraft.value.wearId = noWear?.id || C5_RESEARCH_NO_WEAR_ID;
        researchDraft.value.phaseId = "";
    }
    else if (researchDraft.value.wearId === C5_RESEARCH_NO_WEAR_ID) {
        researchDraft.value.wearId = "";
    }
    keepResearchSelection("rarityId", researchRarityOptions.value);
    keepResearchSelection("versionId", researchVersionOptions.value);
    keepResearchSelection("wearId", researchWearOptions.value);
    keepResearchSelection("phaseId", researchPhaseOptions.value);
    researchEstimate.value = null;
    if (announce)
        researchMessage.value = "大类已切换，关联细类、武器、品质和磨损条件已同步";
}
function onResearchSubtypeChange() {
    researchDraft.value.weaponId = "";
    keepResearchSelection("rarityId", researchRarityOptions.value);
    keepResearchSelection("versionId", researchVersionOptions.value);
    keepResearchSelection("phaseId", researchPhaseOptions.value);
    researchEstimate.value = null;
}
function applyResearchFilters(announce = true) {
    const filters = buildC5ResearchFilterPayload(researchDraft.value, {
        supportsWear: researchSupportsWear.value,
    });
    researchAppliedFilters.value = filters;
    researchEstimate.value = null;
    researchError.value = "";
    if (announce)
        researchMessage.value = "筛选条件已应用；估算和扫描都会使用当前完整条件";
    return filters;
}
function resetResearchFilters() {
    researchDraft.value = defaultResearchDraft();
    researchAppliedFilters.value = null;
    researchEstimate.value = null;
    researchError.value = "";
    researchMessage.value = "筛选条件已重置；已创建任务仍保留其冻结条件和结果";
}
async function estimateResearchScan() {
    if (researchEstimating.value)
        return;
    researchEstimating.value = true;
    researchError.value = "";
    try {
        const filters = applyResearchFilters(false);
        const data = await fetchJson("/api/c5-research/estimate", {
            method: "POST",
            body: JSON.stringify(buildC5ResearchEstimatePayload(filters)),
        });
        researchEstimate.value = normalizeResearchEstimate(data);
        researchMessage.value = "估算完成；尚未创建扫描任务，也没有请求 Steam 行情";
    }
    catch (error) {
        researchError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        researchEstimating.value = false;
    }
}
function stopResearchPolling() {
    window.clearInterval(researchPollTimer);
    researchPollTimer = undefined;
}
function startResearchPolling() {
    stopResearchPolling();
    if (!researchTask.value?.requestId || researchTask.value.terminal)
        return;
    researchPollTimer = window.setInterval(() => void pollResearchTask(), 2500);
}
async function startResearchScan() {
    if (researchSubmitting.value || researchHasActiveTask.value)
        return;
    researchSubmitting.value = true;
    researchError.value = "";
    try {
        const filters = applyResearchFilters(false);
        const response = await fetchJsonResponse("/api/c5-research/scans", {
            method: "POST",
            body: JSON.stringify(filters),
        });
        if (response.httpStatus !== 202) {
            throw new Error(`创建扫描必须返回 202，实际为 ${response.httpStatus}`);
        }
        const requestId = researchText(response.data, ["requestId", "request_id"]);
        if (!requestId)
            throw new Error("扫描已被接收，但响应缺少 requestId");
        const accepted = normalizeResearchTask(response.data, requestId);
        researchTask.value = {
            ...accepted,
            requestId,
            status: "queued",
            terminal: false,
        };
        window.localStorage.setItem(RESEARCH_REQUEST_STORAGE_KEY, requestId);
        researchResults.value = { ...EMPTY_RESEARCH_RESULTS };
        researchResultPage.value = 1;
        lastLoadedResearchResultCount = -1;
        researchMessage.value = `任务 ${requestId} 已排队；202 仅表示后台接收，页面将轮询真实终态`;
        startResearchPolling();
    }
    catch (error) {
        researchError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        researchSubmitting.value = false;
    }
}
async function pollResearchTask(requestId = researchTask.value?.requestId || "") {
    if (!requestId || researchPolling.value)
        return;
    researchPolling.value = true;
    try {
        const data = await fetchJson(`/api/c5-research/scans/${encodeURIComponent(requestId)}`);
        const task = normalizeResearchTask(data, requestId);
        researchTask.value = task;
        researchError.value = "";
        if (task.resultCount !== lastLoadedResearchResultCount || task.terminal) {
            await loadResearchResults();
            lastLoadedResearchResultCount = task.resultCount;
        }
        if (task.terminal) {
            stopResearchPolling();
            researchMessage.value = task.status === "completed"
                ? `任务 ${task.requestId} 已完成，结果以终态数据为准`
                : task.status === "completed_with_errors"
                    ? `任务 ${task.requestId} 已完成，但部分品类存在行情异常`
                    : `任务 ${task.requestId} 已进入终态：${researchStatusLabel(task.status)}`;
        }
    }
    catch (error) {
        researchError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        researchPolling.value = false;
    }
}
async function loadResearchResults() {
    const requestId = researchTask.value?.requestId;
    if (!requestId || researchResultsLoading.value)
        return;
    researchResultsLoading.value = true;
    try {
        const query = new URLSearchParams({
            page: String(researchResultPage.value),
            pageSize: String(researchResultPageSize.value),
            sort: researchResultSort.value,
        });
        const data = await fetchJson(`/api/c5-research/scans/${encodeURIComponent(requestId)}/results?${query.toString()}`);
        researchResults.value = normalizeResearchResults(data);
        researchError.value = "";
    }
    catch (error) {
        researchError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        researchResultsLoading.value = false;
    }
}
async function controlResearchTask(action) {
    const requestId = researchTask.value?.requestId;
    if (!requestId || researchControlAction.value)
        return;
    researchControlAction.value = action;
    researchError.value = "";
    try {
        await fetchJson(`/api/c5-research/scans/${encodeURIComponent(requestId)}/${action}`, { method: "POST", body: JSON.stringify({}) });
        researchMessage.value = {
            pause: "暂停请求已提交，等待状态接口确认",
            resume: "恢复请求已提交，等待后台继续扫描",
            cancel: "取消请求已提交；在状态接口返回取消终态前不会伪装完成",
        }[action];
        await pollResearchTask(requestId);
        startResearchPolling();
    }
    catch (error) {
        researchError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        researchControlAction.value = "";
    }
}
async function turnResearchResultsPage(nextPage) {
    const bounded = Math.max(1, Math.min(researchResultPageCount.value, nextPage));
    if (bounded === researchResultPage.value)
        return;
    researchResultPage.value = bounded;
    await loadResearchResults();
}
async function changeResearchResultPageSize() {
    researchResultPage.value = 1;
    await loadResearchResults();
}
async function changeResearchResultSort() {
    researchResultPage.value = 1;
    await loadResearchResults();
}
async function addResearchResultToWatch(item) {
    const marketHashName = String(item.marketHashName || "").trim();
    if (!marketHashName || researchAddingItem.value)
        return;
    researchAddingItem.value = marketHashName;
    researchError.value = "";
    try {
        await fetchJson("/api/profit-trade/selection-watch", {
            method: "POST",
            body: JSON.stringify({ action: "add", marketHashName }),
        });
        researchAddedItems.value = new Set([...researchAddedItems.value, marketHashName]);
        researchMessage.value = `${marketHashName} 已加入自选观察；没有创建交易流水`;
        await loadItems(true);
    }
    catch (error) {
        researchError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        researchAddingItem.value = "";
    }
}
function csvCell(value) {
    return `"${String(value ?? "").replace(/"/g, '""')}"`;
}
function exportReport() {
    const headers = [
        "产品", "Market Hash Name", "品质", "磨损", "Steam买入", "C5售价",
        "C5预计到手", "单件收益", "当前ROI", "7日平均ROI", "正ROI占比",
        "库存状态", "推荐",
    ];
    const rows = sortedItems.value.map((item) => [
        item.name,
        item.marketHashName,
        compactRarityName(item.rarityName),
        item.wearName,
        item.steamBuyPrice,
        item.c5ListingPrice,
        item.c5ExpectedNetPrice,
        item.expectedProfit,
        item.expectedRoi,
        item.averageRoi7d,
        item.positiveRoiShare7d,
        adviceLabel(item),
        recommendationLabel(item),
    ]);
    const content = `\ufeff${[headers, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n")}`;
    const url = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `c5-t-monitor-${new Date().toISOString().slice(0, 10)}.csv`;
    anchor.click();
    URL.revokeObjectURL(url);
}
const selectedResearchItemClass = computed(() => (researchTaxonomy.value.itemClasses.find((option) => option.id === researchDraft.value.itemClassId) || null));
const researchSupportsWear = computed(() => itemClassSupportsWear(selectedResearchItemClass.value));
const researchSubtypeOptions = computed(() => taxonomyOptionsForContext(researchTaxonomy.value.subtypes, { itemClassId: researchDraft.value.itemClassId }));
const researchWeaponOptions = computed(() => taxonomyOptionsForContext(researchTaxonomy.value.weapons, {
    itemClassId: researchDraft.value.itemClassId,
    subtypeId: researchDraft.value.subtypeId,
}));
const researchRarityOptions = computed(() => taxonomyOptionsForContext(researchTaxonomy.value.rarities, {
    itemClassId: researchDraft.value.itemClassId,
    subtypeId: researchDraft.value.subtypeId,
}));
const researchVersionOptions = computed(() => taxonomyOptionsForContext(researchTaxonomy.value.versions, {
    itemClassId: researchDraft.value.itemClassId,
    subtypeId: researchDraft.value.subtypeId,
}));
const researchWearOptions = computed(() => wearOptionsForItemClass(taxonomyOptionsForContext(researchTaxonomy.value.wears, {
    itemClassId: researchDraft.value.itemClassId,
    subtypeId: researchDraft.value.subtypeId,
}), selectedResearchItemClass.value));
const researchPhaseOptions = computed(() => researchSupportsWear.value === false
    ? []
    : taxonomyOptionsForContext(researchTaxonomy.value.phases, {
        itemClassId: researchDraft.value.itemClassId,
        subtypeId: researchDraft.value.subtypeId,
    }));
const researchHasActiveTask = computed(() => Boolean(researchTask.value?.requestId && !researchTask.value.terminal));
const researchProgress = computed(() => {
    if (researchTask.value?.progressPct !== null && researchTask.value?.progressPct !== undefined) {
        return researchTask.value.progressPct;
    }
    const total = researchTask.value?.catalogMatchedCount || 0;
    return total > 0 ? (researchTask.value?.processedCount || 0) / total : 0;
});
const researchResultPageCount = computed(() => Math.max(1, Math.ceil(researchResults.value.total / researchResultPageSize.value)));
const researchCanPause = computed(() => ["queued", "running"].includes(String(researchTask.value?.status || "").toLowerCase()));
const researchCanResume = computed(() => (String(researchTask.value?.status || "").toLowerCase() === "paused"));
const researchCanCancel = computed(() => Boolean(researchTask.value?.requestId && !researchTask.value.terminal));
const itemTypes = computed(() => Array.from(new Set(payload.value.items.map((item) => String(item.itemType || "").trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b, "zh-CN")));
const qualities = computed(() => Array.from(new Set(payload.value.items.map((item) => compactRarityName(item.rarityName)).filter((name) => name !== "未知"))).sort((a, b) => a.localeCompare(b, "zh-CN")));
const sortedItems = computed(() => sortSelectionItems(filterSelectionItems(payload.value.items, appliedFilters.value)));
const pageCount = computed(() => Math.max(1, Math.ceil(sortedItems.value.length / pageSize)));
const pageItems = computed(() => {
    if (page.value > pageCount.value)
        page.value = pageCount.value;
    const start = (page.value - 1) * pageSize;
    return sortedItems.value.slice(start, start + pageSize);
});
const pageNumbers = computed(() => {
    const total = pageCount.value;
    if (total <= 5)
        return Array.from({ length: total }, (_, index) => index + 1);
    const current = page.value;
    if (current <= 3)
        return [1, 2, 3, 4, total];
    if (current >= total - 2)
        return [1, total - 3, total - 2, total - 1, total];
    const candidates = new Set([1, total, current - 1, current, current + 1]);
    return [...candidates].filter((value) => value >= 1 && value <= total).sort((a, b) => a - b);
});
const positiveOpportunityCount = computed(() => (payload.value.summary.positiveOpportunityCount
    ?? payload.value.items.filter((item) => (finiteNumber(item.expectedRoi) || 0) > 0).length));
const availablePriceCount = computed(() => (payload.value.summary.availablePriceCount
    ?? payload.value.items.filter((item) => finiteNumber(item.c5ListingPrice) !== null && finiteNumber(item.steamBuyPrice) !== null).length));
const positiveProfitTotal = computed(() => (payload.value.summary.positiveExpectedProfitTotal
    ?? payload.value.items.reduce((sum, item) => sum + Math.max(0, finiteNumber(item.expectedProfit) || 0), 0)));
const positiveCostTotal = computed(() => (payload.value.summary.positiveExpectedCostTotal
    ?? payload.value.items.reduce((sum, item) => {
        if ((finiteNumber(item.expectedRoi) || 0) <= 0)
            return sum;
        return sum + (finiteNumber(item.steamBuyPrice) || 0) * (finiteNumber(item.balanceDiscount) || 0);
    }, 0)));
const averagePositiveRoi = computed(() => {
    const provided = finiteNumber(payload.value.summary.averagePositiveRoi);
    if (provided !== null)
        return provided;
    const values = payload.value.items
        .map((item) => finiteNumber(item.expectedRoi))
        .filter((value) => value !== null && value > 0);
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
});
const opportunityRate = computed(() => (finiteNumber(payload.value.summary.positiveOpportunityRate)
    ?? (payload.value.activeCount > 0 ? positiveOpportunityCount.value / payload.value.activeCount : 0)));
const distribution = computed(() => {
    const supplied = payload.value.summary.distribution || {};
    const buckets = { high: 0, good: 0, low: 0, poor: 0 };
    const hasSummary = ["high", "good", "low", "poor"].every((key) => finiteNumber(supplied[key]) !== null);
    if (hasSummary) {
        buckets.high = Math.max(0, Math.round(finiteNumber(supplied.high) || 0));
        buckets.good = Math.max(0, Math.round(finiteNumber(supplied.good) || 0));
        buckets.low = Math.max(0, Math.round(finiteNumber(supplied.low) || 0));
        buckets.poor = Math.max(0, Math.round(finiteNumber(supplied.poor) || 0));
    }
    else {
        for (const item of payload.value.items) {
            const roi = finiteNumber(item.expectedRoi);
            if (roi === null)
                continue;
            if (roi >= 0.2)
                buckets.high += 1;
            else if (roi >= 0.1)
                buckets.good += 1;
            else if (roi >= 0.05)
                buckets.low += 1;
            else
                buckets.poor += 1;
        }
    }
    const total = Math.max(1, Object.values(buckets).reduce((sum, value) => sum + value, 0));
    return {
        ...buckets,
        highWidth: `${(buckets.high / total) * 100}%`,
        goodWidth: `${(buckets.good / total) * 100}%`,
        lowWidth: `${(buckets.low / total) * 100}%`,
        poorWidth: `${(buckets.poor / total) * 100}%`,
    };
});
const errorItemCount = computed(() => (payload.value.items.filter((item) => Boolean(item.lastError)).length));
const trendPoints = computed(() => {
    const points = selectedHistory.value?.trend || [];
    const values = points
        .map((point) => finiteNumber(point.expectedRoi))
        .filter((value) => value !== null);
    if (!values.length)
        return "0,60 320,60";
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    const span = Math.max(0.0001, maximum - minimum);
    return values.map((value, index) => {
        const x = values.length === 1 ? 160 : (index / (values.length - 1)) * 320;
        const y = 70 - ((value - minimum) / span) * 48;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
});
const trendFillPoints = computed(() => `${trendPoints.value} 320,76 0,76`);
const historySummary = computed(() => selectedHistory.value?.summary || {});
const durationBuckets = computed(() => {
    const rows = historySummary.value.roiDurationBuckets;
    if (Array.isArray(rows) && rows.length)
        return rows;
    return [
        { key: "high", label: "≥ 2.00%", seconds: 0, share: 0 },
        { key: "good", label: "1%~2%", seconds: 0, share: 0 },
        { key: "low", label: "0%~1%", seconds: 0, share: 0 },
        { key: "negative", label: "< 0%", seconds: 0, share: 0 },
    ];
});
const lastRefresh = computed(() => {
    const source = payload.value.generatedAt;
    if (!source)
        return "尚未读取";
    const date = new Date(source);
    return Number.isNaN(date.getTime())
        ? "尚未读取"
        : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
});
onMounted(async () => {
    await Promise.allSettled([loadItems(), loadResearchTaxonomy()]);
    const storedRequestId = window.localStorage.getItem(RESEARCH_REQUEST_STORAGE_KEY) || "";
    if (storedRequestId) {
        researchTask.value = normalizeResearchTask({ requestId: storedRequestId, status: "queued", researchOnly: true, canExecute: false }, storedRequestId);
        await pollResearchTask(storedRequestId);
        startResearchPolling();
    }
    pollTimer = window.setInterval(() => loadItems(true), 30000);
});
onBeforeUnmount(() => {
    window.clearInterval(pollTimer);
    window.clearTimeout(refreshTimer);
    stopResearchPolling();
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "monitor-shell" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "page-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
(__VLS_ctx.activeMode === "condition" ? "C5 条件扫描" : "C5 做T监控");
if (__VLS_ctx.activeMode === 'condition') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
}
if (__VLS_ctx.activeMode === 'watch') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "heading-actions" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.startScan) },
        ...{ class: "button primary heading-primary" },
        type: "button",
        disabled: (__VLS_ctx.scanning),
    });
    const __VLS_0 = {}.ScanLine;
    /** @type {[typeof __VLS_components.ScanLine, ]} */ ;
    // @ts-ignore
    const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
        size: (15),
    }));
    const __VLS_2 = __VLS_1({
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_1));
    (__VLS_ctx.scanning ? "正在提交…" : "开始扫描 C5 市场");
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "monitor-mode-switch" },
    'aria-label': "C5 扫描模式",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setActiveMode('condition');
        } },
    type: "button",
    ...{ class: ({ active: __VLS_ctx.activeMode === 'condition' }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setActiveMode('watch');
        } },
    type: "button",
    ...{ class: ({ active: __VLS_ctx.activeMode === 'watch' }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
if (__VLS_ctx.activeMode === 'watch') {
    if (__VLS_ctx.apiError || __VLS_ctx.actionMessage) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "notice" },
            ...{ class: ({ error: __VLS_ctx.apiError }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.apiError || __VLS_ctx.actionMessage);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.activeMode === 'watch'))
                        return;
                    if (!(__VLS_ctx.apiError || __VLS_ctx.actionMessage))
                        return;
                    __VLS_ctx.apiError = '';
                    __VLS_ctx.actionMessage = '';
                } },
            type: "button",
            title: "关闭",
        });
        const __VLS_4 = {}.X;
        /** @type {[typeof __VLS_components.X, ]} */ ;
        // @ts-ignore
        const __VLS_5 = __VLS_asFunctionalComponent(__VLS_4, new __VLS_4({
            size: (14),
        }));
        const __VLS_6 = __VLS_5({
            size: (14),
        }, ...__VLS_functionalComponentArgsRest(__VLS_5));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "kpis" },
        'aria-label': "今日扫描指标",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "kpi" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "kpi-icon" },
    });
    const __VLS_8 = {}.Layers3;
    /** @type {[typeof __VLS_components.Layers3, ]} */ ;
    // @ts-ignore
    const __VLS_9 = __VLS_asFunctionalComponent(__VLS_8, new __VLS_8({
        size: (27),
    }));
    const __VLS_10 = __VLS_9({
        size: (27),
    }, ...__VLS_functionalComponentArgsRest(__VLS_9));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "kpi-copy" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatCount(__VLS_ctx.payload.activeCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.formatDailyChange(__VLS_ctx.payload.summary.scannedItemsChangePct));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "kpi" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "kpi-icon" },
    });
    const __VLS_12 = {}.Activity;
    /** @type {[typeof __VLS_components.Activity, ]} */ ;
    // @ts-ignore
    const __VLS_13 = __VLS_asFunctionalComponent(__VLS_12, new __VLS_12({
        size: (27),
    }));
    const __VLS_14 = __VLS_13({
        size: (27),
    }, ...__VLS_functionalComponentArgsRest(__VLS_13));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "kpi-copy" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatCount(__VLS_ctx.positiveOpportunityCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.formatDailyChange(__VLS_ctx.payload.summary.positiveOpportunityChangePct));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "kpi" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "kpi-icon" },
    });
    const __VLS_16 = {}.Tag;
    /** @type {[typeof __VLS_components.Tag, ]} */ ;
    // @ts-ignore
    const __VLS_17 = __VLS_asFunctionalComponent(__VLS_16, new __VLS_16({
        size: (27),
    }));
    const __VLS_18 = __VLS_17({
        size: (27),
    }, ...__VLS_functionalComponentArgsRest(__VLS_17));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "kpi-copy" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatSummaryMoney(__VLS_ctx.positiveProfitTotal));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.formatDailyChange(__VLS_ctx.payload.summary.positiveExpectedProfitChangePct));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "kpi" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "kpi-icon" },
    });
    const __VLS_20 = {}.ChartPie;
    /** @type {[typeof __VLS_components.ChartPie, ]} */ ;
    // @ts-ignore
    const __VLS_21 = __VLS_asFunctionalComponent(__VLS_20, new __VLS_20({
        size: (27),
    }));
    const __VLS_22 = __VLS_21({
        size: (27),
    }, ...__VLS_functionalComponentArgsRest(__VLS_21));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "kpi-copy" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "filter-bar" },
        'aria-label': "C5 市场筛选",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        value: (__VLS_ctx.market),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        value: (__VLS_ctx.itemType),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [value] of __VLS_getVForSourceType((__VLS_ctx.itemTypes))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (value),
            value: (value),
        });
        (value);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        value: (__VLS_ctx.quality),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [value] of __VLS_getVForSourceType((__VLS_ctx.qualities))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (value),
            value: (value),
        });
        (value);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "range-inputs" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        inputmode: "decimal",
        'aria-label': "最低磨损",
    });
    (__VLS_ctx.wearMin);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        inputmode: "decimal",
        'aria-label': "最高磨损",
    });
    (__VLS_ctx.wearMax);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "range-inputs price" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        inputmode: "decimal",
        placeholder: "¥ 最低价",
        'aria-label': "最低价格",
    });
    (__VLS_ctx.priceMin);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        inputmode: "decimal",
        placeholder: "¥ 最高价",
        'aria-label': "最高价格",
    });
    (__VLS_ctx.priceMax);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "search-input" },
    });
    const __VLS_24 = {}.Search;
    /** @type {[typeof __VLS_components.Search, ]} */ ;
    // @ts-ignore
    const __VLS_25 = __VLS_asFunctionalComponent(__VLS_24, new __VLS_24({
        size: (14),
    }));
    const __VLS_26 = __VLS_25({
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_25));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onKeyup: (__VLS_ctx.applyFilters) },
        placeholder: "饰品名称 / 关键词",
    });
    (__VLS_ctx.keyword);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "filter-actions" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.resetFilters) },
        ...{ class: "button" },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.applyFilters) },
        ...{ class: "button primary" },
        type: "button",
    });
    const __VLS_28 = {}.ListFilter;
    /** @type {[typeof __VLS_components.ListFilter, ]} */ ;
    // @ts-ignore
    const __VLS_29 = __VLS_asFunctionalComponent(__VLS_28, new __VLS_28({
        size: (15),
    }));
    const __VLS_30 = __VLS_29({
        size: (15),
    }, ...__VLS_functionalComponentArgsRest(__VLS_29));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "opportunity" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "opportunity-summary" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "opportunity-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "opportunity-badge" },
    });
    const __VLS_32 = {}.ShoppingCart;
    /** @type {[typeof __VLS_components.ShoppingCart, ]} */ ;
    // @ts-ignore
    const __VLS_33 = __VLS_asFunctionalComponent(__VLS_32, new __VLS_32({
        size: (22),
    }));
    const __VLS_34 = __VLS_33({
        size: (22),
    }, ...__VLS_functionalComponentArgsRest(__VLS_33));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "op-stat" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatCount(__VLS_ctx.payload.activeCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "op-stat" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
        ...{ class: "green" },
    });
    (__VLS_ctx.formatCount(__VLS_ctx.positiveOpportunityCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.opportunityRate));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "op-stat" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatSummaryMoney(__VLS_ctx.positiveCostTotal));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "op-stat" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatSummaryMoney(__VLS_ctx.positiveProfitTotal));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "op-stat" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.averagePositiveRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "roi-distribution" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.selectedItem && __VLS_ctx.selectItem(__VLS_ctx.selectedItem, true);
            } },
        ...{ class: "distribution-more" },
        type: "button",
    });
    const __VLS_36 = {}.ChevronRight;
    /** @type {[typeof __VLS_components.ChevronRight, ]} */ ;
    // @ts-ignore
    const __VLS_37 = __VLS_asFunctionalComponent(__VLS_36, new __VLS_36({
        size: (12),
    }));
    const __VLS_38 = __VLS_37({
        size: (12),
    }, ...__VLS_functionalComponentArgsRest(__VLS_37));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "distribution-bar" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
        ...{ style: ({ width: __VLS_ctx.distribution.highWidth }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
        ...{ style: ({ width: __VLS_ctx.distribution.goodWidth }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
        ...{ style: ({ width: __VLS_ctx.distribution.lowWidth }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
        ...{ style: ({ width: __VLS_ctx.distribution.poorWidth }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "distribution-legend" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i)({
        ...{ class: "legend-dot" },
    });
    (__VLS_ctx.distribution.high);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i)({
        ...{ class: "legend-dot light" },
    });
    (__VLS_ctx.distribution.good);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i)({
        ...{ class: "legend-dot amber" },
    });
    (__VLS_ctx.distribution.low);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i)({
        ...{ class: "legend-dot gray" },
    });
    (__VLS_ctx.distribution.poor);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "ranking" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "ranking-head" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "rank-grid rank-header" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "rank-body" },
    });
    if (__VLS_ctx.loading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "table-empty" },
        });
    }
    else if (!__VLS_ctx.pageItems.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "table-empty" },
        });
    }
    for (const [item, index] of __VLS_getVForSourceType((__VLS_ctx.pageItems))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.activeMode === 'watch'))
                        return;
                    __VLS_ctx.selectItem(item, false);
                } },
            key: (item.marketHashName),
            type: "button",
            ...{ class: "rank-grid rank-row" },
            ...{ class: ({ selected: __VLS_ctx.selectedItem?.marketHashName === item.marketHashName }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "rank-number" },
        });
        ((__VLS_ctx.page - 1) * __VLS_ctx.pageSize + index + 1);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "product" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "product-thumb" },
        });
        if (item.imageUrl) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.img)({
                src: (item.imageUrl),
                alt: "",
            });
        }
        else {
            const __VLS_40 = {}.Target;
            /** @type {[typeof __VLS_components.Target, ]} */ ;
            // @ts-ignore
            const __VLS_41 = __VLS_asFunctionalComponent(__VLS_40, new __VLS_40({
                size: (18),
            }));
            const __VLS_42 = __VLS_41({
                size: (18),
            }, ...__VLS_functionalComponentArgsRest(__VLS_41));
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "product-copy" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (item.name || item.marketHashName);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (item.marketHashName);
        if (item.wearName) {
            (item.wearName);
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
            ...{ class: "quality-tag" },
            ...{ class: (__VLS_ctx.qualityTone(item)) },
        });
        (__VLS_ctx.compactRarityName(item.rarityName));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "money" },
        });
        (__VLS_ctx.formatMoney(item.steamBuyPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "money" },
        });
        (__VLS_ctx.formatMoney(item.c5ListingPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "money" },
        });
        (__VLS_ctx.formatMoney(item.c5ExpectedNetPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "roi" },
            ...{ class: ({ negative: (__VLS_ctx.finiteNumber(item.expectedProfit) || 0) < 0 }) },
        });
        (__VLS_ctx.formatSignedMoney(item.expectedProfit));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "roi" },
            ...{ class: ({ negative: (__VLS_ctx.finiteNumber(item.expectedRoi) || 0) < 0, neutral: (__VLS_ctx.finiteNumber(item.expectedRoi) || 0) >= 0 && (__VLS_ctx.finiteNumber(item.expectedRoi) || 0) < 0.006 }) },
        });
        (__VLS_ctx.formatPercent(item.expectedRoi));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "roi" },
            ...{ class: ({ negative: (__VLS_ctx.finiteNumber(item.averageRoi7d) || 0) < 0, neutral: __VLS_ctx.finiteNumber(item.averageRoi7d) === null || ((__VLS_ctx.finiteNumber(item.averageRoi7d) || 0) >= 0 && (__VLS_ctx.finiteNumber(item.averageRoi7d) || 0) < 0.008) }) },
        });
        (__VLS_ctx.formatPercent(item.averageRoi7d));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "stability" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.formatPercent(item.positiveRoiShare7d));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
            ...{ class: "mini-track" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
            ...{ class: ({ amber: (__VLS_ctx.finiteNumber(item.positiveRoiShare7d) || 0) < 0.7 }) },
            ...{ style: ({ width: `${Math.max(0, Math.min(100, (__VLS_ctx.finiteNumber(item.positiveRoiShare7d) || 0) * 100))}%` }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
            ...{ class: "inventory-status" },
            ...{ class: (__VLS_ctx.inventoryAdvice(item)) },
        });
        (__VLS_ctx.adviceLabel(item));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
            ...{ class: "recommendation" },
            ...{ class: (__VLS_ctx.recommendationTone(item)) },
        });
        (__VLS_ctx.recommendationLabel(item));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "detail-cell" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.activeMode === 'watch'))
                        return;
                    __VLS_ctx.selectItem(item, true);
                } },
            ...{ class: "detail-link" },
            type: "button",
            title: "查看详情",
        });
        const __VLS_44 = {}.ChevronRight;
        /** @type {[typeof __VLS_components.ChevronRight, ]} */ ;
        // @ts-ignore
        const __VLS_45 = __VLS_asFunctionalComponent(__VLS_44, new __VLS_44({
            size: (14),
        }));
        const __VLS_46 = __VLS_45({
            size: (14),
        }, ...__VLS_functionalComponentArgsRest(__VLS_45));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "pagination" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.sortedItems.length);
    (__VLS_ctx.page);
    (__VLS_ctx.pageCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pages" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.page -= 1;
            } },
        ...{ class: "page-button" },
        type: "button",
        disabled: (__VLS_ctx.page <= 1),
        title: "上一页",
    });
    const __VLS_48 = {}.ChevronLeft;
    /** @type {[typeof __VLS_components.ChevronLeft, ]} */ ;
    // @ts-ignore
    const __VLS_49 = __VLS_asFunctionalComponent(__VLS_48, new __VLS_48({
        size: (14),
    }));
    const __VLS_50 = __VLS_49({
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_49));
    for (const [number, index] of __VLS_getVForSourceType((__VLS_ctx.pageNumbers))) {
        (number);
        if (index && number - __VLS_ctx.pageNumbers[index - 1] > 1) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.activeMode === 'watch'))
                        return;
                    __VLS_ctx.page = number;
                } },
            ...{ class: "page-button" },
            ...{ class: ({ active: __VLS_ctx.page === number }) },
            type: "button",
        });
        (number);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.page += 1;
            } },
        ...{ class: "page-button" },
        type: "button",
        disabled: (__VLS_ctx.page >= __VLS_ctx.pageCount),
        title: "下一页",
    });
    const __VLS_52 = {}.ChevronRight;
    /** @type {[typeof __VLS_components.ChevronRight, ]} */ ;
    // @ts-ignore
    const __VLS_53 = __VLS_asFunctionalComponent(__VLS_52, new __VLS_52({
        size: (14),
    }));
    const __VLS_54 = __VLS_53({
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_53));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "page-size" },
    });
    (__VLS_ctx.pageSize);
    const __VLS_56 = {}.ChevronDown;
    /** @type {[typeof __VLS_components.ChevronDown, ]} */ ;
    // @ts-ignore
    const __VLS_57 = __VLS_asFunctionalComponent(__VLS_56, new __VLS_56({
        size: (13),
    }));
    const __VLS_58 = __VLS_57({
        size: (13),
    }, ...__VLS_functionalComponentArgsRest(__VLS_57));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "bottom-panels" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "bottom-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "panel-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.selectedItem?.weaponName || __VLS_ctx.selectedItem?.marketHashName || "未选择产品");
    (__VLS_ctx.statisticsDays === 1 ? "24 小时" : `${__VLS_ctx.statisticsDays} 天`);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.selectedItem?.expectedRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "trend-wrap" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "trend-stats" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.historySummary.lowestRoi ?? __VLS_ctx.selectedItem?.lowestRoi7d));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.historySummary.averageRoi ?? __VLS_ctx.selectedItem?.averageRoi7d));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.historySummary.highestRoi ?? __VLS_ctx.selectedItem?.highestRoi7d));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.svg, __VLS_intrinsicElements.svg)({
        ...{ class: "trend-chart" },
        viewBox: "0 0 320 90",
        preserveAspectRatio: "none",
        'aria-label': "ROI 走势",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
        ...{ class: "trend-grid" },
        x1: "0",
        y1: "18",
        x2: "320",
        y2: "18",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
        ...{ class: "trend-grid" },
        x1: "0",
        y1: "42",
        x2: "320",
        y2: "42",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
        ...{ class: "trend-grid" },
        x1: "0",
        y1: "66",
        x2: "320",
        y2: "66",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.polygon)({
        ...{ class: "trend-fill" },
        points: (__VLS_ctx.trendFillPoints),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.polyline)({
        ...{ class: "trend-line" },
        points: (__VLS_ctx.trendPoints),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.text, __VLS_intrinsicElements.text)({
        ...{ class: "trend-axis" },
        x: "0",
        y: "88",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.text, __VLS_intrinsicElements.text)({
        ...{ class: "trend-axis" },
        x: "80",
        y: "88",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.text, __VLS_intrinsicElements.text)({
        ...{ class: "trend-axis" },
        x: "160",
        y: "88",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.text, __VLS_intrinsicElements.text)({
        ...{ class: "trend-axis" },
        x: "240",
        y: "88",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.text, __VLS_intrinsicElements.text)({
        ...{ class: "trend-axis" },
        x: "300",
        y: "88",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "bottom-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "panel-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.historySummary.positiveRoiShare ?? __VLS_ctx.selectedItem?.positiveRoiShare7d));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "duration-list" },
    });
    for (const [bucket] of __VLS_getVForSourceType((__VLS_ctx.durationBuckets))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (bucket.key),
            ...{ class: "duration-row" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (bucket.label);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
            ...{ class: "duration-track" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
            ...{ style: ({ width: `${Math.max(0, Math.min(100, bucket.share * 100))}%` }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (__VLS_ctx.formatDuration(bucket.seconds));
        (Math.round(bucket.share * 100));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "bottom-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "panel-title" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "monitor-status" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "status-line" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
        ...{ class: "online" },
    });
    (__VLS_ctx.apiError ? "离线" : "运行中");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "status-line" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "status-line" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatCount(__VLS_ctx.errorItemCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "status-line" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
}
else {
    if (__VLS_ctx.researchError || __VLS_ctx.researchMessage) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "notice research-notice" },
            ...{ class: ({ error: __VLS_ctx.researchError }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.researchError || __VLS_ctx.researchMessage);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.activeMode === 'watch'))
                        return;
                    if (!(__VLS_ctx.researchError || __VLS_ctx.researchMessage))
                        return;
                    __VLS_ctx.researchError = '';
                    __VLS_ctx.researchMessage = '';
                } },
            type: "button",
            title: "关闭",
        });
        const __VLS_60 = {}.X;
        /** @type {[typeof __VLS_components.X, ]} */ ;
        // @ts-ignore
        const __VLS_61 = __VLS_asFunctionalComponent(__VLS_60, new __VLS_60({
            size: (14),
        }));
        const __VLS_62 = __VLS_61({
            size: (14),
        }, ...__VLS_functionalComponentArgsRest(__VLS_61));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "research-filter-panel" },
        'aria-label': "C5 条件扫描筛选",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "research-section-head" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    if (__VLS_ctx.researchTaxonomyLoading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (__VLS_ctx.formatCount(__VLS_ctx.researchTaxonomy.itemClasses.length));
        if (__VLS_ctx.researchTaxonomy.catalogVersion) {
            (__VLS_ctx.researchTaxonomy.catalogVersion);
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.loadResearchTaxonomy) },
        ...{ class: "button" },
        type: "button",
        disabled: (__VLS_ctx.researchTaxonomyLoading),
    });
    (__VLS_ctx.researchTaxonomyLoading ? "加载中…" : "刷新分类");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "research-filter-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        ...{ onChange: (...[$event]) => {
                if (!!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.onResearchItemClassChange();
            } },
        value: (__VLS_ctx.researchDraft.itemClassId),
        disabled: (__VLS_ctx.researchTaxonomyLoading),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.researchTaxonomy.itemClasses))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (option.id),
            value: (option.id),
        });
        (__VLS_ctx.researchOptionLabel(option));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        ...{ onChange: (__VLS_ctx.onResearchSubtypeChange) },
        value: (__VLS_ctx.researchDraft.subtypeId),
        disabled: (!__VLS_ctx.researchSubtypeOptions.length),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.researchSubtypeOptions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (option.id),
            value: (option.id),
        });
        (__VLS_ctx.researchOptionLabel(option));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        value: (__VLS_ctx.researchDraft.weaponId),
        disabled: (!__VLS_ctx.researchWeaponOptions.length),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.researchWeaponOptions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (option.id),
            value: (option.id),
        });
        (__VLS_ctx.researchOptionLabel(option));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        value: (__VLS_ctx.researchDraft.rarityId),
        disabled: (!__VLS_ctx.researchRarityOptions.length),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.researchRarityOptions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (option.id),
            value: (option.id),
        });
        (__VLS_ctx.researchOptionLabel(option));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        value: (__VLS_ctx.researchDraft.versionId),
        disabled: (!__VLS_ctx.researchVersionOptions.length),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.researchVersionOptions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (option.id),
            value: (option.id),
        });
        (__VLS_ctx.researchOptionLabel(option));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        value: (__VLS_ctx.researchDraft.wearId),
        disabled: (__VLS_ctx.researchSupportsWear === false || !__VLS_ctx.researchWearOptions.length),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.researchWearOptions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (option.id),
            value: (option.id),
        });
        (__VLS_ctx.researchOptionLabel(option));
    }
    if (__VLS_ctx.researchSupportsWear === false) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
            ...{ class: "field-hint" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "research-range" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        inputmode: "decimal",
        'aria-label': "条件扫描最低磨损",
        disabled: (__VLS_ctx.researchSupportsWear === false),
    });
    (__VLS_ctx.researchDraft.wearMin);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        inputmode: "decimal",
        'aria-label': "条件扫描最高磨损",
        disabled: (__VLS_ctx.researchSupportsWear === false),
    });
    (__VLS_ctx.researchDraft.wearMax);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        value: (__VLS_ctx.researchDraft.phaseId),
        disabled: (__VLS_ctx.researchSupportsWear === false || !__VLS_ctx.researchPhaseOptions.length),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.researchPhaseOptions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (option.id),
            value: (option.id),
        });
        (__VLS_ctx.researchOptionLabel(option));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field research-field--price" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "research-range" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        inputmode: "decimal",
        placeholder: "¥ 最低价",
        'aria-label': "条件扫描最低 C5 价格",
    });
    (__VLS_ctx.researchDraft.priceMin);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        inputmode: "decimal",
        placeholder: "¥ 最高价",
        'aria-label': "条件扫描最高 C5 价格",
    });
    (__VLS_ctx.researchDraft.priceMax);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "research-field research-field--keyword" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "research-search" },
    });
    const __VLS_64 = {}.Search;
    /** @type {[typeof __VLS_components.Search, ]} */ ;
    // @ts-ignore
    const __VLS_65 = __VLS_asFunctionalComponent(__VLS_64, new __VLS_64({
        size: (14),
    }));
    const __VLS_66 = __VLS_65({
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_65));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onKeyup: (...[$event]) => {
                if (!!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.applyResearchFilters();
            } },
        placeholder: "中文名 / Market Hash Name",
    });
    (__VLS_ctx.researchDraft.keyword);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "research-filter-actions" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.resetResearchFilters) },
        ...{ class: "button" },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.applyResearchFilters();
            } },
        ...{ class: "button" },
        type: "button",
    });
    const __VLS_68 = {}.ListFilter;
    /** @type {[typeof __VLS_components.ListFilter, ]} */ ;
    // @ts-ignore
    const __VLS_69 = __VLS_asFunctionalComponent(__VLS_68, new __VLS_68({
        size: (14),
    }));
    const __VLS_70 = __VLS_69({
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_69));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.estimateResearchScan) },
        ...{ class: "button" },
        type: "button",
        disabled: (__VLS_ctx.researchEstimating),
    });
    (__VLS_ctx.researchEstimating ? "估算中…" : "估算");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.startResearchScan) },
        ...{ class: "button primary" },
        type: "button",
        disabled: (__VLS_ctx.researchSubmitting || __VLS_ctx.researchHasActiveTask),
    });
    const __VLS_72 = {}.ScanLine;
    /** @type {[typeof __VLS_components.ScanLine, ]} */ ;
    // @ts-ignore
    const __VLS_73 = __VLS_asFunctionalComponent(__VLS_72, new __VLS_72({
        size: (14),
    }));
    const __VLS_74 = __VLS_73({
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_73));
    (__VLS_ctx.researchSubmitting ? "正在提交…" : __VLS_ctx.researchHasActiveTask ? "已有任务运行" : "一键扫描全量");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "research-overview-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "research-estimate-card" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "research-metrics" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.researchEstimate ? __VLS_ctx.formatCount(__VLS_ctx.researchEstimate.catalogMatchedCount) : "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.researchEstimate ? __VLS_ctx.formatCount(__VLS_ctx.researchEstimate.requiresC5PriceCount) : "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.researchEstimate?.estimatedSeconds === null || !__VLS_ctx.researchEstimate ? "—" : __VLS_ctx.formatDuration(__VLS_ctx.researchEstimate.estimatedSeconds));
    if (__VLS_ctx.researchEstimate?.warnings.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.ul, __VLS_intrinsicElements.ul)({
            ...{ class: "research-warnings" },
        });
        for (const [warning] of __VLS_getVForSourceType((__VLS_ctx.researchEstimate.warnings))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
                key: (warning),
            });
            (warning);
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "research-task-card" },
        ...{ class: (`is-${__VLS_ctx.researchTask?.status || 'idle'}`) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.researchTask?.requestId || "尚未创建 requestId");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.researchStatusLabel(__VLS_ctx.researchTask?.status));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "research-progress" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
        ...{ style: ({ width: `${Math.round(__VLS_ctx.researchProgress * 100)}%` }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (Math.round(__VLS_ctx.researchProgress * 100));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "research-task-stats" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.formatCount(__VLS_ctx.researchTask?.catalogMatchedCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.formatCount(__VLS_ctx.researchTask?.processedCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.formatCount(__VLS_ctx.researchTask?.successCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.formatCount(__VLS_ctx.researchTask?.failedCount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.formatCount(__VLS_ctx.researchTask?.resultCount));
    if (__VLS_ctx.researchTask?.error) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "research-task-error" },
        });
        (__VLS_ctx.researchTask.error);
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        (__VLS_ctx.researchTask?.message || "202 只会显示排队；后续状态由 requestId 轮询更新。");
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.controlResearchTask('pause');
            } },
        ...{ class: "button" },
        type: "button",
        disabled: (!__VLS_ctx.researchCanPause || Boolean(__VLS_ctx.researchControlAction)),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.controlResearchTask('resume');
            } },
        ...{ class: "button" },
        type: "button",
        disabled: (!__VLS_ctx.researchCanResume || Boolean(__VLS_ctx.researchControlAction)),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.controlResearchTask('cancel');
            } },
        ...{ class: "button danger" },
        type: "button",
        disabled: (!__VLS_ctx.researchCanCancel || Boolean(__VLS_ctx.researchControlAction)),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "research-results-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "research-results-head" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        ...{ onChange: (__VLS_ctx.changeResearchResultSort) },
        value: (__VLS_ctx.researchResultSort),
        disabled: (!__VLS_ctx.researchTask?.requestId),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "roi_desc",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "roi_asc",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "c5_price_asc",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "c5_price_desc",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "steam_price_asc",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "steam_price_desc",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "updated_desc",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "catalog",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "research-result-grid research-result-header" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "research-result-body" },
    });
    if (__VLS_ctx.researchResultsLoading) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "table-empty" },
        });
    }
    else if (!__VLS_ctx.researchTask?.requestId) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "table-empty" },
        });
    }
    else if (!__VLS_ctx.researchResults.items.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "table-empty" },
        });
    }
    else {
        for (const [item] of __VLS_getVForSourceType((__VLS_ctx.researchResults.items))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                key: (String(item.id || item.marketHashName)),
                ...{ class: "research-result-grid research-result-row" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "research-product" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
            if (item.imageUrl) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.img)({
                    src: (item.imageUrl),
                    alt: "",
                });
            }
            else {
                const __VLS_76 = {}.Target;
                /** @type {[typeof __VLS_components.Target, ]} */ ;
                // @ts-ignore
                const __VLS_77 = __VLS_asFunctionalComponent(__VLS_76, new __VLS_76({
                    size: (17),
                }));
                const __VLS_78 = __VLS_77({
                    size: (17),
                }, ...__VLS_functionalComponentArgsRest(__VLS_77));
            }
            __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
            (item.name || item.marketHashName);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (item.marketHashName);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (item.itemClassName || item.itemType || "—");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (item.subtypeName || item.weaponName || "");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.compactRarityName(item.rarityName));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (item.versionName || "");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (item.wearName || "无磨损");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (item.phaseName || "");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.formatMoney(item.steamBuyPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.formatMoney(item.c5ListingPrice));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "roi" },
                ...{ class: ({ negative: (__VLS_ctx.finiteNumber(item.expectedProfit) || 0) < 0 }) },
            });
            (__VLS_ctx.formatSignedMoney(item.expectedProfit));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "roi" },
                ...{ class: ({ negative: (__VLS_ctx.finiteNumber(item.expectedRoi) || 0) < 0 }) },
            });
            (__VLS_ctx.formatPercent(item.expectedRoi));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "research-result-status" },
                ...{ class: ({ error: item.error }) },
            });
            (item.error || item.status || "已采集");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(__VLS_ctx.activeMode === 'watch'))
                            return;
                        if (!!(__VLS_ctx.researchResultsLoading))
                            return;
                        if (!!(!__VLS_ctx.researchTask?.requestId))
                            return;
                        if (!!(!__VLS_ctx.researchResults.items.length))
                            return;
                        __VLS_ctx.addResearchResultToWatch(item);
                    } },
                ...{ class: "button research-add-button" },
                type: "button",
                disabled: (__VLS_ctx.researchAddedItems.has(item.marketHashName) || __VLS_ctx.researchAddingItem === item.marketHashName),
            });
            (__VLS_ctx.researchAddedItems.has(item.marketHashName) ? "已加入" : __VLS_ctx.researchAddingItem === item.marketHashName ? "加入中…" : "加入自选观察");
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "research-results-pagination" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.formatCount(__VLS_ctx.researchResults.total));
    (__VLS_ctx.researchResultPage);
    (__VLS_ctx.researchResultPageCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.turnResearchResultsPage(__VLS_ctx.researchResultPage - 1);
            } },
        ...{ class: "page-button" },
        type: "button",
        disabled: (__VLS_ctx.researchResultPage <= 1 || __VLS_ctx.researchResultsLoading),
    });
    const __VLS_80 = {}.ChevronLeft;
    /** @type {[typeof __VLS_components.ChevronLeft, ]} */ ;
    // @ts-ignore
    const __VLS_81 = __VLS_asFunctionalComponent(__VLS_80, new __VLS_80({
        size: (14),
    }));
    const __VLS_82 = __VLS_81({
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_81));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.activeMode === 'watch'))
                    return;
                __VLS_ctx.turnResearchResultsPage(__VLS_ctx.researchResultPage + 1);
            } },
        ...{ class: "page-button" },
        type: "button",
        disabled: (__VLS_ctx.researchResultPage >= __VLS_ctx.researchResultPageCount || __VLS_ctx.researchResultsLoading),
    });
    const __VLS_84 = {}.ChevronRight;
    /** @type {[typeof __VLS_components.ChevronRight, ]} */ ;
    // @ts-ignore
    const __VLS_85 = __VLS_asFunctionalComponent(__VLS_84, new __VLS_84({
        size: (14),
    }));
    const __VLS_86 = __VLS_85({
        size: (14),
    }, ...__VLS_functionalComponentArgsRest(__VLS_85));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        ...{ onChange: (__VLS_ctx.changeResearchResultPageSize) },
        value: (__VLS_ctx.researchResultPageSize),
        disabled: (__VLS_ctx.researchResultsLoading),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: (10),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: (20),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: (50),
    });
}
if (__VLS_ctx.activeMode === 'watch' && __VLS_ctx.drawerOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.activeMode === 'watch' && __VLS_ctx.drawerOpen))
                    return;
                __VLS_ctx.drawerOpen = false;
            } },
        ...{ class: "drawer-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
        ...{ class: "detail-drawer" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.selectedItem?.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.selectedItem?.name);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.activeMode === 'watch' && __VLS_ctx.drawerOpen))
                    return;
                __VLS_ctx.drawerOpen = false;
            } },
        type: "button",
        title: "关闭",
    });
    const __VLS_88 = {}.X;
    /** @type {[typeof __VLS_components.X, ]} */ ;
    // @ts-ignore
    const __VLS_89 = __VLS_asFunctionalComponent(__VLS_88, new __VLS_88({
        size: (17),
    }));
    const __VLS_90 = __VLS_89({
        size: (17),
    }, ...__VLS_functionalComponentArgsRest(__VLS_89));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "drawer-body" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "drawer-summary" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.selectedItem?.expectedRoi));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.statisticsDays);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.historySummary.averageRoi ?? __VLS_ctx.selectedItem?.averageRoi7d));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatPercent(__VLS_ctx.historySummary.highestRoi ?? __VLS_ctx.selectedItem?.highestRoi7d));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "window-tabs" },
    });
    for (const [days] of __VLS_getVForSourceType(([1, 7, 30]))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.activeMode === 'watch' && __VLS_ctx.drawerOpen))
                        return;
                    __VLS_ctx.setStatisticsWindow(days);
                } },
            key: (days),
            type: "button",
            ...{ class: ({ active: __VLS_ctx.statisticsDays === days }) },
        });
        (days === 1 ? "24小时" : `${days}天`);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.svg, __VLS_intrinsicElements.svg)({
        ...{ class: "drawer-chart" },
        viewBox: "0 0 320 140",
        preserveAspectRatio: "none",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
        ...{ class: "trend-grid" },
        x1: "0",
        y1: "25",
        x2: "320",
        y2: "25",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
        ...{ class: "trend-grid" },
        x1: "0",
        y1: "70",
        x2: "320",
        y2: "70",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.line)({
        ...{ class: "trend-grid" },
        x1: "0",
        y1: "115",
        x2: "320",
        y2: "115",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.polyline)({
        ...{ class: "trend-line" },
        points: (__VLS_ctx.trendPoints),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "duration-list" },
    });
    for (const [bucket] of __VLS_getVForSourceType((__VLS_ctx.durationBuckets))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (bucket.key),
            ...{ class: "duration-row" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (bucket.label);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
            ...{ class: "duration-track" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span)({
            ...{ style: ({ width: `${bucket.share * 100}%` }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (__VLS_ctx.formatDuration(bucket.seconds));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "drawer-prices" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem?.steamBuyPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem?.c5ListingPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.selectedItem?.c5ExpectedNetPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.formatSignedMoney(__VLS_ctx.selectedItem?.expectedProfit));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "drawer-note" },
    });
    (__VLS_ctx.historyLoading ? "正在读取历史样本…" : `研究结论基于 ${__VLS_ctx.formatCount(__VLS_ctx.historySummary.validObservationCount || __VLS_ctx.historySummary.observedCount)} 个有效样本，仅用于选品，不会自动创建 Profit Trade 流水。`);
}
/** @type {__VLS_StyleScopedClasses['monitor-shell']} */ ;
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['page-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['heading-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['heading-primary']} */ ;
/** @type {__VLS_StyleScopedClasses['monitor-mode-switch']} */ ;
/** @type {__VLS_StyleScopedClasses['notice']} */ ;
/** @type {__VLS_StyleScopedClasses['kpis']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['kpi-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['range-inputs']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['range-inputs']} */ ;
/** @type {__VLS_StyleScopedClasses['price']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['search-input']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['opportunity']} */ ;
/** @type {__VLS_StyleScopedClasses['opportunity-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['opportunity-title']} */ ;
/** @type {__VLS_StyleScopedClasses['opportunity-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['op-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['op-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['green']} */ ;
/** @type {__VLS_StyleScopedClasses['op-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['op-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['op-stat']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-distribution']} */ ;
/** @type {__VLS_StyleScopedClasses['distribution-more']} */ ;
/** @type {__VLS_StyleScopedClasses['distribution-bar']} */ ;
/** @type {__VLS_StyleScopedClasses['distribution-legend']} */ ;
/** @type {__VLS_StyleScopedClasses['legend-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['legend-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['light']} */ ;
/** @type {__VLS_StyleScopedClasses['legend-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['amber']} */ ;
/** @type {__VLS_StyleScopedClasses['legend-dot']} */ ;
/** @type {__VLS_StyleScopedClasses['gray']} */ ;
/** @type {__VLS_StyleScopedClasses['ranking']} */ ;
/** @type {__VLS_StyleScopedClasses['ranking-head']} */ ;
/** @type {__VLS_StyleScopedClasses['rank-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['rank-header']} */ ;
/** @type {__VLS_StyleScopedClasses['rank-body']} */ ;
/** @type {__VLS_StyleScopedClasses['table-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['table-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['rank-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['rank-row']} */ ;
/** @type {__VLS_StyleScopedClasses['rank-number']} */ ;
/** @type {__VLS_StyleScopedClasses['product']} */ ;
/** @type {__VLS_StyleScopedClasses['product-thumb']} */ ;
/** @type {__VLS_StyleScopedClasses['product-copy']} */ ;
/** @type {__VLS_StyleScopedClasses['quality-tag']} */ ;
/** @type {__VLS_StyleScopedClasses['money']} */ ;
/** @type {__VLS_StyleScopedClasses['money']} */ ;
/** @type {__VLS_StyleScopedClasses['money']} */ ;
/** @type {__VLS_StyleScopedClasses['roi']} */ ;
/** @type {__VLS_StyleScopedClasses['roi']} */ ;
/** @type {__VLS_StyleScopedClasses['roi']} */ ;
/** @type {__VLS_StyleScopedClasses['stability']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['inventory-status']} */ ;
/** @type {__VLS_StyleScopedClasses['recommendation']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-cell']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-link']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['pages']} */ ;
/** @type {__VLS_StyleScopedClasses['page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['page-size']} */ ;
/** @type {__VLS_StyleScopedClasses['bottom-panels']} */ ;
/** @type {__VLS_StyleScopedClasses['bottom-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-title']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-stats']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-chart']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-fill']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-line']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-axis']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-axis']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-axis']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-axis']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-axis']} */ ;
/** @type {__VLS_StyleScopedClasses['bottom-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-title']} */ ;
/** @type {__VLS_StyleScopedClasses['duration-list']} */ ;
/** @type {__VLS_StyleScopedClasses['duration-row']} */ ;
/** @type {__VLS_StyleScopedClasses['duration-track']} */ ;
/** @type {__VLS_StyleScopedClasses['bottom-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-title']} */ ;
/** @type {__VLS_StyleScopedClasses['monitor-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-line']} */ ;
/** @type {__VLS_StyleScopedClasses['online']} */ ;
/** @type {__VLS_StyleScopedClasses['status-line']} */ ;
/** @type {__VLS_StyleScopedClasses['status-line']} */ ;
/** @type {__VLS_StyleScopedClasses['status-line']} */ ;
/** @type {__VLS_StyleScopedClasses['notice']} */ ;
/** @type {__VLS_StyleScopedClasses['research-notice']} */ ;
/** @type {__VLS_StyleScopedClasses['research-filter-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['research-section-head']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['research-filter-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['field-hint']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-range']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field--price']} */ ;
/** @type {__VLS_StyleScopedClasses['research-range']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field']} */ ;
/** @type {__VLS_StyleScopedClasses['research-field--keyword']} */ ;
/** @type {__VLS_StyleScopedClasses['research-search']} */ ;
/** @type {__VLS_StyleScopedClasses['research-filter-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['research-overview-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['research-estimate-card']} */ ;
/** @type {__VLS_StyleScopedClasses['research-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['research-warnings']} */ ;
/** @type {__VLS_StyleScopedClasses['research-task-card']} */ ;
/** @type {__VLS_StyleScopedClasses['research-progress']} */ ;
/** @type {__VLS_StyleScopedClasses['research-task-stats']} */ ;
/** @type {__VLS_StyleScopedClasses['research-task-error']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['danger']} */ ;
/** @type {__VLS_StyleScopedClasses['research-results-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['research-results-head']} */ ;
/** @type {__VLS_StyleScopedClasses['research-result-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['research-result-header']} */ ;
/** @type {__VLS_StyleScopedClasses['research-result-body']} */ ;
/** @type {__VLS_StyleScopedClasses['table-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['table-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['table-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['research-result-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['research-result-row']} */ ;
/** @type {__VLS_StyleScopedClasses['research-product']} */ ;
/** @type {__VLS_StyleScopedClasses['roi']} */ ;
/** @type {__VLS_StyleScopedClasses['roi']} */ ;
/** @type {__VLS_StyleScopedClasses['research-result-status']} */ ;
/** @type {__VLS_StyleScopedClasses['button']} */ ;
/** @type {__VLS_StyleScopedClasses['research-add-button']} */ ;
/** @type {__VLS_StyleScopedClasses['research-results-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['page-button']} */ ;
/** @type {__VLS_StyleScopedClasses['drawer-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-drawer']} */ ;
/** @type {__VLS_StyleScopedClasses['drawer-body']} */ ;
/** @type {__VLS_StyleScopedClasses['drawer-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['window-tabs']} */ ;
/** @type {__VLS_StyleScopedClasses['drawer-chart']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trend-line']} */ ;
/** @type {__VLS_StyleScopedClasses['duration-list']} */ ;
/** @type {__VLS_StyleScopedClasses['duration-row']} */ ;
/** @type {__VLS_StyleScopedClasses['duration-track']} */ ;
/** @type {__VLS_StyleScopedClasses['drawer-prices']} */ ;
/** @type {__VLS_StyleScopedClasses['drawer-note']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            Activity: Activity,
            ChartPie: ChartPie,
            ChevronDown: ChevronDown,
            ChevronLeft: ChevronLeft,
            ChevronRight: ChevronRight,
            Layers3: Layers3,
            ListFilter: ListFilter,
            ScanLine: ScanLine,
            Search: Search,
            ShoppingCart: ShoppingCart,
            Tag: Tag,
            Target: Target,
            X: X,
            adviceLabel: adviceLabel,
            compactRarityName: compactRarityName,
            finiteNumber: finiteNumber,
            formatCount: formatCount,
            formatDuration: formatDuration,
            formatMoney: formatMoney,
            formatPercent: formatPercent,
            formatSignedMoney: formatSignedMoney,
            inventoryAdvice: inventoryAdvice,
            qualityTone: qualityTone,
            recommendationLabel: recommendationLabel,
            recommendationTone: recommendationTone,
            payload: payload,
            loading: loading,
            scanning: scanning,
            apiError: apiError,
            actionMessage: actionMessage,
            page: page,
            pageSize: pageSize,
            selectedItem: selectedItem,
            historyLoading: historyLoading,
            drawerOpen: drawerOpen,
            statisticsDays: statisticsDays,
            market: market,
            itemType: itemType,
            quality: quality,
            wearMin: wearMin,
            wearMax: wearMax,
            priceMin: priceMin,
            priceMax: priceMax,
            keyword: keyword,
            activeMode: activeMode,
            researchTaxonomy: researchTaxonomy,
            researchTaxonomyLoading: researchTaxonomyLoading,
            researchDraft: researchDraft,
            researchEstimate: researchEstimate,
            researchEstimating: researchEstimating,
            researchSubmitting: researchSubmitting,
            researchResultsLoading: researchResultsLoading,
            researchControlAction: researchControlAction,
            researchAddingItem: researchAddingItem,
            researchAddedItems: researchAddedItems,
            researchError: researchError,
            researchMessage: researchMessage,
            researchTask: researchTask,
            researchResults: researchResults,
            researchResultPage: researchResultPage,
            researchResultPageSize: researchResultPageSize,
            researchResultSort: researchResultSort,
            formatDailyChange: formatDailyChange,
            formatSummaryMoney: formatSummaryMoney,
            selectItem: selectItem,
            setStatisticsWindow: setStatisticsWindow,
            startScan: startScan,
            applyFilters: applyFilters,
            resetFilters: resetFilters,
            researchOptionLabel: researchOptionLabel,
            researchStatusLabel: researchStatusLabel,
            setActiveMode: setActiveMode,
            loadResearchTaxonomy: loadResearchTaxonomy,
            onResearchItemClassChange: onResearchItemClassChange,
            onResearchSubtypeChange: onResearchSubtypeChange,
            applyResearchFilters: applyResearchFilters,
            resetResearchFilters: resetResearchFilters,
            estimateResearchScan: estimateResearchScan,
            startResearchScan: startResearchScan,
            controlResearchTask: controlResearchTask,
            turnResearchResultsPage: turnResearchResultsPage,
            changeResearchResultPageSize: changeResearchResultPageSize,
            changeResearchResultSort: changeResearchResultSort,
            addResearchResultToWatch: addResearchResultToWatch,
            researchSupportsWear: researchSupportsWear,
            researchSubtypeOptions: researchSubtypeOptions,
            researchWeaponOptions: researchWeaponOptions,
            researchRarityOptions: researchRarityOptions,
            researchVersionOptions: researchVersionOptions,
            researchWearOptions: researchWearOptions,
            researchPhaseOptions: researchPhaseOptions,
            researchHasActiveTask: researchHasActiveTask,
            researchProgress: researchProgress,
            researchResultPageCount: researchResultPageCount,
            researchCanPause: researchCanPause,
            researchCanResume: researchCanResume,
            researchCanCancel: researchCanCancel,
            itemTypes: itemTypes,
            qualities: qualities,
            sortedItems: sortedItems,
            pageCount: pageCount,
            pageItems: pageItems,
            pageNumbers: pageNumbers,
            positiveOpportunityCount: positiveOpportunityCount,
            positiveProfitTotal: positiveProfitTotal,
            positiveCostTotal: positiveCostTotal,
            averagePositiveRoi: averagePositiveRoi,
            opportunityRate: opportunityRate,
            distribution: distribution,
            errorItemCount: errorItemCount,
            trendPoints: trendPoints,
            trendFillPoints: trendFillPoints,
            historySummary: historySummary,
            durationBuckets: durationBuckets,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
