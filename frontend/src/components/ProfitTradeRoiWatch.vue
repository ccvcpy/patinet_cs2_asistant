<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import FolioIcon from "./FolioIcon.vue";
import ProfitTradeManualExecutionDialog from "./ProfitTradeManualExecutionDialog.vue";
import ProfitTradeRoiHistoryDrawer from "./ProfitTradeRoiHistoryDrawer.vue";
import ProfitTradeRoiWatchCard from "./ProfitTradeRoiWatchCard.vue";
import type {
  ProfitTradeHistoryPage,
  ProfitTradeHistoryRange,
  ProfitTradeHistoryStats,
  ProfitTradeHistoryTrend,
  ProfitTradeItemSearchResult,
  ProfitTradeListingsCircuit,
  ProfitTradeWatchHistoryItem,
  ProfitTradeWatchItem,
  ProfitTradeWatchPage,
  ProfitTradeWatchPool,
  ProfitTradeWatchSummary,
} from "./profit_trade_roi_types";

const props = withDefaults(defineProps<{
  running?: boolean;
  executorEnabled?: boolean;
  allowRealExecution?: boolean;
}>(), {
  running: false,
  executorEnabled: false,
  allowRealExecution: false,
});

type InventoryFilter = "active" | "all" | "exited";
type PoolState = {
  rows: ProfitTradeWatchItem[];
  total: number;
  page: number;
  summary: ProfitTradeWatchSummary;
  loading: boolean;
  error: string;
};
type SelectedHistory = { pool: ProfitTradeWatchPool; row: ProfitTradeWatchItem };
type CatalogSearchPagination = {
  offset: number;
  limit: number;
  total: number;
  hasMore: boolean;
  nextOffset?: number | null;
};
type ManualExecutionTradeStatus = {
  id: number;
  tradeNo: string;
  status: string;
  stepKey: string;
  error?: string | null;
  purchaseRequestSent?: boolean | null;
};
type ManualExecutionBatchStatus = {
  requestId: string;
  taskKey: string;
  marketHashName: string;
  name: string;
  requestedQuantity: number;
  status: "pending" | "retry" | "running" | "completed" | "failed" | "cancelled" | string;
  terminal: boolean;
  summary: string;
  error?: string | null;
  queuedAt?: string | null;
  updatedAt?: string | null;
  completedAt?: string | null;
  nextAttemptAt?: string | null;
  counts: { created: number; bought: number; listed: number; failed: number };
  trades: ManualExecutionTradeStatus[];
};

const pageSize = 12;
const inventory = reactive<PoolState>({
  rows: [], total: 0, page: 1, summary: {}, loading: false, error: "",
});
const selection = reactive<PoolState>({
  rows: [], total: 0, page: 1, summary: {}, loading: false, error: "",
});
const inventoryKeywordDraft = ref("");
const inventoryKeyword = ref("");
const inventoryStatus = ref<InventoryFilter>("active");
const inventorySort = ref("roi_desc");
const listingsCircuit = ref<ProfitTradeListingsCircuit>({ status: "closed", isBlocking: false });
const listingsCooling = computed(() => listingsCircuit.value.status === "open");

const selectionQuery = ref("");
const selectionSuggestions = ref<ProfitTradeItemSearchResult[]>([]);
const selectedCatalogItem = ref<ProfitTradeItemSearchResult | null>(null);
const selectionSearchOpen = ref(false);
const selectionSearching = ref(false);
const selectionSearchHasMore = ref(false);
const selectionSearchNextOffset = ref(0);
const selectionAdding = ref(false);
const selectionActionError = ref("");
let selectionSearchTimer: ReturnType<typeof setTimeout> | null = null;

const selectedManualExecution = ref<ProfitTradeWatchItem | null>(null);
const manualExecutionSubmitting = ref(false);
const manualExecutionError = ref("");
const manualExecutionMessage = ref("");
const manualExecutionBatch = ref<ManualExecutionBatchStatus | null>(null);
const manualExecutionStatusError = ref("");
const manualExecutionStorageKey = "profitTrade.manualExecutionRequestId";
let manualExecutionPollTimer: ReturnType<typeof setTimeout> | null = null;
let manualExecutionStatusLoading = false;
let terminalBatchRefreshId = "";

const selectedHistory = ref<SelectedHistory | null>(null);
const history = reactive<{
  rows: ProfitTradeWatchHistoryItem[];
  total: number;
  page: number;
  loading: boolean;
  error: string;
  stats: ProfitTradeHistoryStats | null;
  trend: ProfitTradeHistoryTrend;
}>({
  rows: [],
  total: 0,
  page: 1,
  loading: false,
  error: "",
  stats: null,
  trend: { totalValidPoints: 0, sampled: false, points: [] },
});
const historyRange = ref<ProfitTradeHistoryRange>("7d");
let inventoryLoadSequence = 0;
let selectionLoadSequence = 0;

