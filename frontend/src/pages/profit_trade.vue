<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import ProfitTradeRoiWatch from "../components/ProfitTradeRoiWatch.vue";

type ProfitStep = {
  key: string;
  label: string;
  index: number;
};

type ProfitTrade = {
  id: number;
  tradeNo: string;
  marketHashName: string;
  name?: string | null;
  status: string;
  stepKey: string;
  stepIndex: number;
  progressPct: number;
  requiresManualAction: boolean;
  aAssetId?: string | null;
  aSteamId?: string | null;
  bAssetId?: string | null;
  steamListingId?: string | null;
  c5ProductId?: string | null;
  steamBuyPrice?: number | null;
  steamBalanceDiscount?: number | null;
  steamRealCost?: number | null;
  c5ListingPrice?: number | null;
  c5ExpectedNetPrice?: number | null;
  c5SoldNetPrice?: number | null;
  expectedProfit?: number | null;
  realizedProfit?: number | null;
  expectedRoiPct?: number | null;
  realizedRoiPct?: number | null;
  error?: string | null;
  note?: Record<string, unknown> | null;
  steamBoughtAt?: string | null;
  steamBoughtAtSource?: string | null;
  createdAt: string;
  updatedAt: string;
  completedAt?: string | null;
};

type ProfitDashboard = {
  generatedAt: string;
  config: {
    enabled: boolean;
    allowRealExecution: boolean;
    allowRepriceExecution: boolean;
    balanceDiscount?: number;
    balanceDiscountPct?: number;
    minRoiPct?: number;
    minItemValue?: number;
    maxBuyPerCycle: number;
    dailySteamBudget?: number;
    scanMaxItems: number;
    reservationSeconds: number;
    steamBuyPriceTolerancePct?: number;
    requireC5RecentSales?: boolean;
    requireC5MarketDepth?: boolean;
    liquidityMinRecentSales?: number;
    c5MinOnSaleCount?: number;
    c5MaxListingPremiumPct?: number;
    manualReviewRoiPct?: number;
    stickerSlabStatus?: "blocked" | "active";
    stickerStatus?: "blocked" | "active";
    protectedAssetIds: string[];
    protectedMarketHashNames: string[];
    protectedSteamIds: string[];
    aiAudit: {
      enabled: boolean;
      provider: string;
      model: string;
    };
  };
  steps: ProfitStep[];
  summary: {
    activeCount: number;
    completedCount: number;
    failedCount: number;
    reservedAssetCount: number;
    expectedProfit: number;
    realizedProfit: number;
    dailySteamSpent?: number;
    dailySteamRemaining?: number;
  };
  trades: ProfitTrade[];
  lastRun?: {
    generatedAt?: string;
    summary?: string;
    boughtCount?: number;
    listedCount?: number;
    settledCount?: number;
    skippedCount?: number;
    errorCount?: number;
    errors?: string[];
  } | null;
};

const fallbackDashboard: ProfitDashboard = {
  generatedAt: "",
  config: {
    enabled: false,
    allowRealExecution: false,
    allowRepriceExecution: false,
    balanceDiscount: undefined,
    balanceDiscountPct: undefined,
    minRoiPct: undefined,
    minItemValue: undefined,
    maxBuyPerCycle: 1,
    dailySteamBudget: 1000,
    scanMaxItems: 80,
    reservationSeconds: 60,
    steamBuyPriceTolerancePct: 1,
    requireC5RecentSales: true,
    requireC5MarketDepth: true,
    liquidityMinRecentSales: 3,
    c5MinOnSaleCount: 3,
    c5MaxListingPremiumPct: 3,
    manualReviewRoiPct: 20,
    stickerSlabStatus: "blocked",
    stickerStatus: "blocked",
    protectedAssetIds: [],
    protectedMarketHashNames: [],
    protectedSteamIds: [],
    aiAudit: {
      enabled: false,
      provider: "deepseek",
      model: "",
    },
  },
  steps: [
    { key: "discovered", label: "发现机会", index: 0 },
    { key: "audited", label: "审计通过", index: 1 },
    { key: "asset_locked", label: "锁定A", index: 2 },
    { key: "steam_bought", label: "买入B", index: 3 },
    { key: "c5_listed", label: "C5上架", index: 4 },
    { key: "c5_sold", label: "C5售出", index: 5 },
    { key: "settled", label: "收益结算", index: 6 },
  ],
  summary: {
    activeCount: 0,
    completedCount: 0,
    failedCount: 0,
    reservedAssetCount: 0,
    expectedProfit: 0,
    realizedProfit: 0,
    dailySteamSpent: 0,
    dailySteamRemaining: 1000,
  },
  trades: [],
  lastRun: null,
};

const dashboard = ref<ProfitDashboard>(fallbackDashboard);
const loading = ref(false);
const apiOnline = ref<boolean | null>(null);
const message = ref("");
const manualProtectedAssetId = ref("");
const manualProtectedMarketHashName = ref("");
const manualProtectedSteamId = ref("");
const dailySteamBudgetDraft = ref("1000");
const manualSettleInputs = ref<Record<number, string>>({});
const autoRunIntervalMs = 10 * 60 * 1000;
const autoRunStorageKey = "profitTrade.autoRun.v1";
const lastRunStorageKey = "profitTrade.lastRun.v1";
const autoRunTimer = ref<ReturnType<typeof setTimeout> | null>(null);
const autoRunTickTimer = ref<ReturnType<typeof setInterval> | null>(null);
const autoRunActive = ref(false);
const autoRunNextAt = ref<number | null>(null);
const autoRunInFlight = ref(false);
const lastRunAt = ref<number | null>(null);
const lastRunResult = ref("");
const nowMs = ref(Date.now());
const completedDateFrom = ref("");
const completedDateTo = ref("");
const completedPage = ref(1);
const completedPageSize = 10;

const sortedTrades = computed(() => [...dashboard.value.trades].sort((a, b) => b.id - a.id));
const activeTrades = computed(() => sortedTrades.value.filter((trade) => trade.status !== "completed"));
const completedTrades = computed(() => sortedTrades.value.filter((trade) => trade.status === "completed"));
const completedHasDateFilter = computed(() => completedDateFrom.value !== "" || completedDateTo.value !== "");
const completedFilteredTrades = computed(() => {
  const from = parseDateStart(completedDateFrom.value);
  const to = parseDateEnd(completedDateTo.value);
  const hasFilter = from !== null || to !== null;
  return completedTrades.value.filter((trade) => {
    const time = completedTradePurchaseTimeMs(trade);
    if (time === null) return !hasFilter;
    if (from !== null && time < from) return false;
    if (to !== null && time > to) return false;
    return true;
  });
});
const completedTotalProfit = computed(() => completedTrades.value.reduce(
  (total, trade) => total + (Number(trade.realizedProfit) || 0),
  0,
));
const completedFilteredProfit = computed(() => completedFilteredTrades.value.reduce(
  (total, trade) => total + (Number(trade.realizedProfit) || 0),
  0,
));
const completedTotalSteamBuy = computed(() => completedTrades.value.reduce(
  (total, trade) => total + (Number(trade.steamBuyPrice) || 0),
  0,
));
const completedFilteredSteamBuy = computed(() => completedFilteredTrades.value.reduce(
  (total, trade) => total + (Number(trade.steamBuyPrice) || 0),
  0,
));
const completedProfitSummaryLabel = computed(() => (
  completedHasDateFilter.value ? "当前筛选总收益" : "总收益"
));
const completedSteamBuySummaryLabel = computed(() => (
  completedHasDateFilter.value ? "筛选已结算 Steam买入" : "已结算 Steam买入总额"
));
const completedTotalPages = computed(() => Math.max(1, Math.ceil(completedFilteredTrades.value.length / completedPageSize)));
const completedCurrentPage = computed(() => Math.min(Math.max(1, completedPage.value), completedTotalPages.value));
const completedPagedTrades = computed(() => {
  const start = (completedCurrentPage.value - 1) * completedPageSize;
  return completedFilteredTrades.value.slice(start, start + completedPageSize);
});
const completedPageRangeLabel = computed(() => {
  const total = completedFilteredTrades.value.length;
  if (total === 0) return "0 / 0";
  const start = (completedCurrentPage.value - 1) * completedPageSize + 1;
  const end = Math.min(start + completedPageSize - 1, total);
  return `${start}-${end} / ${total}`;
});
const protectedAssetCount = computed(() => dashboard.value.config.protectedAssetIds?.length ?? 0);
const protectedNameCount = computed(() => dashboard.value.config.protectedMarketHashNames?.length ?? 0);
const protectedSteamCount = computed(() => dashboard.value.config.protectedSteamIds?.length ?? 0);
const protectedAssetPreview = computed(() => dashboard.value.config.protectedAssetIds ?? []);
const protectedNamePreview = computed(() => dashboard.value.config.protectedMarketHashNames ?? []);
const protectedSteamPreview = computed(() => dashboard.value.config.protectedSteamIds ?? []);
const autoRunEnabled = computed(() => autoRunActive.value);
const stickerSlabActive = computed(() => dashboard.value.config.stickerSlabStatus === "active");
const stickerActive = computed(() => dashboard.value.config.stickerStatus === "active");
const autoRunCountdown = computed(() => {
  if (!autoRunNextAt.value) return "";
  const remainingSeconds = Math.max(0, Math.ceil((autoRunNextAt.value - nowMs.value) / 1000));
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
});
const apiStatusLabel = computed(() => {
  if (apiOnline.value === true) return "后端 API 已启动";
  if (apiOnline.value === false) return "后端 API 未连接";
  return "后端 API 检查中";
});
const realExecutionLabel = computed(() => (
  dashboard.value.config.allowRealExecution ? "真实执行已开放" : "真实执行未开放"
));
const autoRunStatusLabel = computed(() => {
  if (autoRunInFlight.value) return "浏览器循环执行中";
  return autoRunActive.value ? "浏览器循环运行中" : "浏览器循环未运行";
});
const nextAutoRunLabel = computed(() => {
  if (autoRunInFlight.value) return "本轮执行中";
  if (!autoRunActive.value) return "未计划";
  if (!autoRunNextAt.value) return "等待安排";
  return `${formatDateTime(autoRunNextAt.value)}（${autoRunCountdown.value}）`;
});
const lastRunLabel = computed(() => {
  const backendLastRun = dashboard.value.lastRun;
  if (backendLastRun?.generatedAt && backendLastRun?.summary) {
    return `${formatDateTime(backendLastRun.generatedAt)}｜${backendLastRun.summary}`;
  }
  if (!lastRunAt.value || !lastRunResult.value) return "暂无";
  return `${formatDateTime(lastRunAt.value)}｜${lastRunResult.value}`;
});

