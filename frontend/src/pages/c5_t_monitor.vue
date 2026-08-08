<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  Activity,
  ChartPie,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Layers3,
  ListFilter,
  ScanLine,
  Search,
  ShoppingCart,
  Tag,
  Target,
  X,
} from "@lucide/vue";
import {
  C5_RESEARCH_NO_WEAR_ID,
  adviceLabel,
  buildC5ResearchEstimatePayload,
  buildC5ResearchFilterPayload,
  compactRarityName,
  filterSelectionItems,
  finiteNumber,
  formatCount,
  formatDuration,
  formatMoney,
  formatPercent,
  formatSignedMoney,
  historyWindowStart,
  inventoryAdvice,
  isC5ResearchTerminalStatus,
  itemClassSupportsWear,
  normalizeC5ResearchTaxonomy,
  optionalNumber,
  qualityTone,
  recommendationLabel,
  recommendationTone,
  sortSelectionItems,
  taxonomyOptionsForContext,
  wearOptionsForItemClass,
  type C5ResearchEstimate,
  type C5ResearchFilterDraft,
  type C5ResearchFilterPayload,
  type C5ResearchMode,
  type C5ResearchResultItem,
  type C5ResearchResultPage,
  type C5ResearchScanState,
  type C5ResearchTaxonomy,
  type C5ResearchTaxonomyOption,
  type MonitorFilters,
  type RoiDurationBucket,
  type SelectionHistoryPayload,
  type SelectionWatchItem,
  type SelectionWatchPayload,
} from "./c5_t_monitor_shared";

const EMPTY_PAYLOAD: SelectionWatchPayload = {
  researchOnly: true,
  canExecute: false,
  activeCount: 0,
  total: 0,
  page: 1,
  pageSize: 200,
  summary: {},
  items: [],
};

const EMPTY_RESEARCH_TAXONOMY: C5ResearchTaxonomy = {
  itemClasses: [],
  subtypes: [],
  weapons: [],
  rarities: [],
  versions: [],
  wears: [],
  phases: [],
};

const EMPTY_RESEARCH_RESULTS: C5ResearchResultPage = {
  items: [],
  total: 0,
  page: 1,
  pageSize: 20,
  sort: "roi_desc",
};

const RESEARCH_REQUEST_STORAGE_KEY = "c5-research:last-request-id";

function defaultResearchDraft(): C5ResearchFilterDraft {
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

const payload = ref<SelectionWatchPayload>(EMPTY_PAYLOAD);
const loading = ref(true);
const scanning = ref(false);
const apiError = ref("");
const actionMessage = ref("");
const page = ref(1);
const pageSize = 10;
const selectedItem = ref<SelectionWatchItem | null>(null);
const selectedHistory = ref<SelectionHistoryPayload | null>(null);
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
const appliedFilters = ref<MonitorFilters>({
  itemType: "",
  quality: "",
  wearMin: 0,
  wearMax: 1,
  priceMin: null,
  priceMax: null,
  keyword: "",
});
const activeMode = ref<C5ResearchMode>("condition");
const researchTaxonomy = ref<C5ResearchTaxonomy>(EMPTY_RESEARCH_TAXONOMY);
const researchTaxonomyLoading = ref(false);
const researchDraft = ref<C5ResearchFilterDraft>(defaultResearchDraft());
const researchAppliedFilters = ref<C5ResearchFilterPayload | null>(null);
const researchEstimate = ref<C5ResearchEstimate | null>(null);
const researchEstimating = ref(false);
const researchSubmitting = ref(false);
const researchPolling = ref(false);
const researchResultsLoading = ref(false);
const researchControlAction = ref("");
const researchAddingItem = ref("");
const researchAddedItems = ref<Set<string>>(new Set());
const researchError = ref("");
const researchMessage = ref("");
const researchTask = ref<C5ResearchScanState | null>(null);
const researchResults = ref<C5ResearchResultPage>(EMPTY_RESEARCH_RESULTS);
const researchResultPage = ref(1);
const researchResultPageSize = ref(20);
const researchResultSort = ref("roi_desc");
let pollTimer: number | undefined;
let refreshTimer: number | undefined;
let researchPollTimer: number | undefined;
let lastLoadedResearchResultCount = -1;

async function fetchJsonResponse<T>(
  path: string,
  options?: RequestInit,
): Promise<{ httpStatus: number; data: T }> {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
  });
  const data = await response.json().catch(() => ({})) as T & { error?: string };
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return { httpStatus: response.status, data };
}

async function fetchJson<T = Record<string, unknown>>(path: string, options?: RequestInit): Promise<T> {
  return (await fetchJsonResponse<T>(path, options)).data;
}

function researchRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function researchText(record: Record<string, unknown>, keys: string[], fallback = ""): string {
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null && String(value).trim()) return String(value).trim();
  }
  return fallback;
}

function researchNumber(record: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = finiteNumber(record[key]);
    if (value !== null) return value;
  }
  return null;
}

function researchTaxonomyName(
  options: C5ResearchTaxonomyOption[],
  id: string,
  fallback = "",
): string {
  return options.find((option) => option.id === id)?.name || fallback || id;
}

function normalizeResearchEstimate(value: unknown): C5ResearchEstimate {
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

function normalizeResearchTask(value: unknown, requestIdFallback = ""): C5ResearchScanState {
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

function normalizeResearchResults(value: unknown): C5ResearchResultPage {
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
      } satisfies C5ResearchResultItem;
    });
  return {
    items,
    total: Math.max(0, Math.round(researchNumber(root, ["total", "resultCount"]) || rawItems.length)),
    page: Math.max(1, Math.round(researchNumber(root, ["page"]) || researchResultPage.value)),
    pageSize: Math.max(1, Math.round(researchNumber(root, ["pageSize", "page_size"]) || researchResultPageSize.value)),
    sort: researchText(root, ["sort"], researchResultSort.value),
  };
}

function formatDailyChange(value: unknown): string {
  const number = finiteNumber(value);
  if (number === null) return "—";
  return `${number >= 0 ? "+" : ""}${formatPercent(number)} ${number >= 0 ? "↑" : "↓"}`;
}

function formatSummaryMoney(value: unknown): string {
  return formatMoney(value).replace("¥", "¥ ");
}

async function loadItems(silent = false) {
  if (!silent) loading.value = true;
  try {
    const data = await fetchJson(
      "/api/profit-trade/selection-watch?active=1&page=1&pageSize=200&sort=roi_desc",
    ) as SelectionWatchPayload;
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
    } else if (payload.value.items.length) {
      await selectItem(payload.value.items[0], false);
    } else {
      selectedItem.value = null;
      selectedHistory.value = null;
    }
  } catch (error) {
    apiError.value = error instanceof Error ? error.message : String(error);
  } finally {
    loading.value = false;
  }
}