const inventoryPages = computed(() => Math.max(1, Math.ceil(inventory.total / pageSize)));
const selectionPages = computed(() => Math.max(1, Math.ceil(selection.total / pageSize)));
const historyPages = computed(() => Math.max(1, Math.ceil(history.total / 20)));
const inventoryItemCount = computed(() => numericOr(inventory.summary.activeItemCount, inventory.total));
const selectionItemCount = computed(() => numericOr(selection.summary.activeItemCount, selection.total));
const inventoryProfitTotal = computed(() => inventory.summary.currentExpectedProfitTotal ?? null);
const inventoryBuyOrderProfitTotal = computed(() => inventory.summary.buyOrderReferenceProfitTotal ?? null);
const inventoryLongBuyActiveOrders = computed(() => numericOr(
  inventory.summary.longBuyActiveOrders,
  0,
));

function numericOr(value: number | null | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function money(value?: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `¥${value.toFixed(2)}` : "—";
}

function time(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function manualExecutionStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "排队中",
    retry: "等待重试",
    running: "执行中",
    completed: "已完成",
    failed: "执行失败",
    cancelled: "已取消",
  };
  return labels[status] || status || "状态未知";
}

function clearManualExecutionPoll(): void {
  if (!manualExecutionPollTimer) return;
  clearTimeout(manualExecutionPollTimer);
  manualExecutionPollTimer = null;
}

function rememberManualExecutionRequest(requestId: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(manualExecutionStorageKey, requestId);
}

function scheduleManualExecutionStatus(requestId: string, delay = 1500): void {
  clearManualExecutionPoll();
  manualExecutionPollTimer = setTimeout(() => {
    void loadManualExecutionStatus(requestId);
  }, delay);
}

async function loadManualExecutionStatus(requestId: string): Promise<void> {
  const normalized = requestId.trim();
  if (!normalized || manualExecutionStatusLoading) return;
  manualExecutionStatusLoading = true;
  try {
    const query = new URLSearchParams({ requestId: normalized });
    const response = await fetch(
      `/api/profit-trade/manual-execution/status?${query}`,
      { cache: "no-store" },
    );
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
    const payload = await response.json() as ManualExecutionBatchStatus;
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
    } else {
      scheduleManualExecutionStatus(payload.requestId);
    }
  } catch (reason) {
    manualExecutionStatusError.value = (
      `一键执行状态读取失败：${reason instanceof Error ? reason.message : String(reason)}`
    );
    scheduleManualExecutionStatus(normalized, 5000);
  } finally {
    manualExecutionStatusLoading = false;
  }
}

function restoreManualExecutionStatus(): void {
  if (typeof window === "undefined") return;
  const requestId = window.localStorage.getItem(manualExecutionStorageKey)?.trim();
  if (requestId) void loadManualExecutionStatus(requestId);
}

function dismissManualExecutionStatus(): void {
  clearManualExecutionPoll();
  manualExecutionBatch.value = null;
  manualExecutionStatusError.value = "";
  manualExecutionMessage.value = "";
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(manualExecutionStorageKey);
  }
}

async function responseError(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { error?: string; detail?: string };
    return payload.error || payload.detail || response.statusText;
  } catch {
    return response.statusText;
  }
}

function assignPoolPayload(pool: PoolState, payload: ProfitTradeWatchPage): void {
  pool.rows = Array.isArray(payload.items) ? payload.items : [];
  pool.total = numericOr(payload.total, 0);
  pool.summary = payload.summary || {};
}