function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `CNY ${Number(value).toFixed(2)}`;
}

function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Number(value).toFixed(2)}%`;
}

function formatDateTime(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function parseDateStart(value: string): number | null {
  if (!value) return null;
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime()) ? null : date.getTime();
}

function parseDateEnd(value: string): number | null {
  if (!value) return null;
  const date = new Date(`${value}T23:59:59.999`);
  return Number.isNaN(date.getTime()) ? null : date.getTime();
}

function completedTradePurchaseTimeMs(trade: ProfitTrade): number | null {
  const raw = trade.steamBoughtAt;
  if (!raw) return null;
  const time = new Date(raw).getTime();
  return Number.isNaN(time) ? null : time;
}

function resetCompletedPage(): void {
  completedPage.value = 1;
}

function setCompletedDatePreset(days: number | "all"): void {
  if (days === "all") {
    completedDateFrom.value = "";
    completedDateTo.value = "";
    resetCompletedPage();
    return;
  }
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - Math.max(0, days - 1));
  completedDateFrom.value = start.toISOString().slice(0, 10);
  completedDateTo.value = end.toISOString().slice(0, 10);
  resetCompletedPage();
}

function goCompletedPage(direction: -1 | 1): void {
  completedPage.value = Math.min(
    completedTotalPages.value,
    Math.max(1, completedCurrentPage.value + direction),
  );
}

function noteText(trade: ProfitTrade, key: string): string {
  const value = trade.note?.[key];
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function noteNumber(trade: ProfitTrade, key: string): number | null {
  const value = Number(trade.note?.[key]);
  return Number.isFinite(value) ? value : null;
}

function balanceDiscountPct(trade: ProfitTrade): number | null {
  if (trade.steamBalanceDiscount === null || trade.steamBalanceDiscount === undefined) return null;
  return Number(trade.steamBalanceDiscount) * 100;
}

function steamAccountLabel(trade: ProfitTrade): string {
  const name = noteText(trade, "steamAccountName");
  const id = noteText(trade, "steamAccountId");
  if (name === "-" && id === "-") return "未记录";
  if (name === "-") return id;
  if (id === "-") return name;
  return `${name} / ${id}`;
}

function duplicateBuyText(trade: ProfitTrade): string {
  const duplicate = trade.note?.extraDuplicateBuyDuringRepair;
  if (!duplicate || typeof duplicate !== "object") return "";
  const data = duplicate as Record<string, unknown>;
  const account = String(data.steamAccountName || "-");
  const assetId = String(data.assetId || "-");
  const price = Number(data.price);
  const priceText = Number.isFinite(price) ? ` / ${formatMoney(price)}` : "";
  return `${account} / ${assetId}${priceText}`;
}

function signedClass(value: number | null | undefined): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) return "neutral";
  return numeric > 0 ? "positive" : "negative";
}

function isStickerSlab(trade: ProfitTrade): boolean {
  const hash = trade.marketHashName.toLowerCase();
  const name = String(trade.name || "");
  return hash.startsWith("sticker slab |") || name.includes("印花板");
}

function canManualSettle(trade: ProfitTrade): boolean {
  return trade.status === "c5_listed" || (
    trade.status === "manual_required"
    && trade.stepIndex >= 4
    && Number(trade.steamBuyPrice) > 0
  );
}

function hasTrackedSteamBuyOrder(trade: ProfitTrade): boolean {
  const buyOrderId = String(trade.note?.steamBuyOrderId ?? "").trim();
  return buyOrderId !== "" || Boolean(trade.note?.steamBuyUnverifiedAt);
}

function dismissActionLabel(trade: ProfitTrade): string {
  return hasTrackedSteamBuyOrder(trade) ? "撤销求购并关闭" : "已知晓并隐藏";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    candidate: "候选",
    audited: "审计通过",
    locked: "已锁定A",
    buying: "买入B",
    steam_bought: "已买入B",
    listing_c5: "C5上架中",
    c5_listed: "C5已上架",
    selling_c5: "等待C5售出",
    completed: "已结算",
    failed: "失败",
    manual_required: "需人工处理",
    cancelled: "已取消",
  };
  return labels[status] ?? status;
}

function stepClass(step: ProfitStep, trade: ProfitTrade): string {
  if (trade.status === "failed" || trade.status === "manual_required") {
    return step.index <= trade.stepIndex ? "attention" : "pending";
  }
  if (step.index < trade.stepIndex) return "done";
  if (step.index === trade.stepIndex) return "current";
  return "pending";
}

async function fetchJson(url: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json() as { error?: string };
      detail = payload.error ? ` ${payload.error}` : "";
    } catch {
      detail = "";
    }
    throw new Error(`${response.status} ${response.statusText}${detail}`);
  }
  return response.json();
}

async function loadDashboard(): Promise<void> {
  loading.value = true;
  message.value = "";
  try {
    dashboard.value = (await fetchJson("/api/profit-trade/dashboard")) as ProfitDashboard;
    dailySteamBudgetDraft.value = String(dashboard.value.config.dailySteamBudget ?? 1000);
    apiOnline.value = true;
    window.dispatchEvent(new CustomEvent("profit-trade:dashboard-status", {
      detail: { allowRealExecution: dashboard.value.config.allowRealExecution },
    }));
  } catch {
    apiOnline.value = false;
    dashboard.value = fallbackDashboard;
    message.value = "API未连接：无法读取当前真实状态，页面不会使用静态运营数据替代。";
  } finally {
    loading.value = false;
  }
}

async function toggleEnabled(): Promise<void> {
  const nextEnabled = !dashboard.value.config.enabled;
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: nextEnabled }),
    });
    apiOnline.value = true;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `API未连接，无法${nextEnabled ? "开启" : "关闭"}后端开关`;
  }
}

async function toggleRealExecution(): Promise<void> {
  const nextAllowed = !dashboard.value.config.allowRealExecution;
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allowRealExecution: nextAllowed }),
    });
    apiOnline.value = true;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `真实执行开关失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function toggleRepriceExecution(): Promise<void> {
  const nextAllowed = !dashboard.value.config.allowRepriceExecution;
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allowRepriceExecution: nextAllowed }),
    });
    apiOnline.value = true;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `C5改价开关失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function setItemTypeStatus(
  key: "stickerSlabStatus" | "stickerStatus",
  status: "blocked" | "active",
): Promise<void> {
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [key]: status }),
    });
    apiOnline.value = true;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `品类状态更新失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function saveDailySteamBudget(): Promise<void> {
  const value = Number(dailySteamBudgetDraft.value);
  if (!Number.isFinite(value) || value < 0) {
    message.value = "每日余额上限必须是大于等于0的数字";
    return;
  }
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dailySteamBudget: value }),
    });
    apiOnline.value = true;
    message.value = `每日余额上限已保存：CNY ${value.toFixed(2)}`;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `每日余额上限保存失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function manualSettleTrade(trade: ProfitTrade): Promise<void> {
  const value = Number(manualSettleInputs.value[trade.id]);
  if (!Number.isFinite(value) || value <= 0) {
    message.value = "请输入有效的最终到手金额";
    return;
  }
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/manual-settle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tradeId: trade.id,
        soldNetPrice: value,
        source: "manual_other_platform",
      }),
    });
    apiOnline.value = true;
    delete manualSettleInputs.value[trade.id];
    message.value = `${trade.tradeNo} 已手动完结`;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = true;
    message.value = `${trade.tradeNo} 手动完结失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function dismissTrade(trade: ProfitTrade): Promise<void> {
  message.value = "";
  try {
    const result = (await fetchJson("/api/profit-trade/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tradeId: trade.id,
        reason: "user acknowledged and hid this trade",
      }),
    })) as { ok?: boolean; dismissed?: boolean; message?: string };
    apiOnline.value = true;
    message.value = result.dismissed === false
      ? `${trade.tradeNo} 未隐藏：${result.message ?? "Steam 求购已成交，流水已恢复"}`
      : `${trade.tradeNo} ${hasTrackedSteamBuyOrder(trade) ? "求购已确认撤销并关闭" : "已隐藏"}`;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = true;
    message.value = `${trade.tradeNo} 安全关闭失败：${error instanceof Error ? error.message : String(error)}`;
    await loadDashboard();
  }
}