async function loadHistory(item: SelectionWatchItem) {
  historyLoading.value = true;
  try {
    const query = new URLSearchParams({
      marketHashName: item.marketHashName,
      from: historyWindowStart(statisticsDays.value),
      page: "1",
      pageSize: "500",
    });
    const data = await fetchJson(
      `/api/profit-trade/selection-watch/history?${query.toString()}`,
    ) as SelectionHistoryPayload;
    selectedHistory.value = {
      ...data,
      summary: data.summary || {},
      trend: Array.isArray(data.trend)
        ? data.trend
        : Array.isArray((data.trend as unknown as { points?: unknown[] })?.points)
          ? (data.trend as unknown as { points: SelectionHistoryPayload["trend"] }).points
          : [],
      items: Array.isArray(data.items) ? data.items : [],
    };
  } catch (error) {
    actionMessage.value = "";
    apiError.value = error instanceof Error ? error.message : String(error);
  } finally {
    historyLoading.value = false;
  }
}

async function selectItem(item: SelectionWatchItem, openDrawer: boolean) {
  selectedItem.value = item;
  if (openDrawer) drawerOpen.value = true;
  await loadHistory(item);
}

async function setStatisticsWindow(days: number) {
  statisticsDays.value = days;
  if (selectedItem.value) await loadHistory(selectedItem.value);
}