async function loadInventory(): Promise<void> {
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
  });
  if (inventoryKeyword.value) query.set("keyword", inventoryKeyword.value);
  try {
    const response = await fetch(`/api/profit-trade/roi-watch?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as ProfitTradeWatchPage;
    if (requestSequence !== inventoryLoadSequence) return;
    assignPoolPayload(inventory, payload);
    listingsCircuit.value = payload.listingsCircuit || { status: "closed", isBlocking: false };
  } catch (reason) {
    if (requestSequence !== inventoryLoadSequence) return;
    inventory.error = `库存做T观察读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally {
    if (requestSequence === inventoryLoadSequence) inventory.loading = false;
  }
}

async function loadSelection(): Promise<void> {
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
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as ProfitTradeWatchPage;
    if (requestSequence !== selectionLoadSequence) return;
    assignPoolPayload(selection, payload);
  } catch (reason) {
    if (requestSequence !== selectionLoadSequence) return;
    selection.error = `全市场选品观察读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally {
    if (requestSequence === selectionLoadSequence) selection.loading = false;
  }
}

function loadAll(): void {
  void loadInventory();
  void loadSelection();
}

function searchInventory(): void {
  inventoryKeyword.value = inventoryKeywordDraft.value.trim();
  inventory.page = 1;
  void loadInventory();
}

function turnInventory(direction: -1 | 1): void {
  const next = inventory.page + direction;
  if (next < 1 || next > inventoryPages.value) return;
  inventory.page = next;
  void loadInventory();
}

function turnSelection(direction: -1 | 1): void {
  const next = selection.page + direction;
  if (next < 1 || next > selectionPages.value) return;
  selection.page = next;
  void loadSelection();
}

function manualExecutionMaxQuantity(row: ProfitTradeWatchItem): number {
  const saved = row.manualExecutableQuantity;
  const tradable = row.tradableCount;
  const value = typeof saved === "number" && Number.isFinite(saved)
    ? saved
    : typeof tradable === "number" && Number.isFinite(tradable) ? tradable : 0;
  return Math.max(0, Math.min(20, Math.floor(value)));
}

function manualExecutionDisabledReason(row: ProfitTradeWatchItem): string {
  if (!props.executorEnabled) return "请先开启 Profit Trade 执行器";
  if (!props.allowRealExecution) return "请先开放 Profit Trade 真实执行";
  if (row.active === false) return "已退出观察池，不能执行";
  if (typeof row.expectedRoi !== "number" || row.expectedRoi <= 0) return "当前没有可人工批准的正 ROI";
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
  if (manualExecutionMaxQuantity(row) <= 0) return "当前没有未锁定、可执行的资产";
  return "";
}

function openManualExecution(row: ProfitTradeWatchItem): void {
  const disabledReason = manualExecutionDisabledReason(row);
  manualExecutionMessage.value = "";
  if (disabledReason) {
    manualExecutionError.value = disabledReason;
    return;
  }
  manualExecutionError.value = "";
  selectedManualExecution.value = row;
}

function closeManualExecution(): void {
  if (manualExecutionSubmitting.value) return;
  selectedManualExecution.value = null;
  manualExecutionError.value = "";
}

async function confirmManualExecution(quantity: number): Promise<void> {
  const row = selectedManualExecution.value;
  if (!row || manualExecutionSubmitting.value) return;
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
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as {
      requestId?: string;
      taskKey?: string;
      marketHashName?: string;
      quantity?: number;
      approvedExpectedRoi?: number;
      requestedAt?: string;
    };
    if (!payload.requestId) throw new Error("后台未返回一键执行批次号");
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
  } catch (reason) {
    manualExecutionError.value = `一键执行提交失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally {
    manualExecutionSubmitting.value = false;
  }
}

async function searchSelectionCatalog(append = false): Promise<void> {
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
    const response = await fetch(
      `/api/profit-trade/items/search?query=${encodeURIComponent(query)}&limit=50&offset=${offset}`,
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as {
      items?: ProfitTradeItemSearchResult[];
      pagination?: CatalogSearchPagination;
    };
    if (query !== selectionQuery.value.trim()) return;
    const incoming = Array.isArray(payload.items) ? payload.items : [];
    if (append) {
      const merged = new Map(selectionSuggestions.value.map((item) => [item.marketHashName, item]));
      for (const item of incoming) merged.set(item.marketHashName, item);
      selectionSuggestions.value = [...merged.values()];
    } else {
      selectionSuggestions.value = incoming;
    }
    selectionSearchHasMore.value = Boolean(payload.pagination?.hasMore);
    selectionSearchNextOffset.value = Number(payload.pagination?.nextOffset ?? 0);
    selectionSearchOpen.value = true;
  } catch (reason) {
    selectionSuggestions.value = [];
    selectionSearchOpen.value = true;
    selectionActionError.value = `物品搜索失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally {
    selectionSearching.value = false;
  }
}

function onSelectionQueryInput(): void {
  selectedCatalogItem.value = null;
  selectionActionError.value = "";
  selectionSearchHasMore.value = false;
  selectionSearchNextOffset.value = 0;
  if (selectionSearchTimer) clearTimeout(selectionSearchTimer);
  if (!selectionQuery.value.trim()) {
    selectionSuggestions.value = [];
    selectionSearchHasMore.value = false;
    selectionSearchNextOffset.value = 0;
    selectionSearchOpen.value = false;
    return;
  }
  selectionSearchTimer = setTimeout(() => void searchSelectionCatalog(), 260);
}

function chooseSelectionItem(item: ProfitTradeItemSearchResult): void {
  selectedCatalogItem.value = item;
  selectionQuery.value = item.name === item.marketHashName
    ? item.marketHashName
    : `${item.name} / ${item.marketHashName}`;
  selectionSearchOpen.value = false;
  selectionActionError.value = "";
}

async function updateSelectionWatch(action: "add" | "remove" | "reactivate", marketHashName: string): Promise<boolean> {
  const response = await fetch("/api/profit-trade/selection-watch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, marketHashName }),
  });
  if (!response.ok) throw new Error(await responseError(response));
  return true;
}

async function addSelectionWatch(): Promise<void> {
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
  } catch (reason) {
    selectionActionError.value = `加入选品观察失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally {
    selectionAdding.value = false;
  }
}

async function removeSelectionWatch(row: ProfitTradeWatchItem): Promise<void> {
  selectionActionError.value = "";
  try {
    await updateSelectionWatch("remove", row.marketHashName);
    await loadSelection();
  } catch (reason) {
    selectionActionError.value = `移出选品观察失败：${reason instanceof Error ? reason.message : String(reason)}`;
  }
}

function historyRangeFrom(range: ProfitTradeHistoryRange): string | undefined {
  const days: Record<Exclude<ProfitTradeHistoryRange, "all">, number> = { "7d": 7, "30d": 30, "90d": 90 };
  if (range === "all") return undefined;
  return new Date(Date.now() - days[range] * 24 * 60 * 60 * 1000).toISOString();
}

async function openHistory(pool: ProfitTradeWatchPool, row: ProfitTradeWatchItem): Promise<void> {
  selectedHistory.value = { pool, row };
  history.page = 1;
  historyRange.value = "7d";
  history.rows = [];
  history.total = 0;
  history.stats = null;
  history.trend = { totalValidPoints: 0, sampled: false, points: [] };
  await loadHistory(true);
}

async function loadHistory(refreshOverview = true): Promise<void> {
  if (!selectedHistory.value) return;
  history.loading = true;
  history.error = "";
  const query = new URLSearchParams({
    marketHashName: selectedHistory.value.row.marketHashName,
    page: String(history.page),
    pageSize: "20",
  });
  const from = historyRangeFrom(historyRange.value);
  if (from) query.set("from", from);
  const endpoint = selectedHistory.value.pool === "selection"
    ? "/api/profit-trade/selection-watch/history"
    : "/api/profit-trade/roi-watch/history";
  try {
    const response = await fetch(`${endpoint}?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as ProfitTradeHistoryPage;
    history.rows = Array.isArray(payload.items) ? payload.items : [];
    history.total = numericOr(payload.total, 0);
    if (refreshOverview) {
      history.stats = payload.stats || null;
      history.trend = payload.trend || { totalValidPoints: 0, sampled: false, points: [] };
    }
  } catch (reason) {
    history.rows = [];
    history.total = 0;
    if (refreshOverview) {
      history.stats = null;
      history.trend = { totalValidPoints: 0, sampled: false, points: [] };
    }
    history.error = `历史读取失败：${reason instanceof Error ? reason.message : String(reason)}`;
  } finally {
    history.loading = false;
  }
}

function changeHistoryRange(range: ProfitTradeHistoryRange): void {
  if (historyRange.value === range) return;
  historyRange.value = range;
  history.page = 1;
  history.rows = [];
  history.total = 0;
  history.stats = null;
  history.trend = { totalValidPoints: 0, sampled: false, points: [] };
  void loadHistory(true);
}

function turnHistory(direction: -1 | 1): void {
  const next = history.page + direction;
  if (next < 1 || next > historyPages.value) return;
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
  if (selectionSearchTimer) clearTimeout(selectionSearchTimer);
  clearManualExecutionPoll();
  window.removeEventListener("profit-trade:refresh-observability", loadAll);
});
</script>

<template>
  <section class="roi-watch panel" aria-labelledby="roi-watch-title">
    <div class="watch-heading">
      <div>
        <p class="eyebrow">S1 · ROI 观察中心</p>
        <h2 id="roi-watch-title">库存做T与全市场选品观察</h2>
        <p>库存池服务真实做T判断；选品池只保存研究行情，两者严格隔离。</p>
      </div>
      <button class="secondary-button refresh-all" type="button" @click="loadAll"><FolioIcon name="refresh" :size="15" />刷新观察</button>
    </div>

    <div class="watch-summary" aria-label="ROI 观察汇总">
      <div><small>库存观察品类数</small><strong>{{ inventoryItemCount }}</strong><span>当前可交易库存</span></div>
      <div><small>选品观察品类数</small><strong>{{ selectionItemCount }}</strong><span>仅研究，不要求库存</span></div>
      <div><small>程序长期求购</small><strong>{{ inventoryLongBuyActiveOrders }}</strong><span>活跃、部分成交或终态核对中</span></div>
      <div><small>当前 ROI 预计收益总和</small><strong>{{ money(inventoryProfitTotal) }}</strong><span>库存可交易品类</span></div>
      <div><small>求购参考预计收益总和</small><strong>{{ money(inventoryBuyOrderProfitTotal) }}</strong><span>仅计有效参考盘口</span></div>
    </div>

    <div v-if="running" class="watch-running" role="status">
      <span></span><strong>本轮扫描与观察池更新中</strong><small>卡片暂时保留上一轮结果，后端完成后会替换为最新行情。</small>
    </div>
    <section
      v-if="manualExecutionBatch"
      class="manual-execution-status"
      :class="`is-${manualExecutionBatch.status}`"
      role="status"
      aria-live="polite"
    >
      <header>
        <div>
          <small>一键执行批次</small>
          <strong>{{ manualExecutionBatch.name || manualExecutionBatch.marketHashName }}</strong>
        </div>
        <span>{{ manualExecutionStatusLabel(manualExecutionBatch.status) }}</span>
      </header>
      <p>{{ manualExecutionBatch.summary }}</p>
      <dl>
        <div><dt>计划执行</dt><dd>{{ manualExecutionBatch.requestedQuantity }} 件</dd></div>
        <div><dt>已建流水</dt><dd>{{ manualExecutionBatch.counts.created }} 件</dd></div>
        <div><dt>已买到 B</dt><dd>{{ manualExecutionBatch.counts.bought }} 件</dd></div>
        <div><dt>已上架 C5</dt><dd>{{ manualExecutionBatch.counts.listed }} 件</dd></div>
        <div><dt>失败 / 取消</dt><dd>{{ manualExecutionBatch.counts.failed }} 件</dd></div>
      </dl>
      <p v-if="manualExecutionBatch.status === 'retry' && manualExecutionBatch.nextAttemptAt" class="batch-next">
        下次重试：{{ time(manualExecutionBatch.nextAttemptAt) }}
      </p>
      <ul v-if="manualExecutionBatch.trades.length" class="batch-trades">
        <li v-for="trade in manualExecutionBatch.trades" :key="trade.id">
          <span>{{ trade.tradeNo || `流水 ${trade.id}` }}</span>
          <strong>{{ trade.status }}</strong>
          <small v-if="trade.error">{{ trade.error }}</small>
        </li>
      </ul>
      <footer>
        <small>批次：{{ manualExecutionBatch.requestId }} · 更新时间：{{ time(manualExecutionBatch.updatedAt || manualExecutionBatch.queuedAt) }}</small>
        <button type="button" @click="dismissManualExecutionStatus">隐藏</button>
      </footer>
      <p v-if="manualExecutionStatusError" class="batch-refresh-error">{{ manualExecutionStatusError }}；页面会自动重试。</p>
    </section>
    <p v-if="manualExecutionMessage" class="manual-execution-message" role="status">{{ manualExecutionMessage }}</p>
    <p v-else-if="manualExecutionError && !selectedManualExecution" class="watch-error" role="alert">{{ manualExecutionError }}</p>
    <p v-if="listingsCooling" class="watch-circuit-note">
      <strong>Steam listings 冷却中：</strong>
      ROI 观察仍会更新；真实机会会重新校验并安全改走求购。冷却结束 {{ time(listingsCircuit.cooldownUntil) }} 后，下一次真实机会会恢复正常 listings 查询。
    </p>

    <div class="dual-watch-layout">
      <section class="watch-pool inventory-pool" aria-labelledby="inventory-watch-title">
        <header class="pool-header">
          <div><p class="eyebrow">库存做T观察池</p><h3 id="inventory-watch-title">已有可交易库存</h3><p>ROI 是主指标；满足条件的品类才可能进入真实执行链路。</p></div>
          <span>{{ inventoryItemCount }} 个品类</span>
        </header>

        <form class="inventory-toolbar" @submit.prevent="searchInventory">
          <label><span>品类搜索</span><input v-model="inventoryKeywordDraft" type="search" placeholder="中文名或 marketHashName"></label>
          <label><span>观察状态</span><select v-model="inventoryStatus"><option value="active">当前观察</option><option value="all">全部历史状态</option><option value="exited">已退出</option></select></label>
          <label><span>排序</span><select v-model="inventorySort"><option value="roi_desc">ROI 从高到低</option><option value="updated_desc">最近观察</option><option value="price_desc">C5 挂价从高到低</option></select></label>
          <button class="secondary-button" type="submit">查询</button>
        </form>

        <p v-if="inventory.error" class="watch-error">{{ inventory.error }}。不会使用静态或伪造数据替代。</p>
        <div v-if="inventory.loading" class="watch-empty">正在读取库存观察池…</div>
        <div v-else-if="!inventory.error && inventory.rows.length === 0" class="watch-empty">当前筛选条件下没有库存做T观察记录。</div>
        <div v-else class="pool-card-grid">
          <ProfitTradeRoiWatchCard
            v-for="row in inventory.rows"
            :key="row.marketHashName"
            :row="row"
            pool="inventory"
            :listings-cooling="listingsCooling"
            :manual-execution-disabled-reason="manualExecutionDisabledReason(row)"
            @open-history="openHistory('inventory', $event)"
            @manual-execute="openManualExecution"
          />
        </div>
        <footer class="pagination"><span>第 {{ inventory.page }} / {{ inventoryPages }} 页，共 {{ inventory.total }} 个品类</span><div><button class="mini-action" type="button" :disabled="inventory.page <= 1" @click="turnInventory(-1)">上一页</button><button class="mini-action" type="button" :disabled="inventory.page >= inventoryPages" @click="turnInventory(1)">下一页</button></div></footer>
      </section>

      <section class="watch-pool selection-pool" aria-labelledby="selection-watch-title">
        <header class="pool-header">
          <div><p class="eyebrow">全市场选品观察池</p><h3 id="selection-watch-title">未来选品研究</h3><p>可加入没有库存的任意品类，永远不创建流水、不锁资产、不买 Steam。</p></div>
          <span>{{ selectionItemCount }} 个品类</span>
        </header>

        <form class="selection-toolbar" @submit.prevent="addSelectionWatch">
          <label for="selection-item-search">中文 / 英文搜索</label>
          <div class="selection-input-row">
            <div class="selection-search">
              <input id="selection-item-search" v-model="selectionQuery" type="search" autocomplete="off" placeholder="输入中文名或英文名" @input="onSelectionQueryInput" @focus="selectionQuery.trim() && searchSelectionCatalog(false)">
              <button class="secondary-button search-button" type="button" :disabled="selectionSearching" @click="searchSelectionCatalog(false)">{{ selectionSearching ? "搜索中…" : "搜索" }}</button>
              <div v-if="selectionSearchOpen" class="selection-suggestions">
                <button v-for="item in selectionSuggestions" :key="item.marketHashName" type="button" @click="chooseSelectionItem(item)"><strong>{{ item.name }}</strong><small>{{ item.marketHashName }}</small></button>
                <button v-if="selectionSearchHasMore" class="catalog-load-more" type="button" :disabled="selectionSearching" @click="searchSelectionCatalog(true)">{{ selectionSearching ? "加载中…" : "加载更多结果" }}</button>
                <p v-if="!selectionSearching && selectionSuggestions.length === 0">没有匹配结果，请换一部分名称再试</p>
              </div>
            </div>
            <button class="primary-add-button" type="submit" :disabled="!selectedCatalogItem || selectionAdding">{{ selectionAdding ? "加入中…" : "加入选品观察" }}</button>
          </div>
          <small v-if="selectedCatalogItem" class="selected-item">已选择：{{ selectedCatalogItem.name }} · {{ selectedCatalogItem.marketHashName }}</small>
          <p class="selection-safety">仅研究 · 不要求库存 · 不创建流水</p>
        </form>

        <p v-if="selectionActionError" class="watch-error">{{ selectionActionError }}</p>
        <p v-if="selection.error" class="watch-error">{{ selection.error }}</p>
        <div v-if="selection.loading" class="watch-empty">正在读取全市场选品观察池…</div>
        <div v-else-if="!selection.error && selection.rows.length === 0" class="watch-empty">还没有选品观察。搜索中文名或英文名，选择准确饰品后点击“加入选品观察”。</div>
        <div v-else class="pool-card-grid">
          <ProfitTradeRoiWatchCard
            v-for="row in selection.rows"
            :key="row.marketHashName"
            :row="row"
            pool="selection"
            @open-history="openHistory('selection', $event)"
            @remove-selection="removeSelectionWatch"
          />
        </div>
        <footer class="pagination"><span>第 {{ selection.page }} / {{ selectionPages }} 页，共 {{ selection.total }} 个品类</span><div><button class="mini-action" type="button" :disabled="selection.page <= 1" @click="turnSelection(-1)">上一页</button><button class="mini-action" type="button" :disabled="selection.page >= selectionPages" @click="turnSelection(1)">下一页</button></div></footer>
      </section>
    </div>

    <ProfitTradeRoiHistoryDrawer
      :selected="selectedHistory?.row || null"
      :pool="selectedHistory?.pool || 'inventory'"
      :history="history.rows"
      :total="history.total"
      :page="history.page"
      :pages="historyPages"
      :loading="history.loading"
      :error="history.error"
      :stats="history.stats"
      :trend="history.trend"
      :range="historyRange"
      @close="selectedHistory = null"
      @change-range="changeHistoryRange"
      @change-page="turnHistory"
    />
    <ProfitTradeManualExecutionDialog
      :row="selectedManualExecution"
      :max-quantity="selectedManualExecution ? manualExecutionMaxQuantity(selectedManualExecution) : 0"
      :submitting="manualExecutionSubmitting"
      :error="manualExecutionError"
      @close="closeManualExecution"
      @confirm="confirmManualExecution"
    />
  </section>
</template>

<style scoped>
.roi-watch{padding:18px;border-color:#dfe5df;box-shadow:0 8px 24px rgba(27,62,44,.045)}.watch-heading{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.watch-heading h2{margin:0;color:#17201c;font-size:19px}.watch-heading p:not(.eyebrow){margin:6px 0 0;color:#6f7872;font-size:12px}.refresh-all{display:inline-flex;align-items:center;justify-content:center;gap:5px;white-space:nowrap}.watch-summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;margin:15px 0}.watch-summary>div{display:grid;gap:3px;padding:11px 12px;border:1px solid #e0e6e0;border-radius:9px;background:#fbfdfb}.watch-summary small,.watch-summary span{color:#728078;font-size:10px}.watch-summary strong{color:#246b4a;font-size:18px}.watch-running{display:flex;align-items:center;gap:8px;margin:12px 0;border:1px solid #b9d8c4;border-radius:8px;padding:9px 11px;color:#236a4c;background:#eef7f1}.watch-running>span{width:13px;height:13px;border:2px solid #bdd8c6;border-top-color:#236a4c;border-radius:50%;animation:watch-spin .8s linear infinite}.watch-running strong{font-size:11px}.watch-running small{color:#6f7872;font-size:10px}.manual-execution-message{margin:12px 0;border:1px solid #b9d8c4;border-radius:8px;padding:9px 11px;color:#236a4c;background:#eef7f1;font-size:11px;font-weight:700}@keyframes watch-spin{to{transform:rotate(360deg)}}.watch-circuit-note{margin:0 0 12px;padding:9px 11px;border:1px solid #dfc77e;border-radius:7px;color:#6b5b2c;background:#fff9e9;font-size:11px}.watch-circuit-note strong{color:#564718}.dual-watch-layout{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start}.watch-pool{min-width:0;padding:14px;border:1px solid #e0e6e0;border-radius:10px;background:#f9fbf9}.selection-pool{border-color:#d0e3d5;background:#f8fbf8}.pool-header{display:flex;justify-content:space-between;gap:12px}.pool-header h3{margin:0;color:#1c2821;font-size:16px}.pool-header p:not(.eyebrow){margin:5px 0 0;color:#6c7871;font-size:11px;line-height:1.55}.pool-header>span{height:max-content;padding:4px 7px;border-radius:999px;color:#286548;background:#e7f3ea;font-size:10px;font-weight:700;white-space:nowrap}.inventory-toolbar{display:grid;grid-template-columns:minmax(170px,1fr) 120px 145px auto;gap:7px;align-items:end;margin:13px 0}.inventory-toolbar label{display:grid;gap:4px}.inventory-toolbar label span,.selection-toolbar>label{color:#68736d;font-size:10px;font-weight:650}.inventory-toolbar input,.inventory-toolbar select,.selection-search input{min-width:0;min-height:34px;border:1px solid #d6ddd6;border-radius:6px;padding:6px 8px;color:#17201c;background:#fff}.watch-error{margin:10px 0;padding:9px 11px;border:1px solid #e7b8b2;border-radius:7px;color:#8d3d34;background:#fff7f5;font-size:11px}.watch-empty{display:grid;place-items:center;min-height:110px;color:#77817b;border:1px dashed #d8ded8;border-radius:8px;background:#fff;font-size:11px;text-align:center;padding:10px}.pool-card-grid{display:grid;grid-template-columns:1fr;gap:10px}.selection-toolbar{display:grid;gap:6px;margin:13px 0}.selection-input-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px;align-items:start}.selection-search{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px}.search-button{white-space:nowrap}.selection-suggestions{position:absolute;z-index:10;top:39px;left:0;right:0;max-height:250px;overflow:auto;border:1px solid #d8e1d8;border-radius:7px;background:#fff;box-shadow:0 9px 22px rgba(28,54,37,.13)}.selection-suggestions button{display:grid;width:100%;gap:2px;padding:8px 10px;border:0;border-bottom:1px solid #edf1ed;color:#243129;background:#fff;text-align:left}.selection-suggestions button:hover{background:#f2f8f3}.selection-suggestions small{overflow-wrap:anywhere;color:#76817b;font-size:9px}.selection-suggestions p{margin:0;padding:10px;color:#748078;font-size:10px}.primary-add-button{min-height:34px;border:1px solid #2d704e;border-radius:6px;padding:6px 10px;color:#fff;background:#28714d;font-size:11px;font-weight:700;white-space:nowrap}.primary-add-button:disabled{border-color:#c4d2c7;background:#b9c8bc;color:#f7faf7}.selected-item{color:#38644a;font-size:10px;overflow-wrap:anywhere}.selection-safety{margin:0;color:#557461;font-size:10px;font-weight:700}.pagination{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:13px;color:#6f7872;font-size:10px}.pagination>div{display:flex;gap:6px}.mini-action{border:1px solid #d6ded7;border-radius:6px;padding:5px 8px;color:#486258;background:#fff;font-size:10px}.mini-action:disabled{opacity:.45}.secondary-button{min-height:34px;border:1px solid #cbd8cd;border-radius:6px;padding:6px 9px;color:#2b6045;background:#fff;font-size:11px;font-weight:700}@media (max-width:1180px){.watch-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.dual-watch-layout{grid-template-columns:1fr}.pool-card-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (max-width:760px){.watch-heading{display:grid}.watch-summary{grid-template-columns:1fr}.inventory-toolbar{grid-template-columns:1fr 1fr}.inventory-toolbar label:first-child{grid-column:1/-1}.pool-card-grid{grid-template-columns:1fr}.selection-input-row{grid-template-columns:1fr}.selection-search{grid-template-columns:1fr}.search-button{width:100%}.pagination{align-items:flex-start;flex-direction:column}}
.manual-execution-status{display:grid;gap:9px;margin:12px 0;padding:12px;border:1px solid #bfdac8;border-radius:9px;color:#254d37;background:#f2f8f4}.manual-execution-status>header,.manual-execution-status>footer{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.manual-execution-status>header>div{display:grid;gap:2px}.manual-execution-status>header small,.manual-execution-status>footer small{color:#6d7c72;font-size:9px;overflow-wrap:anywhere}.manual-execution-status>header strong{color:#1c3e2c;font-size:12px}.manual-execution-status>header>span{padding:3px 7px;border-radius:999px;color:#fff;background:#2b7550;font-size:9px;font-weight:750;white-space:nowrap}.manual-execution-status>p{margin:0;color:#365945;font-size:11px;line-height:1.55;overflow-wrap:anywhere}.manual-execution-status dl{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px;margin:0}.manual-execution-status dl>div{display:grid;gap:2px;padding:7px 8px;border:1px solid rgba(52,105,75,.13);border-radius:6px;background:rgba(255,255,255,.72)}.manual-execution-status dt{color:#718078;font-size:8px}.manual-execution-status dd{margin:0;color:#254d37;font-size:11px;font-weight:750}.manual-execution-status>footer{align-items:center}.manual-execution-status>footer button{border:0;padding:3px 5px;color:#557264;background:transparent;font-size:9px}.manual-execution-status.is-pending,.manual-execution-status.is-retry{border-color:#dec986;background:#fff9e9}.manual-execution-status.is-pending>header>span,.manual-execution-status.is-retry>header>span{background:#8a7227}.manual-execution-status.is-failed,.manual-execution-status.is-cancelled{border-color:#e4b6af;background:#fff6f4}.manual-execution-status.is-failed>header>span,.manual-execution-status.is-cancelled>header>span{background:#a4493f}.batch-trades{display:grid;gap:5px;margin:0;padding:0;list-style:none}.batch-trades li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 8px;padding:6px 8px;border-radius:6px;background:rgba(255,255,255,.72);font-size:9px}.batch-trades li>span{overflow-wrap:anywhere}.batch-trades li>small{grid-column:1/-1;color:#8a4b43;overflow-wrap:anywhere}.batch-next,.batch-refresh-error{font-size:9px!important}.batch-refresh-error{color:#963f35!important}@media (max-width:760px){.manual-execution-status dl{grid-template-columns:repeat(2,minmax(0,1fr))}.manual-execution-status>header,.manual-execution-status>footer{align-items:flex-start;flex-direction:column}}
.selection-suggestions .catalog-load-more{display:block;color:#28714d;text-align:center;font-weight:750}.selection-suggestions .catalog-load-more:disabled{opacity:.55}
</style>