async function toggleStickerSlabStatus(): Promise<void> {
  await setItemTypeStatus("stickerSlabStatus", stickerSlabActive.value ? "blocked" : "active");
}

async function toggleStickerStatus(): Promise<void> {
  await setItemTypeStatus("stickerStatus", stickerActive.value ? "blocked" : "active");
}

async function sendDailyReport(): Promise<void> {
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/serverchan/daily-report", { method: "POST" });
    apiOnline.value = true;
    message.value = "ServerChan日报已发送";
  } catch (error) {
    apiOnline.value = false;
    message.value = "API未连接或ServerChan未配置，日报未发送";
  }
}

async function scanOpportunities(): Promise<void> {
  message.value = "";
  loading.value = true;
  try {
    const payload = await fetchJson("/api/profit-trade/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record: true, lock: false, limit: 20, scanMaxItems: dashboard.value.config.scanMaxItems }),
    }) as { report?: { opportunityCount?: number; createdTradeIds?: number[] } };
    apiOnline.value = true;
    const created = payload.report?.createdTradeIds?.length ?? 0;
    const opportunities = payload.report?.opportunityCount ?? 0;
    message.value = `扫描完成：机会 ${opportunities} 个，写入候选 ${created} 笔`;
    await loadDashboard();
    window.dispatchEvent(new CustomEvent("profit-trade:refresh-observability"));
  } catch (error) {
    apiOnline.value = false;
    message.value = "API未连接或扫描失败";
  } finally {
    loading.value = false;
  }
}

async function runOnce(): Promise<void> {
  message.value = "";
  loading.value = true;
  try {
    const payload = await fetchJson("/api/profit-trade/run-once", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scanMaxItems: dashboard.value.config.scanMaxItems }),
    }) as {
      report?: {
        boughtTradeIds?: number[];
        listedTradeIds?: number[];
        settledTradeIds?: number[];
        skippedTradeIds?: number[];
        errors?: string[];
      };
    };
    apiOnline.value = true;
    const bought = payload.report?.boughtTradeIds?.length ?? 0;
    const listed = payload.report?.listedTradeIds?.length ?? 0;
    const settled = payload.report?.settledTradeIds?.length ?? 0;
    const skipped = payload.report?.skippedTradeIds?.length ?? 0;
    const errors = payload.report?.errors?.length ?? 0;
    const summary = `买入B ${bought} 笔，C5上架 ${listed} 笔，结算 ${settled} 笔，跳过 ${skipped} 笔，错误 ${errors} 个`;
    lastRunAt.value = Date.now();
    lastRunResult.value = summary;
    saveLastRunState();
    message.value = `执行完成：${summary}`;
    await loadDashboard();
    window.dispatchEvent(new CustomEvent("profit-trade:refresh-observability"));
  } catch (error) {
    apiOnline.value = false;
    const summary = `失败：${error instanceof Error ? error.message : String(error)}`;
    lastRunAt.value = Date.now();
    lastRunResult.value = summary;
    saveLastRunState();
    message.value = `执行一轮失败：${summary.replace(/^失败：/, "")}`;
  } finally {
    loading.value = false;
  }
}