async function startScan() {
  if (scanning.value) return;
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
  } catch (error) {
    apiError.value = error instanceof Error ? error.message : String(error);
  } finally {
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

function researchOptionLabel(option: C5ResearchTaxonomyOption): string {
  return option.count === undefined
    ? option.name
    : `${option.name}（${formatCount(option.count)}）`;
}

function researchStatusLabel(status: string | undefined): string {
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

function setActiveMode(mode: C5ResearchMode) {
  activeMode.value = mode;
  drawerOpen.value = false;
  if (mode === "condition" && !researchTaxonomy.value.itemClasses.length) {
    void loadResearchTaxonomy();
  }
  if (mode === "watch") void loadItems(true);
}

async function loadResearchTaxonomy() {
  if (researchTaxonomyLoading.value) return;
  researchTaxonomyLoading.value = true;
  researchError.value = "";
  try {
    const data = await fetchJson<unknown>("/api/c5-research/taxonomy");
    const normalized = normalizeC5ResearchTaxonomy(data);
    if (!normalized.itemClasses.length) throw new Error("完整分类接口没有返回任何饰品大类");
    researchTaxonomy.value = normalized;
    onResearchItemClassChange(false);
  } catch (error) {
    researchError.value = error instanceof Error ? error.message : String(error);
  } finally {
    researchTaxonomyLoading.value = false;
  }
}

function keepResearchSelection(
  field: "subtypeId" | "weaponId" | "rarityId" | "versionId" | "wearId" | "phaseId",
  options: C5ResearchTaxonomyOption[],
) {
  const current = researchDraft.value[field];
  if (current && !options.some((option) => option.id === current)) researchDraft.value[field] = "";
}

function onResearchItemClassChange(announce = true) {
  researchDraft.value.subtypeId = "";
  researchDraft.value.weaponId = "";
  const supportsWear = researchSupportsWear.value;
  if (supportsWear === false) {
    const noWear = wearOptionsForItemClass(
      researchTaxonomy.value.wears,
      selectedResearchItemClass.value,
    )[0];
    researchDraft.value.wearId = noWear?.id || C5_RESEARCH_NO_WEAR_ID;
    researchDraft.value.phaseId = "";
  } else if (researchDraft.value.wearId === C5_RESEARCH_NO_WEAR_ID) {
    researchDraft.value.wearId = "";
  }
  keepResearchSelection("rarityId", researchRarityOptions.value);
  keepResearchSelection("versionId", researchVersionOptions.value);
  keepResearchSelection("wearId", researchWearOptions.value);
  keepResearchSelection("phaseId", researchPhaseOptions.value);
  researchEstimate.value = null;
  if (announce) researchMessage.value = "大类已切换，关联细类、武器、品质和磨损条件已同步";
}

function onResearchSubtypeChange() {
  researchDraft.value.weaponId = "";
  keepResearchSelection("rarityId", researchRarityOptions.value);
  keepResearchSelection("versionId", researchVersionOptions.value);
  keepResearchSelection("phaseId", researchPhaseOptions.value);
  researchEstimate.value = null;
}

function applyResearchFilters(announce = true): C5ResearchFilterPayload {
  const filters = buildC5ResearchFilterPayload(researchDraft.value, {
    supportsWear: researchSupportsWear.value,
  });
  researchAppliedFilters.value = filters;
  researchEstimate.value = null;
  researchError.value = "";
  if (announce) researchMessage.value = "筛选条件已应用；估算和扫描都会使用当前完整条件";
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
  if (researchEstimating.value) return;
  researchEstimating.value = true;
  researchError.value = "";
  try {
    const filters = applyResearchFilters(false);
    const data = await fetchJson<unknown>("/api/c5-research/estimate", {
      method: "POST",
      body: JSON.stringify(buildC5ResearchEstimatePayload(filters)),
    });
    researchEstimate.value = normalizeResearchEstimate(data);
    researchMessage.value = "估算完成；尚未创建扫描任务，也没有请求 Steam 行情";
  } catch (error) {
    researchError.value = error instanceof Error ? error.message : String(error);
  } finally {
    researchEstimating.value = false;
  }
}

function stopResearchPolling() {
  window.clearInterval(researchPollTimer);
  researchPollTimer = undefined;
}

function startResearchPolling() {
  stopResearchPolling();
  if (!researchTask.value?.requestId || researchTask.value.terminal) return;
  researchPollTimer = window.setInterval(() => void pollResearchTask(), 2500);
}

async function startResearchScan() {
  if (researchSubmitting.value || researchHasActiveTask.value) return;
  researchSubmitting.value = true;
  researchError.value = "";
  try {
    const filters = applyResearchFilters(false);
    const response = await fetchJsonResponse<Record<string, unknown>>("/api/c5-research/scans", {
      method: "POST",
      body: JSON.stringify(filters),
    });
    if (response.httpStatus !== 202) {
      throw new Error(`创建扫描必须返回 202，实际为 ${response.httpStatus}`);
    }
    const requestId = researchText(response.data, ["requestId", "request_id"]);
    if (!requestId) throw new Error("扫描已被接收，但响应缺少 requestId");
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
  } catch (error) {
    researchError.value = error instanceof Error ? error.message : String(error);
  } finally {
    researchSubmitting.value = false;
  }
}

async function pollResearchTask(requestId = researchTask.value?.requestId || "") {
  if (!requestId || researchPolling.value) return;
  researchPolling.value = true;
  try {
    const data = await fetchJson<unknown>(`/api/c5-research/scans/${encodeURIComponent(requestId)}`);
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
  } catch (error) {
    researchError.value = error instanceof Error ? error.message : String(error);
  } finally {
    researchPolling.value = false;
  }
}

async function loadResearchResults() {
  const requestId = researchTask.value?.requestId;
  if (!requestId || researchResultsLoading.value) return;
  researchResultsLoading.value = true;
  try {
    const query = new URLSearchParams({
      page: String(researchResultPage.value),
      pageSize: String(researchResultPageSize.value),
      sort: researchResultSort.value,
    });
    const data = await fetchJson<unknown>(
      `/api/c5-research/scans/${encodeURIComponent(requestId)}/results?${query.toString()}`,
    );
    researchResults.value = normalizeResearchResults(data);
    researchError.value = "";
  } catch (error) {
    researchError.value = error instanceof Error ? error.message : String(error);
  } finally {
    researchResultsLoading.value = false;
  }
}

async function controlResearchTask(action: "pause" | "resume" | "cancel") {
  const requestId = researchTask.value?.requestId;
  if (!requestId || researchControlAction.value) return;
  researchControlAction.value = action;
  researchError.value = "";
  try {
    await fetchJson<unknown>(
      `/api/c5-research/scans/${encodeURIComponent(requestId)}/${action}`,
      { method: "POST", body: JSON.stringify({}) },
    );
    researchMessage.value = {
      pause: "暂停请求已提交，等待状态接口确认",
      resume: "恢复请求已提交，等待后台继续扫描",
      cancel: "取消请求已提交；在状态接口返回取消终态前不会伪装完成",
    }[action];
    await pollResearchTask(requestId);
    startResearchPolling();
  } catch (error) {
    researchError.value = error instanceof Error ? error.message : String(error);
  } finally {
    researchControlAction.value = "";
  }
}

async function turnResearchResultsPage(nextPage: number) {
  const bounded = Math.max(1, Math.min(researchResultPageCount.value, nextPage));
  if (bounded === researchResultPage.value) return;
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

async function addResearchResultToWatch(item: C5ResearchResultItem) {
  const marketHashName = String(item.marketHashName || "").trim();
  if (!marketHashName || researchAddingItem.value) return;
  researchAddingItem.value = marketHashName;
  researchError.value = "";
  try {
    await fetchJson<unknown>("/api/profit-trade/selection-watch", {
      method: "POST",
      body: JSON.stringify({ action: "add", marketHashName }),
    });
    researchAddedItems.value = new Set([...researchAddedItems.value, marketHashName]);
    researchMessage.value = `${marketHashName} 已加入自选观察；没有创建交易流水`;
    await loadItems(true);
  } catch (error) {
    researchError.value = error instanceof Error ? error.message : String(error);
  } finally {
    researchAddingItem.value = "";
  }
}

function csvCell(value: unknown): string {
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

const selectedResearchItemClass = computed(() => (
  researchTaxonomy.value.itemClasses.find(
    (option) => option.id === researchDraft.value.itemClassId,
  ) || null
));

const researchSupportsWear = computed(() => itemClassSupportsWear(selectedResearchItemClass.value));

const researchSubtypeOptions = computed(() => taxonomyOptionsForContext(
  researchTaxonomy.value.subtypes,
  { itemClassId: researchDraft.value.itemClassId },
));

const researchWeaponOptions = computed(() => taxonomyOptionsForContext(
  researchTaxonomy.value.weapons,
  {
    itemClassId: researchDraft.value.itemClassId,
    subtypeId: researchDraft.value.subtypeId,
  },
));

const researchRarityOptions = computed(() => taxonomyOptionsForContext(
  researchTaxonomy.value.rarities,
  {
    itemClassId: researchDraft.value.itemClassId,
    subtypeId: researchDraft.value.subtypeId,
  },
));

const researchVersionOptions = computed(() => taxonomyOptionsForContext(
  researchTaxonomy.value.versions,
  {
    itemClassId: researchDraft.value.itemClassId,
    subtypeId: researchDraft.value.subtypeId,
  },
));

const researchWearOptions = computed(() => wearOptionsForItemClass(
  taxonomyOptionsForContext(
    researchTaxonomy.value.wears,
    {
      itemClassId: researchDraft.value.itemClassId,
      subtypeId: researchDraft.value.subtypeId,
    },
  ),
  selectedResearchItemClass.value,
));

const researchPhaseOptions = computed(() => researchSupportsWear.value === false
  ? []
  : taxonomyOptionsForContext(
      researchTaxonomy.value.phases,
      {
        itemClassId: researchDraft.value.itemClassId,
        subtypeId: researchDraft.value.subtypeId,
      },
    ));

const researchHasActiveTask = computed(() => Boolean(
  researchTask.value?.requestId && !researchTask.value.terminal,
));

const researchProgress = computed(() => {
  if (researchTask.value?.progressPct !== null && researchTask.value?.progressPct !== undefined) {
    return researchTask.value.progressPct;
  }
  const total = researchTask.value?.catalogMatchedCount || 0;
  return total > 0 ? (researchTask.value?.processedCount || 0) / total : 0;
});

const researchResultPageCount = computed(() => Math.max(
  1,
  Math.ceil(researchResults.value.total / researchResultPageSize.value),
));

const researchCanPause = computed(() => ["queued", "running"].includes(
  String(researchTask.value?.status || "").toLowerCase(),
));

const researchCanResume = computed(() => (
  String(researchTask.value?.status || "").toLowerCase() === "paused"
));

const researchCanCancel = computed(() => Boolean(
  researchTask.value?.requestId && !researchTask.value.terminal,
));

const itemTypes = computed(() => Array.from(new Set(
  payload.value.items.map((item) => String(item.itemType || "").trim()).filter(Boolean),
)).sort((a, b) => a.localeCompare(b, "zh-CN")));

const qualities = computed(() => Array.from(new Set(
  payload.value.items.map((item) => compactRarityName(item.rarityName)).filter((name) => name !== "未知"),
)).sort((a, b) => a.localeCompare(b, "zh-CN")));

const sortedItems = computed(() => sortSelectionItems(
  filterSelectionItems(payload.value.items, appliedFilters.value),
));

const pageCount = computed(() => Math.max(1, Math.ceil(sortedItems.value.length / pageSize)));
const pageItems = computed(() => {
  if (page.value > pageCount.value) page.value = pageCount.value;
  const start = (page.value - 1) * pageSize;
  return sortedItems.value.slice(start, start + pageSize);
});

const pageNumbers = computed(() => {
  const total = pageCount.value;
  if (total <= 5) return Array.from({ length: total }, (_, index) => index + 1);
  const current = page.value;
  if (current <= 3) return [1, 2, 3, 4, total];
  if (current >= total - 2) return [1, total - 3, total - 2, total - 1, total];
  const candidates = new Set([1, total, current - 1, current, current + 1]);
  return [...candidates].filter((value) => value >= 1 && value <= total).sort((a, b) => a - b);
});

const positiveOpportunityCount = computed(() => (
  payload.value.summary.positiveOpportunityCount
  ?? payload.value.items.filter((item) => (finiteNumber(item.expectedRoi) || 0) > 0).length
));
const availablePriceCount = computed(() => (
  payload.value.summary.availablePriceCount
  ?? payload.value.items.filter((item) => finiteNumber(item.c5ListingPrice) !== null && finiteNumber(item.steamBuyPrice) !== null).length
));
const positiveProfitTotal = computed(() => (
  payload.value.summary.positiveExpectedProfitTotal
  ?? payload.value.items.reduce((sum, item) => sum + Math.max(0, finiteNumber(item.expectedProfit) || 0), 0)
));
const positiveCostTotal = computed(() => (
  payload.value.summary.positiveExpectedCostTotal
  ?? payload.value.items.reduce((sum, item) => {
    if ((finiteNumber(item.expectedRoi) || 0) <= 0) return sum;
    return sum + (finiteNumber(item.steamBuyPrice) || 0) * (finiteNumber(item.balanceDiscount) || 0);
  }, 0)
));
const averagePositiveRoi = computed(() => {
  const provided = finiteNumber(payload.value.summary.averagePositiveRoi);
  if (provided !== null) return provided;
  const values = payload.value.items
    .map((item) => finiteNumber(item.expectedRoi))
    .filter((value): value is number => value !== null && value > 0);
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
});

const opportunityRate = computed(() => (
  finiteNumber(payload.value.summary.positiveOpportunityRate)
  ?? (payload.value.activeCount > 0 ? positiveOpportunityCount.value / payload.value.activeCount : 0)
));

const distribution = computed(() => {
  const supplied = payload.value.summary.distribution || {};
  const buckets = { high: 0, good: 0, low: 0, poor: 0 };
  const hasSummary = ["high", "good", "low", "poor"].every(
    (key) => finiteNumber(supplied[key]) !== null,
  );
  if (hasSummary) {
    buckets.high = Math.max(0, Math.round(finiteNumber(supplied.high) || 0));
    buckets.good = Math.max(0, Math.round(finiteNumber(supplied.good) || 0));
    buckets.low = Math.max(0, Math.round(finiteNumber(supplied.low) || 0));
    buckets.poor = Math.max(0, Math.round(finiteNumber(supplied.poor) || 0));
  } else {
    for (const item of payload.value.items) {
      const roi = finiteNumber(item.expectedRoi);
      if (roi === null) continue;
      if (roi >= 0.2) buckets.high += 1;
      else if (roi >= 0.1) buckets.good += 1;
      else if (roi >= 0.05) buckets.low += 1;
      else buckets.poor += 1;
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

const errorItemCount = computed(() => (
  payload.value.items.filter((item) => Boolean(item.lastError)).length
));

const trendPoints = computed(() => {
  const points = selectedHistory.value?.trend || [];
  const values = points
    .map((point) => finiteNumber(point.expectedRoi))
    .filter((value): value is number => value !== null);
  if (!values.length) return "0,60 320,60";
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
const durationBuckets = computed<RoiDurationBucket[]>(() => {
  const rows = historySummary.value.roiDurationBuckets;
  if (Array.isArray(rows) && rows.length) return rows;
  return [
    { key: "high", label: "≥ 2.00%", seconds: 0, share: 0 },
    { key: "good", label: "1%~2%", seconds: 0, share: 0 },
    { key: "low", label: "0%~1%", seconds: 0, share: 0 },
    { key: "negative", label: "< 0%", seconds: 0, share: 0 },
  ];
});

const lastRefresh = computed(() => {
  const source = payload.value.generatedAt;
  if (!source) return "尚未读取";
  const date = new Date(source);
  return Number.isNaN(date.getTime())
    ? "尚未读取"
    : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
});

onMounted(async () => {
  await Promise.allSettled([loadItems(), loadResearchTaxonomy()]);
  const storedRequestId = window.localStorage.getItem(RESEARCH_REQUEST_STORAGE_KEY) || "";
  if (storedRequestId) {
    researchTask.value = normalizeResearchTask(
      { requestId: storedRequestId, status: "queued", researchOnly: true, canExecute: false },
      storedRequestId,
    );
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
</script>

<template>
  <div class="monitor-shell">
    <main class="page">
      <section class="page-heading">
        <div>
          <h1>{{ activeMode === "condition" ? "C5 条件扫描" : "C5 做T监控" }}</h1>
          <p v-if="activeMode === 'condition'">按完整 CSGO-API 分类筛选并采集 C5 与 Steam 官方行情；仅用于研究，不会触发交易。</p>
          <p v-else>实时扫描 C5 市场，结合 Steam 官方盘口识别稳定做T机会，为后续选品与库存运营提供依据。</p>
        </div>
        <div v-if="activeMode === 'watch'" class="heading-actions">
          <button class="button primary heading-primary" type="button" :disabled="scanning" @click="startScan">
            <ScanLine :size="15" />{{ scanning ? "正在提交…" : "开始扫描 C5 市场" }}
          </button>
        </div>
      </section>

      <section class="monitor-mode-switch" aria-label="C5 扫描模式">
        <button
          type="button"
          :class="{ active: activeMode === 'condition' }"
          @click="setActiveMode('condition')"
        >
          条件扫描
          <small>完整品类 · 一键扫描筛选结果</small>
        </button>
        <button
          type="button"
          :class="{ active: activeMode === 'watch' }"
          @click="setActiveMode('watch')"
        >
          自选观察
          <small>保留现有观察、趋势与持续时间</small>
        </button>
        <span><b>只读研究</b> 不购买 · 不求购 · 不锁仓 · 不上架</span>
      </section>

      <template v-if="activeMode === 'watch'">
      <div v-if="apiError || actionMessage" class="notice" :class="{ error: apiError }">
        <span>{{ apiError || actionMessage }}</span>
        <button type="button" title="关闭" @click="apiError = ''; actionMessage = ''"><X :size="14" /></button>
      </div>

      <section class="kpis" aria-label="今日扫描指标">
        <article class="kpi">
          <span class="kpi-icon"><Layers3 :size="27" /></span>
          <div class="kpi-copy"><span>今日扫描饰品</span><strong>{{ formatCount(payload.activeCount) }}</strong><small>较昨日 <b>{{ formatDailyChange(payload.summary.scannedItemsChangePct) }}</b></small></div>
        </article>
        <article class="kpi">
          <span class="kpi-icon"><Activity :size="27" /></span>
          <div class="kpi-copy"><span>可做T机会</span><strong>{{ formatCount(positiveOpportunityCount) }}</strong><small>较昨日 <b>{{ formatDailyChange(payload.summary.positiveOpportunityChangePct) }}</b></small></div>
        </article>
        <article class="kpi">
          <span class="kpi-icon"><Tag :size="27" /></span>
          <div class="kpi-copy"><span>预计利润总额</span><strong>{{ formatSummaryMoney(positiveProfitTotal) }}</strong><small>较昨日 <b>{{ formatDailyChange(payload.summary.positiveExpectedProfitChangePct) }}</b></small></div>
        </article>
        <article class="kpi">
          <span class="kpi-icon"><ChartPie :size="27" /></span>
          <div class="kpi-copy"><span>库存周转率</span><strong>2.38</strong><small>较昨日 <b>+0.27 ↑</b></small></div>
        </article>
      </section>

      <section class="filter-bar" aria-label="C5 市场筛选">
        <label class="field"><span>市场</span><select v-model="market"><option>C5</option></select></label>
        <label class="field"><span>饰品类型</span><select v-model="itemType"><option value="">全部</option><option v-for="value in itemTypes" :key="value" :value="value">{{ value }}</option></select></label>
        <label class="field"><span>品质</span><select v-model="quality"><option value="">全部</option><option v-for="value in qualities" :key="value" :value="value">{{ value }}</option></select></label>
        <label class="field">
          <span>磨损区间</span>
          <span class="range-inputs"><input v-model="wearMin" inputmode="decimal" aria-label="最低磨损" /><i>~</i><input v-model="wearMax" inputmode="decimal" aria-label="最高磨损" /></span>
        </label>
        <label class="field">
          <span>价格区间</span>
          <span class="range-inputs price"><input v-model="priceMin" inputmode="decimal" placeholder="¥ 最低价" aria-label="最低价格" /><i>~</i><input v-model="priceMax" inputmode="decimal" placeholder="¥ 最高价" aria-label="最高价格" /></span>
        </label>
        <label class="field"><span>搜索</span><span class="search-input"><Search :size="14" /><input v-model="keyword" placeholder="饰品名称 / 关键词" @keyup.enter="applyFilters" /></span></label>
        <div class="filter-actions">
          <button class="button" type="button" @click="resetFilters">重置</button>
          <button class="button primary" type="button" @click="applyFilters"><ListFilter :size="15" />筛选</button>
        </div>
      </section>

      <section class="opportunity">
        <div class="opportunity-summary">
          <div class="opportunity-title"><span class="opportunity-badge"><ShoppingCart :size="22" /></span><strong>今日扫描概览（C5）</strong></div>
          <div class="op-stat"><span>扫描饰品</span><strong>{{ formatCount(payload.activeCount) }}</strong></div>
          <div class="op-stat"><span>可做T机会</span><strong class="green">{{ formatCount(positiveOpportunityCount) }} <small>({{ formatPercent(opportunityRate) }})</small></strong></div>
          <div class="op-stat"><span>预计成本</span><strong>{{ formatSummaryMoney(positiveCostTotal) }}</strong></div>
          <div class="op-stat"><span>预计利润</span><strong>{{ formatSummaryMoney(positiveProfitTotal) }}</strong></div>
          <div class="op-stat"><span>平均利润率</span><strong>{{ formatPercent(averagePositiveRoi) }}</strong></div>
        </div>
        <div class="roi-distribution">
          <header><span>机会分布（按利润率）</span><button class="distribution-more" type="button" @click="selectedItem && selectItem(selectedItem, true)">查看更多 <ChevronRight :size="12" /></button></header>
          <div class="distribution-bar">
            <span :style="{ width: distribution.highWidth }" /><span :style="{ width: distribution.goodWidth }" />
            <span :style="{ width: distribution.lowWidth }" /><span :style="{ width: distribution.poorWidth }" />
          </div>
          <div class="distribution-legend">
            <span><i class="legend-dot" />≥ 20%（{{ distribution.high }}）</span>
            <span><i class="legend-dot light" />10%~20%（{{ distribution.good }}）</span>
            <span><i class="legend-dot amber" />5%~10%（{{ distribution.low }}）</span>
            <span><i class="legend-dot gray" />&lt; 5%（{{ distribution.poor }}）</span>
          </div>
        </div>
      </section>

      <section class="ranking">
        <header class="ranking-head">
          <h2>做T产品推荐排行</h2>
          <span>按稳定推荐分排序，当前 ROI 不是唯一依据</span>
        </header>
        <div class="rank-grid rank-header">
          <span>排名</span><span>产品</span><span>品质</span><span>Steam 买入</span><span>C5 售价</span>
          <span>C5 预计到手</span><span>单件收益</span><span>当前 ROI</span><span>7日平均 ROI</span>
          <span>正 ROI 占比</span><span>库存状态</span><span>推荐</span><span>详情</span>
        </div>
        <div class="rank-body">
          <div v-if="loading" class="table-empty">正在读取研究观察池…</div>
          <div v-else-if="!pageItems.length" class="table-empty">当前筛选没有可展示的观察品类</div>
          <button
            v-for="(item, index) in pageItems"
            :key="item.marketHashName"
            type="button"
            class="rank-grid rank-row"
            :class="{ selected: selectedItem?.marketHashName === item.marketHashName }"
            @click="selectItem(item, false)"
          >
            <span class="rank-number">{{ (page - 1) * pageSize + index + 1 }}</span>
            <span class="product">
              <span class="product-thumb">
                <img v-if="item.imageUrl" :src="item.imageUrl" alt="" />
                <Target v-else :size="18" />
              </span>
              <span class="product-copy"><strong>{{ item.name || item.marketHashName }}</strong><small>{{ item.marketHashName }}<template v-if="item.wearName"> · {{ item.wearName }}</template></small></span>
            </span>
            <span><i class="quality-tag" :class="qualityTone(item)">{{ compactRarityName(item.rarityName) }}</i></span>
            <span class="money">{{ formatMoney(item.steamBuyPrice) }}</span>
            <span class="money">{{ formatMoney(item.c5ListingPrice) }}</span>
            <span class="money">{{ formatMoney(item.c5ExpectedNetPrice) }}</span>
            <span class="roi" :class="{ negative: (finiteNumber(item.expectedProfit) || 0) < 0 }">{{ formatSignedMoney(item.expectedProfit) }}</span>
            <span class="roi" :class="{ negative: (finiteNumber(item.expectedRoi) || 0) < 0, neutral: (finiteNumber(item.expectedRoi) || 0) >= 0 && (finiteNumber(item.expectedRoi) || 0) < 0.006 }">{{ formatPercent(item.expectedRoi) }}</span>
            <span class="roi" :class="{ negative: (finiteNumber(item.averageRoi7d) || 0) < 0, neutral: finiteNumber(item.averageRoi7d) === null || ((finiteNumber(item.averageRoi7d) || 0) >= 0 && (finiteNumber(item.averageRoi7d) || 0) < 0.008) }">{{ formatPercent(item.averageRoi7d) }}</span>
            <span class="stability"><strong>{{ formatPercent(item.positiveRoiShare7d) }}</strong><i class="mini-track"><span :class="{ amber: (finiteNumber(item.positiveRoiShare7d) || 0) < 0.7 }" :style="{ width: `${Math.max(0, Math.min(100, (finiteNumber(item.positiveRoiShare7d) || 0) * 100))}%` }" /></i></span>
            <span><i class="inventory-status" :class="inventoryAdvice(item)">{{ adviceLabel(item) }}</i></span>
            <span><i class="recommendation" :class="recommendationTone(item)">{{ recommendationLabel(item) }}</i></span>
            <span class="detail-cell"><button class="detail-link" type="button" title="查看详情" @click.stop="selectItem(item, true)"><ChevronRight :size="14" /></button></span>
          </button>
        </div>
        <footer class="pagination">
          <span>共 {{ sortedItems.length }} 项，第 {{ page }} / {{ pageCount }} 页</span>
          <div class="pages">
            <button class="page-button" type="button" :disabled="page <= 1" title="上一页" @click="page -= 1"><ChevronLeft :size="14" /></button>
            <template v-for="(number, index) in pageNumbers" :key="number">
              <span v-if="index && number - pageNumbers[index - 1] > 1">…</span>
              <button class="page-button" :class="{ active: page === number }" type="button" @click="page = number">{{ number }}</button>
            </template>
            <button class="page-button" type="button" :disabled="page >= pageCount" title="下一页" @click="page += 1"><ChevronRight :size="14" /></button>
            <span class="page-size">每页 {{ pageSize }} 项<ChevronDown :size="13" /></span>
          </div>
        </footer>
      </section>

      <section class="bottom-panels">
        <article class="bottom-panel">
          <header class="panel-title"><div><strong>首选产品 ROI 走势</strong><small>{{ selectedItem?.weaponName || selectedItem?.marketHashName || "未选择产品" }} · 最近 {{ statisticsDays === 1 ? "24 小时" : `${statisticsDays} 天` }}</small></div><span>当前 {{ formatPercent(selectedItem?.expectedRoi) }}</span></header>
          <div class="trend-wrap">
            <div class="trend-stats">
              <span>最低<strong>{{ formatPercent(historySummary.lowestRoi ?? selectedItem?.lowestRoi7d) }}</strong></span>
              <span>平均<strong>{{ formatPercent(historySummary.averageRoi ?? selectedItem?.averageRoi7d) }}</strong></span>
              <span>最高<strong>{{ formatPercent(historySummary.highestRoi ?? selectedItem?.highestRoi7d) }}</strong></span>
            </div>
            <svg class="trend-chart" viewBox="0 0 320 90" preserveAspectRatio="none" aria-label="ROI 走势">
              <line class="trend-grid" x1="0" y1="18" x2="320" y2="18" /><line class="trend-grid" x1="0" y1="42" x2="320" y2="42" /><line class="trend-grid" x1="0" y1="66" x2="320" y2="66" />
              <polygon class="trend-fill" :points="trendFillPoints" />
              <polyline class="trend-line" :points="trendPoints" />
              <text class="trend-axis" x="0" y="88">09:30</text><text class="trend-axis" x="80" y="88">15:30</text>
              <text class="trend-axis" x="160" y="88">21:30</text><text class="trend-axis" x="240" y="88">03:30</text>
              <text class="trend-axis" x="300" y="88">09:30</text>
            </svg>
          </div>
        </article>
        <article class="bottom-panel">
          <header class="panel-title"><div><strong>正 ROI 持续时间</strong><small>不是只看最高点</small></div><span>{{ formatPercent(historySummary.positiveRoiShare ?? selectedItem?.positiveRoiShare7d) }} 为正</span></header>
          <div class="duration-list">
            <div v-for="bucket in durationBuckets" :key="bucket.key" class="duration-row">
              <span>{{ bucket.label }}</span><i class="duration-track"><span :style="{ width: `${Math.max(0, Math.min(100, bucket.share * 100))}%` }" /></i><b>{{ formatDuration(bucket.seconds) }} · {{ Math.round(bucket.share * 100) }}%</b>
            </div>
          </div>
        </article>
        <article class="bottom-panel">
          <header class="panel-title"><div><strong>监控状态</strong><small>共享 Profit Trade 选品观察链路</small></div><span>研究模式</span></header>
          <div class="monitor-status">
            <div class="status-line"><span>调度状态</span><strong class="online">{{ apiError ? "离线" : "运行中" }}</strong></div>
            <div class="status-line"><span>采样间隔</span><strong>10 分钟</strong></div>
            <div class="status-line"><span>价格异常</span><strong>{{ formatCount(errorItemCount) }} 项</strong></div>
            <div class="status-line"><span>远端写操作</span><strong>永久禁用</strong></div>
          </div>
        </article>
      </section>
      </template>

      <template v-else>
        <div v-if="researchError || researchMessage" class="notice research-notice" :class="{ error: researchError }">
          <span>{{ researchError || researchMessage }}</span>
          <button type="button" title="关闭" @click="researchError = ''; researchMessage = ''"><X :size="14" /></button>
        </div>

        <section class="research-filter-panel" aria-label="C5 条件扫描筛选">
          <header class="research-section-head">
            <div>
              <strong>完整品类筛选</strong>
              <small v-if="researchTaxonomyLoading">正在读取本地完整分类…</small>
              <small v-else>
                {{ formatCount(researchTaxonomy.itemClasses.length) }} 个大类
                <template v-if="researchTaxonomy.catalogVersion"> · catalog {{ researchTaxonomy.catalogVersion }}</template>
              </small>
            </div>
            <button class="button" type="button" :disabled="researchTaxonomyLoading" @click="loadResearchTaxonomy">
              {{ researchTaxonomyLoading ? "加载中…" : "刷新分类" }}
            </button>
          </header>

          <div class="research-filter-grid">
            <label class="research-field">
              <span>饰品大类</span>
              <select v-model="researchDraft.itemClassId" :disabled="researchTaxonomyLoading" @change="onResearchItemClassChange()">
                <option value="">全部大类</option>
                <option v-for="option in researchTaxonomy.itemClasses" :key="option.id" :value="option.id">{{ researchOptionLabel(option) }}</option>
              </select>
            </label>
            <label class="research-field">
              <span>细类</span>
              <select v-model="researchDraft.subtypeId" :disabled="!researchSubtypeOptions.length" @change="onResearchSubtypeChange">
                <option value="">全部细类</option>
                <option v-for="option in researchSubtypeOptions" :key="option.id" :value="option.id">{{ researchOptionLabel(option) }}</option>
              </select>
            </label>
            <label class="research-field">
              <span>武器型号</span>
              <select v-model="researchDraft.weaponId" :disabled="!researchWeaponOptions.length">
                <option value="">全部型号</option>
                <option v-for="option in researchWeaponOptions" :key="option.id" :value="option.id">{{ researchOptionLabel(option) }}</option>
              </select>
            </label>
            <label class="research-field">
              <span>品质（rarity.id）</span>
              <select v-model="researchDraft.rarityId" :disabled="!researchRarityOptions.length">
                <option value="">全部品质</option>
                <option v-for="option in researchRarityOptions" :key="option.id" :value="option.id">{{ researchOptionLabel(option) }}</option>
              </select>
            </label>
            <label class="research-field">
              <span>版本</span>
              <select v-model="researchDraft.versionId" :disabled="!researchVersionOptions.length">
                <option value="">全部版本</option>
                <option v-for="option in researchVersionOptions" :key="option.id" :value="option.id">{{ researchOptionLabel(option) }}</option>
              </select>
            </label>
            <label class="research-field">
              <span>磨损档位（wear.id）</span>
              <select
                v-model="researchDraft.wearId"
                :disabled="researchSupportsWear === false || !researchWearOptions.length"
              >
                <option value="">全部磨损</option>
                <option v-for="option in researchWearOptions" :key="option.id" :value="option.id">{{ researchOptionLabel(option) }}</option>
              </select>
              <small v-if="researchSupportsWear === false" class="field-hint">非皮肤品类自动使用“无磨损”</small>
            </label>
            <label class="research-field">
              <span>磨损区间（0.00~1.00）</span>
              <span class="research-range">
                <input v-model="researchDraft.wearMin" inputmode="decimal" aria-label="条件扫描最低磨损" :disabled="researchSupportsWear === false" />
                <i>~</i>
                <input v-model="researchDraft.wearMax" inputmode="decimal" aria-label="条件扫描最高磨损" :disabled="researchSupportsWear === false" />
              </span>
            </label>
            <label class="research-field">
              <span>相位</span>
              <select v-model="researchDraft.phaseId" :disabled="researchSupportsWear === false || !researchPhaseOptions.length">
                <option value="">全部相位</option>
                <option v-for="option in researchPhaseOptions" :key="option.id" :value="option.id">{{ researchOptionLabel(option) }}</option>
              </select>
            </label>
            <label class="research-field research-field--price">
              <span>C5 价格区间</span>
              <span class="research-range">
                <input v-model="researchDraft.priceMin" inputmode="decimal" placeholder="¥ 最低价" aria-label="条件扫描最低 C5 价格" />
                <i>~</i>
                <input v-model="researchDraft.priceMax" inputmode="decimal" placeholder="¥ 最高价" aria-label="条件扫描最高 C5 价格" />
              </span>
            </label>
            <label class="research-field research-field--keyword">
              <span>搜索</span>
              <span class="research-search">
                <Search :size="14" />
                <input v-model="researchDraft.keyword" placeholder="中文名 / Market Hash Name" @keyup.enter="applyResearchFilters()" />
              </span>
            </label>
          </div>

          <footer class="research-filter-actions">
            <span>品质与五档磨损均提交稳定 ID；任务创建后冻结本次筛选。</span>
            <div>
              <button class="button" type="button" @click="resetResearchFilters">重置</button>
              <button class="button" type="button" @click="applyResearchFilters()"><ListFilter :size="14" />筛选</button>
              <button class="button" type="button" :disabled="researchEstimating" @click="estimateResearchScan">
                {{ researchEstimating ? "估算中…" : "估算" }}
              </button>
              <button class="button primary" type="button" :disabled="researchSubmitting || researchHasActiveTask" @click="startResearchScan">
                <ScanLine :size="14" />{{ researchSubmitting ? "正在提交…" : researchHasActiveTask ? "已有任务运行" : "一键扫描全量" }}
              </button>
            </div>
          </footer>
        </section>

        <section class="research-overview-grid">
          <article class="research-estimate-card">
            <header><strong>筛选估算</strong><span>不请求 Steam</span></header>
            <div class="research-metrics">
              <span><small>Catalog 命中</small><b>{{ researchEstimate ? formatCount(researchEstimate.catalogMatchedCount) : "—" }}</b></span>
              <span><small>需读取 C5</small><b>{{ researchEstimate ? formatCount(researchEstimate.requiresC5PriceCount) : "—" }}</b></span>
              <span><small>预计耗时</small><b>{{ researchEstimate?.estimatedSeconds === null || !researchEstimate ? "—" : formatDuration(researchEstimate.estimatedSeconds) }}</b></span>
            </div>
            <ul v-if="researchEstimate?.warnings.length" class="research-warnings">
              <li v-for="warning in researchEstimate.warnings" :key="warning">{{ warning }}</li>
            </ul>
            <p v-else>先估算可确认筛选命中规模；估算不会创建任务。</p>
          </article>

          <article class="research-task-card" :class="`is-${researchTask?.status || 'idle'}`">
            <header>
              <div><strong>扫描任务</strong><small>{{ researchTask?.requestId || "尚未创建 requestId" }}</small></div>
              <span>{{ researchStatusLabel(researchTask?.status) }}</span>
            </header>
            <div class="research-progress"><i><span :style="{ width: `${Math.round(researchProgress * 100)}%` }" /></i><b>{{ Math.round(researchProgress * 100) }}%</b></div>
            <div class="research-task-stats">
              <span>目标 <b>{{ formatCount(researchTask?.catalogMatchedCount) }}</b></span>
              <span>已处理 <b>{{ formatCount(researchTask?.processedCount) }}</b></span>
              <span>成功 <b>{{ formatCount(researchTask?.successCount) }}</b></span>
              <span>异常 <b>{{ formatCount(researchTask?.failedCount) }}</b></span>
              <span>结果 <b>{{ formatCount(researchTask?.resultCount) }}</b></span>
            </div>
            <p v-if="researchTask?.error" class="research-task-error">{{ researchTask.error }}</p>
            <p v-else>{{ researchTask?.message || "202 只会显示排队；后续状态由 requestId 轮询更新。" }}</p>
            <footer>
              <button class="button" type="button" :disabled="!researchCanPause || Boolean(researchControlAction)" @click="controlResearchTask('pause')">暂停</button>
              <button class="button" type="button" :disabled="!researchCanResume || Boolean(researchControlAction)" @click="controlResearchTask('resume')">恢复</button>
              <button class="button danger" type="button" :disabled="!researchCanCancel || Boolean(researchControlAction)" @click="controlResearchTask('cancel')">取消</button>
            </footer>
          </article>
        </section>

        <section class="research-results-panel">
          <header class="research-results-head">
            <div><strong>条件扫描结果</strong><small>只读研究结果；加入自选观察必须逐条明确操作</small></div>
            <label>排序
              <select v-model="researchResultSort" :disabled="!researchTask?.requestId" @change="changeResearchResultSort">
                <option value="roi_desc">ROI 从高到低</option>
                <option value="roi_asc">ROI 从低到高</option>
                <option value="c5_price_asc">C5 价格从低到高</option>
                <option value="c5_price_desc">C5 价格从高到低</option>
                <option value="steam_price_asc">Steam 价格从低到高</option>
                <option value="steam_price_desc">Steam 价格从高到低</option>
                <option value="updated_desc">最近更新</option>
                <option value="catalog">Catalog 顺序</option>
              </select>
            </label>
          </header>
          <div class="research-result-grid research-result-header">
            <span>产品</span><span>分类</span><span>品质 / 版本</span><span>磨损 / 相位</span>
            <span>Steam 卖盘</span><span>C5 售价</span><span>单件收益</span><span>ROI</span><span>状态</span><span>操作</span>
          </div>
          <div class="research-result-body">
            <div v-if="researchResultsLoading" class="table-empty">正在读取任务结果…</div>
            <div v-else-if="!researchTask?.requestId" class="table-empty">创建条件扫描后，结果将在这里按 requestId 分页展示</div>
            <div v-else-if="!researchResults.items.length" class="table-empty">当前任务暂时没有可展示结果</div>
            <div
              v-for="item in researchResults.items"
              v-else
              :key="String(item.id || item.marketHashName)"
              class="research-result-grid research-result-row"
            >
              <span class="research-product">
                <i><img v-if="item.imageUrl" :src="item.imageUrl" alt="" /><Target v-else :size="17" /></i>
                <b>{{ item.name || item.marketHashName }}<small>{{ item.marketHashName }}</small></b>
              </span>
              <span>{{ item.itemClassName || item.itemType || "—" }}<small>{{ item.subtypeName || item.weaponName || "" }}</small></span>
              <span>{{ compactRarityName(item.rarityName) }}<small>{{ item.versionName || "" }}</small></span>
              <span>{{ item.wearName || "无磨损" }}<small>{{ item.phaseName || "" }}</small></span>
              <span>{{ formatMoney(item.steamBuyPrice) }}</span>
              <span>{{ formatMoney(item.c5ListingPrice) }}</span>
              <span class="roi" :class="{ negative: (finiteNumber(item.expectedProfit) || 0) < 0 }">{{ formatSignedMoney(item.expectedProfit) }}</span>
              <span class="roi" :class="{ negative: (finiteNumber(item.expectedRoi) || 0) < 0 }">{{ formatPercent(item.expectedRoi) }}</span>
              <span class="research-result-status" :class="{ error: item.error }">{{ item.error || item.status || "已采集" }}</span>
              <span>
                <button
                  class="button research-add-button"
                  type="button"
                  :disabled="researchAddedItems.has(item.marketHashName) || researchAddingItem === item.marketHashName"
                  @click="addResearchResultToWatch(item)"
                >
                  {{ researchAddedItems.has(item.marketHashName) ? "已加入" : researchAddingItem === item.marketHashName ? "加入中…" : "加入自选观察" }}
                </button>
              </span>
            </div>
          </div>
          <footer class="research-results-pagination">
            <span>共 {{ formatCount(researchResults.total) }} 项，第 {{ researchResultPage }} / {{ researchResultPageCount }} 页</span>
            <div>
              <button class="page-button" type="button" :disabled="researchResultPage <= 1 || researchResultsLoading" @click="turnResearchResultsPage(researchResultPage - 1)"><ChevronLeft :size="14" /></button>
              <button class="page-button" type="button" :disabled="researchResultPage >= researchResultPageCount || researchResultsLoading" @click="turnResearchResultsPage(researchResultPage + 1)"><ChevronRight :size="14" /></button>
              <select v-model.number="researchResultPageSize" :disabled="researchResultsLoading" @change="changeResearchResultPageSize">
                <option :value="10">每页 10 项</option>
                <option :value="20">每页 20 项</option>
                <option :value="50">每页 50 项</option>
              </select>
            </div>
          </footer>
        </section>
      </template>
    </main>

    <div v-if="activeMode === 'watch' && drawerOpen" class="drawer-backdrop" @click.self="drawerOpen = false">
      <aside class="detail-drawer">
        <header>
          <div><strong>{{ selectedItem?.marketHashName }}</strong><small>{{ selectedItem?.name }}</small></div>
          <button type="button" title="关闭" @click="drawerOpen = false"><X :size="17" /></button>
        </header>
        <div class="drawer-body">
          <div class="drawer-summary">
            <span><small>当前 ROI</small><strong>{{ formatPercent(selectedItem?.expectedRoi) }}</strong></span>
            <span><small>{{ statisticsDays }}日平均</small><strong>{{ formatPercent(historySummary.averageRoi ?? selectedItem?.averageRoi7d) }}</strong></span>
            <span><small>历史最高</small><strong>{{ formatPercent(historySummary.highestRoi ?? selectedItem?.highestRoi7d) }}</strong></span>
          </div>
          <div class="window-tabs">
            <button v-for="days in [1, 7, 30]" :key="days" type="button" :class="{ active: statisticsDays === days }" @click="setStatisticsWindow(days)">{{ days === 1 ? "24小时" : `${days}天` }}</button>
          </div>
          <section><h3>ROI 走势</h3><svg class="drawer-chart" viewBox="0 0 320 140" preserveAspectRatio="none"><line class="trend-grid" x1="0" y1="25" x2="320" y2="25" /><line class="trend-grid" x1="0" y1="70" x2="320" y2="70" /><line class="trend-grid" x1="0" y1="115" x2="320" y2="115" /><polyline class="trend-line" :points="trendPoints" /></svg></section>
          <section><h3>ROI 区间持续时间</h3><div class="duration-list"><div v-for="bucket in durationBuckets" :key="bucket.key" class="duration-row"><span>{{ bucket.label }}</span><i class="duration-track"><span :style="{ width: `${bucket.share * 100}%` }" /></i><b>{{ formatDuration(bucket.seconds) }}</b></div></div></section>
          <div class="drawer-prices">
            <span><small>Steam 买入</small><strong>{{ formatMoney(selectedItem?.steamBuyPrice) }}</strong></span>
            <span><small>C5 售价</small><strong>{{ formatMoney(selectedItem?.c5ListingPrice) }}</strong></span>
            <span><small>C5 预计到手</small><strong>{{ formatMoney(selectedItem?.c5ExpectedNetPrice) }}</strong></span>
            <span><small>单件预计收益</small><strong>{{ formatSignedMoney(selectedItem?.expectedProfit) }}</strong></span>
          </div>
          <p class="drawer-note">{{ historyLoading ? "正在读取历史样本…" : `研究结论基于 ${formatCount(historySummary.validObservationCount || historySummary.observedCount)} 个有效样本，仅用于选品，不会自动创建 Profit Trade 流水。` }}</p>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped src="./c5_t_monitor.css"></style>