async function refreshSales(): Promise<void> {
  message.value = "";
  loading.value = true;
  try {
    const payload = await fetchJson("/api/profit-trade/refresh-sales", {
      method: "POST",
    }) as { settledTradeIds?: number[]; skippedTradeIds?: number[]; errors?: string[] };
    apiOnline.value = true;
    const settled = payload.settledTradeIds?.length ?? 0;
    const skipped = payload.skippedTradeIds?.length ?? 0;
    const errors = payload.errors?.length ?? 0;
    message.value = `C5状态刷新完成：结算 ${settled} 笔，跳过 ${skipped} 笔，错误 ${errors} 个`;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `刷新C5状态失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    loading.value = false;
  }
}

async function lockTrade(trade: ProfitTrade): Promise<void> {
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/lock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tradeId: trade.id }),
    });
    apiOnline.value = true;
    message.value = `${trade.tradeNo} 已锁定A`;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `${trade.tradeNo} 锁定失败`;
  }
}

async function buyTrade(trade: ProfitTrade): Promise<void> {
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/buy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tradeId: trade.id }),
    });
    apiOnline.value = true;
    message.value = `${trade.tradeNo} 已买入B`;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = true;
    message.value = `${trade.tradeNo} 买入B失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function listC5Trade(trade: ProfitTrade): Promise<void> {
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/list-c5", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tradeId: trade.id }),
    });
    apiOnline.value = true;
    message.value = `${trade.tradeNo} 已上架C5`;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = true;
    message.value = `${trade.tradeNo} 上架C5失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function updateProtection(
  action: "add" | "remove",
  kind: "asset" | "marketHashName" | "steamId",
  value: string | null | undefined,
): Promise<void> {
  const normalizedValue = String(value || "").trim();
  if (!normalizedValue) {
    message.value = "没有可保护的值";
    return;
  }
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/protection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, kind, value: normalizedValue }),
    });
    apiOnline.value = true;
    const actionLabel = action === "add" ? "加入保护" : "移出保护";
    message.value = `${normalizedValue} 已${actionLabel}`;
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `保护名单更新失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function addManualProtectedAsset(): Promise<void> {
  await updateProtection("add", "asset", manualProtectedAssetId.value);
  manualProtectedAssetId.value = "";
}

async function addManualProtectedMarketHashName(): Promise<void> {
  await updateProtection("add", "marketHashName", manualProtectedMarketHashName.value);
  manualProtectedMarketHashName.value = "";
}

async function addManualProtectedSteamId(): Promise<void> {
  await updateProtection("add", "steamId", manualProtectedSteamId.value);
  manualProtectedSteamId.value = "";
}

function saveAutoRunState(): void {
  if (typeof window === "undefined") return;
  if (!autoRunActive.value || !autoRunNextAt.value) {
    window.localStorage.removeItem(autoRunStorageKey);
    window.dispatchEvent(new CustomEvent("profit-trade:runtime-state", { detail: { active: false } }));
    return;
  }
  window.localStorage.setItem(
    autoRunStorageKey,
    JSON.stringify({
      enabled: true,
      nextAt: autoRunNextAt.value,
    }),
  );
  window.dispatchEvent(new CustomEvent("profit-trade:runtime-state", { detail: { active: true } }));
}

function handleSharedConfigChange(): void {
  void loadDashboard();
}

function saveLastRunState(): void {
  if (typeof window === "undefined") return;
  if (!lastRunAt.value || !lastRunResult.value) {
    window.localStorage.removeItem(lastRunStorageKey);
    return;
  }
  window.localStorage.setItem(
    lastRunStorageKey,
    JSON.stringify({
      at: lastRunAt.value,
      result: lastRunResult.value,
    }),
  );
}

function clearAutoRunTimers(): void {
  if (autoRunTimer.value !== null) {
    clearTimeout(autoRunTimer.value);
    autoRunTimer.value = null;
  }
  if (autoRunTickTimer.value !== null) {
    clearInterval(autoRunTickTimer.value);
    autoRunTickTimer.value = null;
  }
}

function scheduleAutoRun(nextAt: number): void {
  if (autoRunTimer.value !== null) {
    clearTimeout(autoRunTimer.value);
    autoRunTimer.value = null;
  }
  autoRunActive.value = true;
  autoRunNextAt.value = nextAt;
  nowMs.value = Date.now();
  saveAutoRunState();
  autoRunTimer.value = setTimeout(() => {
    autoRunTimer.value = null;
    void runScheduledOnce();
  }, Math.max(0, nextAt - Date.now()));
  if (autoRunTickTimer.value === null) {
    autoRunTickTimer.value = setInterval(() => {
      nowMs.value = Date.now();
    }, 1000);
  }
}

async function runScheduledOnce(): Promise<void> {
  if (autoRunInFlight.value) return;
  autoRunInFlight.value = true;
  try {
    await runOnce();
  } finally {
    autoRunInFlight.value = false;
    if (autoRunActive.value) {
      scheduleAutoRun(Date.now() + autoRunIntervalMs);
    }
  }
}

async function startAutoRun(): Promise<void> {
  if (autoRunActive.value) return;
  autoRunActive.value = true;
  autoRunNextAt.value = null;
  nowMs.value = Date.now();
  message.value = "循环执行已开启：正在立刻执行第一轮";
  window.dispatchEvent(new CustomEvent("profit-trade:runtime-state", { detail: { active: true } }));
  await runScheduledOnce();
}

function stopAutoRun(): void {
  clearAutoRunTimers();
  autoRunActive.value = false;
  autoRunNextAt.value = null;
  autoRunInFlight.value = false;
  nowMs.value = Date.now();
  saveAutoRunState();
  message.value = "循环执行已停止";
}

function toggleAutoRun(): void {
  if (autoRunEnabled.value) {
    stopAutoRun();
    return;
  }
  void startAutoRun();
}

function restoreAutoRun(): void {
  if (typeof window === "undefined") return;
  const raw = window.localStorage.getItem(autoRunStorageKey);
  if (!raw) return;
  try {
    const stored = JSON.parse(raw) as { enabled?: boolean; nextAt?: number };
    if (!stored.enabled) return;
    const nextAt = Number(stored.nextAt);
    const restoredNextAt = Number.isFinite(nextAt) && nextAt > 0
      ? Math.max(nextAt, Date.now())
      : Date.now() + autoRunIntervalMs;
    scheduleAutoRun(restoredNextAt);
    message.value = "循环执行已恢复：页面刷新后继续计时";
  } catch {
    window.localStorage.removeItem(autoRunStorageKey);
  }
}

function restoreLastRun(): void {
  if (typeof window === "undefined") return;
  const raw = window.localStorage.getItem(lastRunStorageKey);
  if (!raw) return;
  try {
    const stored = JSON.parse(raw) as { at?: number; result?: string };
    const at = Number(stored.at);
    if (Number.isFinite(at) && stored.result) {
      lastRunAt.value = at;
      lastRunResult.value = String(stored.result);
    }
  } catch {
    window.localStorage.removeItem(lastRunStorageKey);
  }
}

onMounted(() => {
  restoreLastRun();
  void loadDashboard();
  restoreAutoRun();
  window.addEventListener("profit-trade:config-changed", handleSharedConfigChange);
});

onUnmounted(() => {
  clearAutoRunTimers();
  window.removeEventListener("profit-trade:config-changed", handleSharedConfigChange);
});
</script>

<template>
  <main class="page profit-trade-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">搬砖做T</p>
        <h1>Profit Trade</h1>
      </div>
      <div class="toolbar">
        <span class="api-pill" :class="{ online: apiOnline === true, offline: apiOnline === false }">
          {{ apiOnline === true ? "API已连接" : apiOnline === false ? "API未连接" : "检查中" }}
        </span>
        <button class="secondary-button" type="button" @click="loadDashboard">
          刷新
        </button>
        <button class="secondary-button" type="button" @click="scanOpportunities">
          扫描机会
        </button>
        <button class="secondary-button" type="button" @click="runOnce">
          执行一轮
        </button>
        <button
          class="secondary-button"
          :class="{ active: autoRunEnabled }"
          type="button"
          @click="toggleAutoRun"
        >
          {{ autoRunEnabled ? `停止循环 ${autoRunCountdown}` : "循环执行10分钟" }}
        </button>
        <button class="secondary-button" type="button" @click="refreshSales">
          刷新C5状态
        </button>
        <button class="secondary-button" type="button" @click="sendDailyReport">
          发送日报
        </button>
        <button class="primary-button" type="button" @click="toggleEnabled">
          {{ dashboard.config.enabled ? "关闭执行器" : "开启执行器" }}
        </button>
        <button
          class="secondary-button"
          :class="{ active: dashboard.config.allowRepriceExecution }"
          type="button"
          @click="toggleRepriceExecution"
        >
          {{ dashboard.config.allowRepriceExecution ? "禁止仅C5改价" : "仅允许C5改价" }}
        </button>
        <button class="secondary-button danger-button" type="button" @click="toggleRealExecution">
          {{ dashboard.config.allowRealExecution ? "禁止真实执行" : "允许真实执行" }}
        </button>
      </div>
    </header>

    <section class="profit-summary-grid">
      <article class="profit-metric">
        <span>开关</span>
        <strong>{{ dashboard.config.enabled ? "已开启" : "已关闭" }}</strong>
      </article>
      <article class="profit-metric">
        <span>进行中</span>
        <strong>{{ dashboard.summary.activeCount }}</strong>
      </article>
      <article class="profit-metric" :class="{ danger: dashboard.summary.failedCount > 0 }">
        <span>需处理</span>
        <strong>{{ dashboard.summary.failedCount }}</strong>
      </article>
      <article class="profit-metric">
        <span>全部已结算收益</span>
        <strong>{{ formatMoney(dashboard.summary.realizedProfit) }}</strong>
      </article>
      <article class="profit-metric">
        <span>进行中预计</span>
        <strong>{{ formatMoney(dashboard.summary.expectedProfit) }}</strong>
      </article>
      <article class="profit-metric">
        <span>阈值</span>
        <strong>{{ formatPct(dashboard.config.minRoiPct) }} / {{ formatMoney(dashboard.config.minItemValue) }}</strong>
      </article>
      <article class="profit-metric">
        <span>今日余额</span>
        <strong>{{ formatMoney(dashboard.summary.dailySteamSpent) }} / {{ formatMoney(dashboard.summary.dailySteamRemaining) }}</strong>
      </article>
    </section>

    <section class="execution-status-grid" aria-label="执行状态">
      <article class="execution-status-item" :class="{ online: apiOnline === true, offline: apiOnline === false }">
        <span>后端 API</span>
        <strong>{{ apiStatusLabel }}</strong>
      </article>
      <article class="execution-status-item" :class="{ online: dashboard.config.allowRealExecution, offline: !dashboard.config.allowRealExecution }">
        <span>真实执行</span>
        <strong>{{ realExecutionLabel }}</strong>
      </article>
      <article class="execution-status-item" :class="{ online: autoRunEnabled, running: autoRunInFlight }">
        <span>浏览器循环</span>
        <strong>{{ autoRunStatusLabel }}</strong>
      </article>
      <article class="execution-status-item">
        <span>下一次执行</span>
        <strong>{{ nextAutoRunLabel }}</strong>
      </article>
      <article class="execution-status-item wide">
        <span>上一次 run-once</span>
        <strong>{{ lastRunLabel }}</strong>
      </article>
    </section>

    <p v-if="message" class="inline-status">{{ message }}</p>

    <ProfitTradeRoiWatch />

    <section class="profit-layout">
      <article class="panel profit-settings">
        <div class="panel-title-row">
          <h2>执行参数</h2>
          <span class="soft-label">{{ dashboard.generatedAt || "-" }}</span>
        </div>
        <dl class="settings-list">
          <div>
            <dt>真实执行</dt>
            <dd>{{ dashboard.config.allowRealExecution ? "允许" : "禁止" }}</dd>
          </div>
          <div>
            <dt>单轮买入</dt>
            <dd>{{ dashboard.config.maxBuyPerCycle }}</dd>
          </div>
          <div>
            <dt>做T余额折扣</dt>
            <dd>{{ formatPct(dashboard.config.balanceDiscountPct) }}</dd>
          </div>
          <div>
            <dt>每日余额</dt>
            <dd>{{ formatMoney(dashboard.config.dailySteamBudget) }}</dd>
          </div>
          <div>
            <dt>扫描范围</dt>
            <dd>全部符合前置条件的品类</dd>
          </div>
          <div>
            <dt>A锁定</dt>
            <dd>{{ dashboard.config.reservationSeconds }}秒</dd>
          </div>
          <div>
            <dt>C5在售风控</dt>
            <dd>{{ dashboard.config.requireC5MarketDepth ? "必须通过" : "关闭" }}</dd>
          </div>
          <div>
            <dt>在售量下限</dt>
            <dd>{{ dashboard.config.c5MinOnSaleCount ?? 3 }}</dd>
          </div>
          <div>
            <dt>最近成交风控</dt>
            <dd>{{ dashboard.config.requireC5RecentSales ? "必须通过" : "暂不启用" }}</dd>
          </div>
          <div>
            <dt>异常ROI</dt>
            <dd>{{ formatPct(dashboard.config.manualReviewRoiPct ?? 20) }}</dd>
          </div>
          <div>
            <dt>挂价偏离</dt>
            <dd>{{ formatPct(dashboard.config.c5MaxListingPremiumPct ?? 3) }}</dd>
          </div>
          <div>
            <dt>保护资产</dt>
            <dd>{{ protectedAssetCount }}</dd>
          </div>
          <div>
            <dt>保护品类</dt>
            <dd>{{ protectedNameCount }}</dd>
          </div>
          <div>
            <dt>保护账号</dt>
            <dd>{{ protectedSteamCount }}</dd>
          </div>
          <div>
            <dt>AI审计</dt>
            <dd>{{ dashboard.config.aiAudit.enabled ? dashboard.config.aiAudit.provider : "未启用" }}</dd>
          </div>
        </dl>
        <div class="formula-panel">
          <strong>做T收益公式</strong>
          <span>真实成本 = Steam买入价 × 做T余额折扣（当前 {{ formatPct(dashboard.config.balanceDiscountPct) }}）</span>
          <span>预计ROI = C5预计到手 ÷ Steam买入价 - 做T余额折扣；结算ROI = C5实际到手 ÷ Steam买入价 - 该笔流水余额折扣</span>
          <small>最低通过ROI：{{ formatPct(dashboard.config.minRoiPct) }}。已结算流水按单笔记录的余额折扣计算，不会被当前配置改写。</small>
        </div>
        <div class="type-status-panel">
          <button
            class="status-toggle"
            :class="{ active: stickerSlabActive }"
            type="button"
            @click="toggleStickerSlabStatus"
          >
            <span>印花板状态</span>
            <strong>{{ stickerSlabActive ? "参与扫描" : "已屏蔽" }}</strong>
          </button>
          <button
            class="status-toggle"
            :class="{ active: stickerActive }"
            type="button"
            @click="toggleStickerStatus"
          >
            <span>印花状态</span>
            <strong>{{ stickerActive ? "参与扫描" : "已屏蔽" }}</strong>
          </button>
        </div>
        <form class="budget-form" @submit.prevent="saveDailySteamBudget">
          <label for="daily-steam-budget">每日最多使用Steam余额</label>
          <div class="protection-input-row">
            <input
              id="daily-steam-budget"
              v-model.trim="dailySteamBudgetDraft"
              type="number"
              min="0"
              step="1"
              autocomplete="off"
            >
            <button class="mini-action protect-action" type="submit">保存</button>
          </div>
        </form>
        <div class="protection-panel">
          <h3>保护名单</h3>
          <form class="protection-form" @submit.prevent="addManualProtectedAsset">
            <label for="protected-asset-id">固定保护资产</label>
            <div class="protection-input-row">
              <input
                id="protected-asset-id"
                v-model.trim="manualProtectedAssetId"
                type="text"
                autocomplete="off"
                placeholder="assetId"
              >
              <button class="mini-action protect-action" type="submit">加入</button>
            </div>
          </form>
          <form class="protection-form" @submit.prevent="addManualProtectedMarketHashName">
            <label for="protected-market-hash-name">固定保护品类</label>
            <div class="protection-input-row">
              <input
                id="protected-market-hash-name"
                v-model.trim="manualProtectedMarketHashName"
                type="text"
                autocomplete="off"
                placeholder="market_hash_name"
              >
              <button class="mini-action protect-action" type="submit">加入</button>
            </div>
          </form>
          <form class="protection-form" @submit.prevent="addManualProtectedSteamId">
            <label for="protected-steam-id">固定保护账号</label>
            <div class="protection-input-row">
              <input
                id="protected-steam-id"
                v-model.trim="manualProtectedSteamId"
                type="text"
                autocomplete="off"
                placeholder="SteamId64"
              >
              <button class="mini-action protect-action" type="submit">加入</button>
            </div>
          </form>
          <div class="protection-group">
            <span>资产 assetId</span>
            <button
              v-for="assetId in protectedAssetPreview"
              :key="assetId"
              class="protection-chip"
              type="button"
              @click="updateProtection('remove', 'asset', assetId)"
            >
              {{ assetId }}
            </button>
            <small v-if="protectedAssetCount === 0">暂无</small>
          </div>
          <div class="protection-group">
            <span>整类饰品</span>
            <button
              v-for="marketHashName in protectedNamePreview"
              :key="marketHashName"
              class="protection-chip"
              type="button"
              @click="updateProtection('remove', 'marketHashName', marketHashName)"
            >
              {{ marketHashName }}
            </button>
            <small v-if="protectedNameCount === 0">暂无</small>
          </div>
          <div class="protection-group">
            <span>Steam账号</span>
            <button
              v-for="steamId in protectedSteamPreview"
              :key="steamId"
              class="protection-chip"
              type="button"
              @click="updateProtection('remove', 'steamId', steamId)"
            >
              {{ steamId }}
            </button>
            <small v-if="protectedSteamCount === 0">暂无</small>
          </div>
          <small class="protection-hint">点击名单里的标签可以移出保护。</small>
        </div>
      </article>

      <section class="trade-stack">
        <article v-if="loading" class="panel empty-state">加载中...</article>
        <article v-else-if="activeTrades.length === 0" class="panel empty-state">
          <strong>暂无进行中做T流水</strong>
          <span>候选、买入、上架和人工处理中的流水会显示在这里。</span>
          <small v-if="dashboard.config.allowRealExecution">真实执行当前已允许，点“执行一轮”可能触发真实买入或上架。</small>
          <small v-else>真实执行当前禁止，可以先安全扫描和查看候选。</small>
        </article>

        <template v-else>
          <article
            v-for="trade in activeTrades"
            :key="trade.id"
            class="profit-trade-card"
            :class="{ attention: trade.requiresManualAction }"
          >
            <div class="trade-head">
              <div>
                <p class="trade-no">{{ trade.tradeNo }}</p>
                <h2>{{ trade.name || trade.marketHashName }}</h2>
                <p v-if="trade.name && trade.name !== trade.marketHashName" class="trade-hash">
                  {{ trade.marketHashName }}
                </p>
              </div>
            <div class="trade-badges">
              <span class="status-badge" :class="{ attention: trade.requiresManualAction }">
                {{ statusLabel(trade.status) }}
              </span>
              <span class="roi-badge">
                ROI {{ formatPct(trade.realizedRoiPct ?? trade.expectedRoiPct) }}
              </span>
              <span class="roi-basis-badge">
                ROI基准 {{ formatPct(balanceDiscountPct(trade)) }}
              </span>
              <span v-if="isStickerSlab(trade)" class="type-badge warning">
                印花板
              </span>
              <span v-if="noteText(trade, 'liquidityStatus') !== '-'" class="type-badge">
                {{ noteText(trade, "liquidityStatus") }}
              </span>
              <button
                v-if="trade.requiresManualAction"
                class="mini-action dismiss-action"
                type="button"
                @click="dismissTrade(trade)"
              >
                {{ dismissActionLabel(trade) }}
              </button>
              <button
                v-if="trade.aAssetId"
                class="mini-action protect-action"
                type="button"
                @click="updateProtection('add', 'asset', trade.aAssetId)"
              >
                保护此资产
              </button>
              <button
                class="mini-action protect-action"
                type="button"
                @click="updateProtection('add', 'marketHashName', trade.marketHashName)"
              >
                保护此品类
              </button>
              <button
                v-if="trade.status === 'candidate' || trade.status === 'audited'"
                class="mini-action"
                type="button"
                @click="lockTrade(trade)"
              >
                锁定A
              </button>
              <button
                v-if="trade.status === 'locked'"
                class="mini-action"
                type="button"
                @click="buyTrade(trade)"
              >
                买入B
              </button>
              <button
                v-if="trade.status === 'steam_bought'"
                class="mini-action"
                type="button"
                @click="listC5Trade(trade)"
              >
                上架C5
              </button>
              <div v-if="canManualSettle(trade)" class="manual-settle-row">
                <input
                  v-model.trim="manualSettleInputs[trade.id]"
                  type="number"
                  min="0"
                  step="0.01"
                  placeholder="最终到手"
                >
                <button
                  class="mini-action"
                  type="button"
                  @click="manualSettleTrade(trade)"
                >
                  手动完结
                </button>
              </div>
            </div>
          </div>

            <div class="progress-track" aria-hidden="true">
              <span :style="{ width: `${trade.progressPct}%` }"></span>
            </div>
            <ol class="step-row">
              <li
                v-for="step in dashboard.steps"
                :key="step.key"
                :class="stepClass(step, trade)"
              >
                <span>{{ step.index + 1 }}</span>
                <em>{{ step.label }}</em>
              </li>
            </ol>

            <div class="trade-detail-grid">
              <dl>
                <dt>A 旧底仓</dt>
                <dd>{{ trade.aAssetId || "-" }}</dd>
                <dt>A Steam账号</dt>
                <dd>{{ trade.aSteamId || "-" }}</dd>
                <dt>C5单号</dt>
                <dd>{{ trade.c5ProductId || "-" }}</dd>
              </dl>
              <dl>
                <dt>B Steam买入</dt>
                <dd>{{ formatMoney(trade.steamBuyPrice) }}</dd>
                <dt>Steam余额账号</dt>
                <dd>{{ steamAccountLabel(trade) }}</dd>
                <dt>买前余额</dt>
                <dd>{{ formatMoney(noteNumber(trade, "walletBalanceBefore")) }}</dd>
                <dt>B 新资产</dt>
                <dd>{{ trade.bAssetId || "-" }}</dd>
                <dt>Steam listing</dt>
                <dd>{{ trade.steamListingId || "-" }}</dd>
              </dl>
              <dl>
                <dt>真实成本</dt>
                <dd>{{ formatMoney(trade.steamRealCost) }}</dd>
                <dt>ROI基准折扣</dt>
                <dd>{{ formatPct(balanceDiscountPct(trade)) }}</dd>
                <dt>C5预计到手</dt>
                <dd>{{ formatMoney(trade.c5ExpectedNetPrice) }}</dd>
                <dt>C5实际到手</dt>
                <dd>{{ formatMoney(trade.c5SoldNetPrice) }}</dd>
                <dt>C5统计价</dt>
                <dd>{{ formatMoney(noteNumber(trade, "c5CurrentSellPrice")) }}</dd>
              </dl>
              <dl>
                <dt>预计收益</dt>
                <dd>{{ formatMoney(trade.expectedProfit) }}</dd>
                <dt>实际收益</dt>
                <dd>{{ formatMoney(trade.realizedProfit) }}</dd>
                <dt>C5在售量</dt>
                <dd>{{ noteText(trade, "c5OnSaleCount") }}</dd>
                <dt>风控状态</dt>
                <dd>{{ noteText(trade, "liquidityStatus") }}</dd>
                <dt>更新时间</dt>
                <dd>{{ trade.updatedAt }}</dd>
              </dl>
            </div>

            <p v-if="noteText(trade, 'manualReviewReason') !== '-'" class="trade-warning">
              {{ noteText(trade, "manualReviewReason") }}
            </p>

            <p v-if="trade.error" class="trade-error">
              {{ trade.error }}
            </p>
          </article>
        </template>

        <section class="completed-zone">
          <div class="panel-title-row">
            <h2>已完结收益（按 Steam 购买时间）</h2>
            <span class="soft-label">
              {{ completedFilteredTrades.length }} / {{ completedTrades.length }} 笔
            </span>
          </div>
          <p class="completed-filter-note">
            日期按买入 B 的 Steam 购买时间筛选；这里只统计已经完成收益结算的做T流水，不代表该时间段全部 Steam 钱包购买记录。
          </p>
          <div class="completed-toolbar">
            <label>
              <span>开始日期</span>
              <input
                v-model="completedDateFrom"
                type="date"
                @change="resetCompletedPage"
              >
            </label>
            <label>
              <span>结束日期</span>
              <input
                v-model="completedDateTo"
                type="date"
                @change="resetCompletedPage"
              >
            </label>
            <div class="completed-filter-actions">
              <button class="mini-action" type="button" @click="setCompletedDatePreset(1)">
                今天
              </button>
              <button class="mini-action" type="button" @click="setCompletedDatePreset(7)">
                近7天
              </button>
              <button class="mini-action" type="button" @click="setCompletedDatePreset('all')">
                全部
              </button>
            </div>
            <dl class="completed-profit-summary">
              <div>
                <dt>{{ completedProfitSummaryLabel }}</dt>
                <dd :class="signedClass(completedFilteredProfit)">{{ formatMoney(completedFilteredProfit) }}</dd>
                <small v-if="completedHasDateFilter">
                  全部累计：{{ formatMoney(completedTotalProfit) }}
                </small>
              </div>
              <div>
                <dt>{{ completedSteamBuySummaryLabel }}</dt>
                <dd>{{ formatMoney(completedFilteredSteamBuy) }}</dd>
                <small v-if="completedHasDateFilter">
                  全部累计：{{ formatMoney(completedTotalSteamBuy) }}
                </small>
              </div>
            </dl>
            <div class="completed-pagination">
              <button
                class="mini-action"
                type="button"
                :disabled="completedCurrentPage <= 1"
                @click="goCompletedPage(-1)"
              >
                上一页
              </button>
              <span>{{ completedPageRangeLabel }}｜第 {{ completedCurrentPage }} / {{ completedTotalPages }} 页</span>
              <button
                class="mini-action"
                type="button"
                :disabled="completedCurrentPage >= completedTotalPages"
                @click="goCompletedPage(1)"
              >
                下一页
              </button>
            </div>
          </div>
          <article v-if="completedTrades.length === 0" class="panel empty-state">
            <strong>暂无完结流水</strong>
            <span>手动完结或 C5 售出结算后，会在这里单独归档。</span>
          </article>
          <article v-else-if="completedFilteredTrades.length === 0" class="panel empty-state">
            <strong>当前时间范围没有完结收益</strong>
            <span>调整开始日期或结束日期后再查看。</span>
          </article>
          <article
            v-for="trade in completedPagedTrades"
            v-else
            :key="`completed-${trade.id}`"
            class="completed-trade-row"
          >
            <div class="completed-main">
              <strong>{{ trade.name || trade.marketHashName }}</strong>
              <span>{{ trade.tradeNo }}</span>
              <span>Steam余额账号：{{ steamAccountLabel(trade) }}</span>
              <span>Steam购买时间：{{ formatDateTime(trade.steamBoughtAt) }}</span>
              <span>结算时间：{{ formatDateTime(trade.completedAt || trade.updatedAt) }}</span>
              <span>买前余额：{{ formatMoney(noteNumber(trade, "walletBalanceBefore")) }}</span>
              <span>B assetId：{{ trade.bAssetId || "-" }}</span>
              <span v-if="duplicateBuyText(trade)">
                重复买入：{{ duplicateBuyText(trade) }}
              </span>
            </div>
            <dl class="completed-metrics">
              <dt>Steam买入</dt>
              <dd>{{ formatMoney(trade.steamBuyPrice) }}</dd>
              <dt>真实成本</dt>
              <dd>{{ formatMoney(trade.steamRealCost) }}</dd>
              <dt>C5售出所得</dt>
              <dd>{{ formatMoney(trade.c5SoldNetPrice) }}</dd>
              <dt>ROI基准折扣</dt>
              <dd>{{ formatPct(balanceDiscountPct(trade)) }}</dd>
              <dt>利润率</dt>
              <dd :class="signedClass(trade.realizedRoiPct)">{{ formatPct(trade.realizedRoiPct) }}</dd>
              <dt>利润</dt>
              <dd :class="signedClass(trade.realizedProfit)">{{ formatMoney(trade.realizedProfit) }}</dd>
            </dl>
          </article>
        </section>
      </section>
    </section>
  </main>
</template>

<style scoped>
.profit-trade-page {
  max-width: 1280px;
  align-content: start;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
  align-items: center;
}

.api-pill,
.status-badge,
.roi-badge,
.roi-basis-badge,
.type-badge {
  display: inline-flex;
  min-height: 30px;
  align-items: center;
  border: 1px solid #c9d5df;
  border-radius: 999px;
  padding: 4px 10px;
  color: #526071;
  background: #f8fafc;
  font-size: 13px;
  font-style: normal;
  white-space: nowrap;
}

.api-pill.online {
  border-color: #91c7ad;
  color: #1f684e;
  background: #f1fbf6;
}

.api-pill.offline,
.status-badge.attention {
  border-color: #e0a39a;
  color: #8a3a31;
  background: #fff6f4;
}

.type-badge.warning {
  border-color: #d9c891;
  color: #765b0d;
  background: #fff9e7;
}

.secondary-button.active {
  border-color: #2f6b52;
  color: #1f684e;
  background: #f1fbf6;
}

.profit-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
}

.profit-metric {
  min-height: 92px;
  border: 1px solid #dce2e8;
  border-radius: 8px;
  padding: 12px;
  background: #ffffff;
  display: grid;
  align-content: space-between;
  gap: 8px;
}

.profit-metric span {
  color: #687487;
  font-size: 13px;
}

.profit-metric strong {
  color: #172033;
  font-size: 18px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}

.profit-metric.danger {
  border-color: #e0a39a;
  background: #fff6f4;
}

.execution-status-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(150px, 1fr));
  gap: 10px;
}

.execution-status-item {
  min-height: 74px;
  border: 1px solid #dce2e8;
  border-radius: 8px;
  padding: 10px 12px;
  background: #ffffff;
  display: grid;
  align-content: space-between;
  gap: 8px;
}

.execution-status-item span {
  color: #687487;
  font-size: 12px;
}

.execution-status-item strong {
  color: #172033;
  font-size: 15px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.execution-status-item.online {
  border-color: #91c7ad;
  background: #f1fbf6;
}

.execution-status-item.offline {
  border-color: #e0a39a;
  background: #fff6f4;
}

.execution-status-item.running {
  border-color: #8fb7d8;
  background: #f2f8fd;
}

.execution-status-item.wide {
  grid-column: span 2;
}

.inline-status {
  margin: 0;
  border: 1px solid #d9c891;
  border-radius: 8px;
  padding: 10px 12px;
  color: #6d5717;
  background: #fff9e7;
}

.trade-warning {
  margin: 0;
  border: 1px solid #d9c891;
  border-radius: 8px;
  padding: 9px 10px;
  color: #765b0d;
  background: #fff9e7;
}

.profit-layout {
  display: grid;
  grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}

.settings-list {
  margin: 0;
  display: grid;
  gap: 10px;
}

.settings-list div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid #edf1f5;
  padding-bottom: 8px;
}

.settings-list dt,
.settings-list dd {
  margin: 0;
}

.settings-list dt {
  color: #687487;
}

.settings-list dd {
  color: #172033;
  font-weight: 700;
  text-align: right;
}

.formula-panel {
  margin-top: 12px;
  border: 1px solid #d7e0e7;
  border-radius: 8px;
  padding: 10px;
  background: #fbfcfd;
  display: grid;
  gap: 5px;
}

.formula-panel strong {
  color: #172033;
  font-size: 14px;
}

.formula-panel span {
  color: #526071;
  font-size: 12px;
  line-height: 1.45;
}

.formula-panel small {
  color: #8a3a31;
  font-size: 12px;
  line-height: 1.45;
}

.type-status-panel {
  margin-top: 14px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.status-toggle {
  min-height: 58px;
  border: 1px solid #d6dde6;
  border-radius: 8px;
  padding: 8px 10px;
  background: #f8fafc;
  color: #526071;
  text-align: left;
  cursor: pointer;
  display: grid;
  gap: 4px;
}

.status-toggle span {
  font-size: 12px;
}

.status-toggle strong {
  color: #8a3a31;
  font-size: 15px;
}

.status-toggle.active {
  border-color: #91c7ad;
  background: #f1fbf6;
  color: #1f684e;
}

.status-toggle.active strong {
  color: #1f684e;
}

.budget-form {
  margin-top: 12px;
  display: grid;
  gap: 6px;
}

.budget-form label {
  color: #687487;
  font-size: 12px;
}

.protection-panel {
  margin-top: 14px;
  border-top: 1px solid #edf1f5;
  padding-top: 12px;
  display: grid;
  gap: 10px;
}

.protection-panel h3 {
  margin: 0;
  color: #172033;
  font-size: 15px;
}

.protection-form {
  display: grid;
  gap: 6px;
}

.protection-form label {
  color: #687487;
  font-size: 12px;
}

.protection-input-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px;
}

.protection-input-row input {
  min-width: 0;
  min-height: 32px;
  border: 1px solid #c9d5df;
  border-radius: 8px;
  padding: 5px 8px;
  color: #172033;
  background: #ffffff;
  font-size: 13px;
}

.protection-input-row input:focus {
  border-color: #2f6b52;
  outline: none;
  box-shadow: 0 0 0 2px rgba(47, 107, 82, 0.12);
}

.protection-group {
  max-height: 180px;
  overflow-y: auto;
  overscroll-behavior: contain;
  border: 1px solid #edf1f5;
  border-radius: 8px;
  padding: 8px;
  display: grid;
  gap: 6px;
}

.protection-group > span {
  color: #687487;
  font-size: 12px;
}

.protection-group small,
.protection-hint {
  color: #8a96a5;
  font-size: 12px;
}

.protection-chip {
  min-width: 0;
  border: 1px solid #c9d5df;
  border-radius: 8px;
  padding: 6px 8px;
  color: #172033;
  background: #f8fafc;
  text-align: left;
  overflow-wrap: anywhere;
}

.trade-stack {
  display: grid;
  gap: 12px;
}

.empty-state {
  min-height: 160px;
  display: grid;
  place-items: center;
  color: #687487;
}

.profit-trade-card {
  border: 1px solid #dce2e8;
  border-radius: 8px;
  padding: 14px;
  background: #ffffff;
  display: grid;
  gap: 12px;
}

.profit-trade-card.attention {
  border-color: #e0a39a;
}

.trade-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: start;
}

.trade-no {
  margin: 0 0 4px;
  color: #687487;
  font-size: 13px;
}

.trade-head h2 {
  margin: 0;
  color: #172033;
  font-size: 18px;
  line-height: 1.25;
}

.trade-hash {
  margin: 4px 0 0;
  color: #687487;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.trade-badges {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}

.manual-settle-row {
  display: inline-grid;
  grid-template-columns: 94px auto;
  gap: 6px;
}

.manual-settle-row input {
  width: 94px;
  min-height: 30px;
  border: 1px solid #c9d5df;
  border-radius: 999px;
  padding: 4px 10px;
  color: #172033;
  background: #ffffff;
  font-size: 13px;
}

.roi-badge {
  border-color: #99c8b4;
  color: #1f684e;
  background: #f1fbf6;
}

.roi-basis-badge {
  border-color: #c4ccd6;
  color: #526071;
  background: #f8fafc;
}

.mini-action {
  min-height: 30px;
  border: 1px solid #b9c4ce;
  border-radius: 999px;
  padding: 4px 10px;
  color: #1c2b3a;
  background: #ffffff;
  font-size: 13px;
}

.protect-action {
  border-color: #d7bf7a;
  color: #6d5717;
  background: #fff9e7;
}

.dismiss-action {
  border-color: #c7d0da;
  color: #435366;
  background: #f4f7fa;
}

.danger-button {
  border-color: #d69b92;
  color: #8a3a31;
  background: #fff6f4;
}

.progress-track {
  position: relative;
  height: 10px;
  overflow: hidden;
  border-radius: 999px;
  background: #e9eef3;
}

.progress-track span {
  position: absolute;
  inset: 0 auto 0 0;
  border-radius: inherit;
  background: #2f6b52;
}

.step-row {
  display: grid;
  grid-template-columns: repeat(7, minmax(70px, 1fr));
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.step-row li {
  min-width: 0;
  display: grid;
  justify-items: center;
  gap: 5px;
  color: #8a96a5;
  font-size: 12px;
  text-align: center;
}

.step-row span {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: 1px solid #cfd8e2;
  border-radius: 50%;
  background: #ffffff;
  color: #687487;
  font-weight: 700;
}

.step-row em {
  max-width: 100%;
  font-style: normal;
  overflow-wrap: anywhere;
}

.step-row li.done span,
.step-row li.current span {
  border-color: #2f6b52;
  color: #ffffff;
  background: #2f6b52;
}

.step-row li.done,
.step-row li.current {
  color: #1f684e;
}

.step-row li.attention span {
  border-color: #b64a3e;
  color: #ffffff;
  background: #b64a3e;
}

.step-row li.attention {
  color: #8a3a31;
}

.trade-detail-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.trade-detail-grid dl {
  margin: 0;
  border: 1px solid #edf1f5;
  border-radius: 8px;
  padding: 10px;
  background: #fbfcfd;
  display: grid;
  gap: 4px;
}

.trade-detail-grid dt {
  color: #687487;
  font-size: 12px;
}

.trade-detail-grid dd {
  margin: 0 0 8px;
  color: #172033;
  font-weight: 700;
  overflow-wrap: anywhere;
}

.trade-error {
  margin: 0;
  border: 1px solid #e0a39a;
  border-radius: 8px;
  padding: 10px;
  color: #8a3a31;
  background: #fff6f4;
}

.completed-zone {
  margin-top: 4px;
  display: grid;
  gap: 8px;
}

.completed-filter-note {
  margin: 0 0 8px;
  color: #687487;
  font-size: 12px;
  line-height: 1.5;
}

.completed-toolbar {
  border: 1px solid #dce2e8;
  border-radius: 8px;
  padding: 10px;
  background: #ffffff;
  display: grid;
  grid-template-columns: repeat(2, minmax(150px, 190px)) minmax(190px, auto) minmax(220px, auto) minmax(260px, 1fr);
  gap: 10px;
  align-items: end;
}

.completed-toolbar label {
  min-width: 0;
  display: grid;
  gap: 5px;
  color: #687487;
  font-size: 12px;
}

.completed-toolbar input {
  width: 100%;
  min-height: 32px;
  border: 1px solid #cfd8e2;
  border-radius: 8px;
  padding: 4px 8px;
  color: #172033;
  background: #fbfcfd;
}

.completed-filter-actions,
.completed-pagination {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.completed-profit-summary {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(150px, max-content));
  gap: 8px;
}

.completed-profit-summary div {
  min-height: 48px;
  border: 1px solid #edf1f5;
  border-radius: 8px;
  padding: 6px 8px;
  background: #fbfcfd;
  display: grid;
  align-content: center;
  gap: 2px;
}

.completed-profit-summary dt {
  color: #687487;
  font-size: 12px;
}

.completed-profit-summary dd {
  margin: 0;
  color: #172033;
  font-weight: 700;
}

.completed-profit-summary small {
  color: #687487;
  font-size: 11px;
}

.completed-profit-summary dd.positive {
  color: #1f684e;
}

.completed-profit-summary dd.negative {
  color: #b64a3e;
}

.completed-pagination {
  justify-content: flex-end;
}

.completed-pagination span {
  color: #526071;
  font-size: 13px;
  white-space: nowrap;
}

.completed-pagination button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.completed-trade-row {
  border: 1px solid #dce2e8;
  border-radius: 8px;
  padding: 12px;
  background: #ffffff;
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(540px, 1.6fr);
  gap: 14px;
  align-items: start;
}

.completed-main {
  min-width: 0;
  display: grid;
  gap: 3px;
}

.completed-trade-row strong,
.completed-trade-row span {
  display: block;
  overflow-wrap: anywhere;
}

.completed-trade-row strong {
  color: #172033;
}

.completed-trade-row span {
  color: #687487;
  font-size: 12px;
}

.completed-trade-row dl {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(3, minmax(74px, auto) minmax(88px, 1fr));
  gap: 6px 10px;
  align-items: baseline;
}

.completed-trade-row dt {
  color: #687487;
  font-size: 12px;
}

.completed-trade-row dd {
  margin: 0;
  color: #172033;
  font-weight: 700;
}

.completed-trade-row dd.positive {
  color: #1f684e;
}

.completed-trade-row dd.negative {
  color: #b64a3e;
}

@media (max-width: 980px) {
  .profit-summary-grid,
  .execution-status-grid,
  .trade-detail-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .profit-layout {
    grid-template-columns: 1fr;
  }

  .completed-trade-row {
    grid-template-columns: 1fr;
  }

  .completed-toolbar {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .completed-filter-actions,
  .completed-pagination {
    justify-content: flex-start;
  }

  .completed-trade-row dl {
    grid-template-columns: repeat(2, minmax(74px, auto) minmax(88px, 1fr));
  }

  .step-row {
    grid-template-columns: repeat(4, minmax(70px, 1fr));
  }
}

@media (max-width: 640px) {
  .page-header,
  .trade-head {
    display: grid;
  }

  .toolbar,
  .trade-badges {
    justify-content: flex-start;
  }

  .profit-summary-grid,
  .execution-status-grid,
  .trade-detail-grid,
  .step-row {
    grid-template-columns: 1fr;
  }

  .execution-status-item.wide {
    grid-column: auto;
  }

  .completed-toolbar {
    grid-template-columns: 1fr;
  }

  .completed-trade-row dl {
    grid-template-columns: minmax(74px, auto) minmax(88px, 1fr);
  }
}

/* FOLIO compact data theme */
.api-pill,.status-badge,.roi-badge,.roi-basis-badge,.type-badge{min-height:28px;border-color:var(--folio-line);color:var(--folio-muted);background:var(--folio-surface-soft);font-size:11px;font-weight:700}.api-pill.online,.roi-badge{border-color:#c8dfd0;color:var(--folio-green);background:var(--folio-green-soft)}.api-pill.offline,.status-badge.attention{border-color:#ebceca;color:var(--folio-red);background:var(--folio-red-soft)}.type-badge.warning{border-color:#e7d9bb;color:var(--folio-amber);background:var(--folio-amber-soft)}.secondary-button.active{border-color:#bfd7c7;color:var(--folio-green-dark);background:var(--folio-green-soft)}
.profit-summary-grid,.execution-status-grid{gap:8px}.profit-metric,.execution-status-item{border-color:var(--folio-line);border-radius:14px;background:#fff;box-shadow:var(--folio-shadow)}.profit-metric{min-height:88px;padding:14px}.profit-metric span,.execution-status-item span{color:var(--folio-muted);font-size:11px}.profit-metric strong,.execution-status-item strong{color:var(--folio-ink)}.profit-metric.danger,.execution-status-item.offline{border-color:#ebceca;background:var(--folio-red-soft)}.execution-status-item.online{border-color:#c8dfd0;background:#f3f8f5}.execution-status-item.running{border-color:#cad9df;background:var(--folio-blue-soft)}
.inline-status,.trade-warning{border-color:#e7d9bb;border-radius:11px;color:#805b1c;background:var(--folio-amber-soft)}.profit-layout{gap:14px}.settings-list div,.protection-panel{border-color:#edf0ed}.settings-list dt,.budget-form label,.protection-form label,.protection-group>span,.trade-no,.trade-hash,.trade-detail-grid dt,.completed-filter-note,.completed-toolbar label,.completed-profit-summary dt,.completed-profit-summary small,.completed-trade-row span,.completed-trade-row dt{color:var(--folio-muted)}.settings-list dd,.formula-panel strong,.protection-panel h3,.trade-head h2,.trade-detail-grid dd,.completed-profit-summary dd,.completed-trade-row strong,.completed-trade-row dd{color:var(--folio-ink)}
.formula-panel,.trade-detail-grid dl,.completed-profit-summary div{border-color:#e8ece7;border-radius:12px;background:var(--folio-surface-soft)}.formula-panel small{color:var(--folio-red)}.status-toggle{border-color:var(--folio-line);border-radius:12px;color:var(--folio-muted);background:var(--folio-surface-soft)}.status-toggle strong{color:var(--folio-red)}.status-toggle.active{border-color:#bfd7c7;color:var(--folio-green);background:var(--folio-green-soft)}.status-toggle.active strong{color:var(--folio-green)}
.protection-input-row input,.manual-settle-row input,.completed-toolbar input{border-color:#dfe4df;border-radius:11px;color:var(--folio-ink);background:#fff}.protection-input-row input:focus,.manual-settle-row input:focus,.completed-toolbar input:focus{border-color:var(--folio-green);box-shadow:0 0 0 3px rgba(35,106,76,.1)}.protection-group{border-color:var(--folio-line);border-radius:12px}.protection-chip{border-color:var(--folio-line);border-radius:10px;color:var(--folio-ink);background:var(--folio-surface-soft)}.protection-chip:hover{border-color:#c7d8cc;background:var(--folio-green-soft)}
.profit-trade-card,.completed-toolbar,.completed-trade-row{border-color:var(--folio-line);border-radius:15px;background:#fff;box-shadow:var(--folio-shadow)}.profit-trade-card.attention{border-color:#e8c6c2}.roi-basis-badge{border-color:var(--folio-line);color:var(--folio-muted);background:var(--folio-surface-soft)}.mini-action{border-color:var(--folio-line);color:#405048;background:#f1f4f0}.mini-action:hover{color:var(--folio-green-dark);background:var(--folio-green-soft)}.protect-action{border-color:#e4d5b6;color:var(--folio-amber);background:var(--folio-amber-soft)}.dismiss-action{color:#566159;background:#f1f4f0}.danger-button{border-color:#e6c5c1;color:var(--folio-red);background:var(--folio-red-soft)}
.progress-track{height:8px;background:#e9eee9}.progress-track span{background:var(--folio-green)}.step-row span{border-color:#d8dfd9;color:var(--folio-muted);background:#fff}.step-row li.done span,.step-row li.current span{border-color:var(--folio-green);background:var(--folio-green)}.step-row li.done,.step-row li.current{color:var(--folio-green)}.step-row li.attention span{border-color:var(--folio-red);background:var(--folio-red)}.step-row li.attention,.trade-error{color:var(--folio-red)}.trade-error{border-color:#ebceca;border-radius:11px;background:var(--folio-red-soft)}
.completed-profit-summary dd.positive,.completed-trade-row dd.positive{color:var(--folio-green)}.completed-profit-summary dd.negative,.completed-trade-row dd.negative{color:var(--folio-red)}
.trade-stack,.completed-zone{min-width:0}.completed-toolbar{grid-template-columns:repeat(2,minmax(0,1fr))}.completed-profit-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.completed-filter-actions,.completed-pagination{justify-content:flex-start}
</style>
