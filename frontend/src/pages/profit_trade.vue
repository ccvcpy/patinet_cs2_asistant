<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
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
  completedAtSource?: string | null;
  recordOrigin?: string | null;
  manuallyEdited?: boolean;
};

type ManualEntryAccount = {
  accountId: string;
  name: string;
  steamId?: string | null;
};

type ManualItemSuggestion = {
  marketHashName: string;
  name: string;
};

type ListingsCircuit = {
  status?: "closed" | "open" | "half_open";
  isBlocking?: boolean;
  nextProbeAt?: string | null;
  cooldownUntil?: string | null;
  triggerAccountName?: string | null;
  consecutive429Count?: number;
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
    accountReservedBalances?: Record<string, number>;
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
  manualEntryOptions: {
    accounts: ManualEntryAccount[];
  };
  listingsCircuit: ListingsCircuit;
  runtime?: {
    enabled?: boolean;
    status?: string;
    preparing?: boolean;
    nextAttemptAt?: string | null;
    lastRunAt?: string | null;
    lastRunSummary?: string | null;
  };
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
    accountReservedBalances: {},
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
  manualEntryOptions: { accounts: [] },
  listingsCircuit: { status: "closed", isBlocking: false },
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
const reservedBalanceDrafts = ref<Record<string, string>>({});
const manualSettleInputs = ref<Record<number, string>>({});
const lastRunStorageKey = "profitTrade.lastRun.v1";
const runtimeBusy = ref(false);
const runtimeConfirmEnabled = ref<boolean | null>(null);
const lastRunAt = ref<number | null>(null);
const lastRunResult = ref("");
const completedDateFrom = ref("");
const completedDateTo = ref("");
const completedPage = ref(1);
const completedPageSize = 10;

type ManualRecordForm = {
  marketHashName: string;
  name: string;
  steamAccountId: string;
  steamBuyPrice: string;
  balanceDiscount: string;
  c5SoldNetPrice: string;
  steamBoughtAt: string;
  completedAt: string;
  aAssetId: string;
  bAssetId: string;
  memo: string;
};

const manualRecordOpen = ref(false);
const manualRecordSaving = ref(false);
const manualRecordEditingTradeId = ref<number | null>(null);
const manualRecordError = ref("");
const manualItemQuery = ref("");
const manualItemSuggestions = ref<ManualItemSuggestion[]>([]);
const manualItemSearchOpen = ref(false);
const manualItemSearchBusy = ref(false);
let manualItemSearchTimer: ReturnType<typeof setTimeout> | null = null;
const manualRecordForm = ref<ManualRecordForm>({
  marketHashName: "",
  name: "",
  steamAccountId: "",
  steamBuyPrice: "",
  balanceDiscount: "0.69",
  c5SoldNetPrice: "",
  steamBoughtAt: "",
  completedAt: "",
  aAssetId: "",
  bAssetId: "",
  memo: "",
});

const sortedTrades = computed(() => [...dashboard.value.trades].sort((a, b) => b.id - a.id));
const activeTrades = computed(() => sortedTrades.value.filter((trade) => trade.status !== "completed"));
const completedTrades = computed(() => sortedTrades.value
  .filter((trade) => trade.status === "completed")
  .sort((a, b) => {
    const aTime = completedTradePurchaseTimeMs(a) ?? Number.NEGATIVE_INFINITY;
    const bTime = completedTradePurchaseTimeMs(b) ?? Number.NEGATIVE_INFINITY;
    return bTime - aTime || b.id - a.id;
  }));
const manualEntryAccounts = computed(() => dashboard.value.manualEntryOptions?.accounts ?? []);
const manualRecordPreview = computed(() => {
  const steamBuyPrice = Number(manualRecordForm.value.steamBuyPrice);
  const balanceDiscount = Number(manualRecordForm.value.balanceDiscount);
  const c5SoldNetPrice = Number(manualRecordForm.value.c5SoldNetPrice);
  if (![steamBuyPrice, balanceDiscount, c5SoldNetPrice].every(Number.isFinite) || steamBuyPrice <= 0) {
    return { steamRealCost: null, realizedProfit: null, realizedRoiPct: null };
  }
  const steamRealCost = steamBuyPrice * balanceDiscount;
  return {
    steamRealCost,
    realizedProfit: c5SoldNetPrice - steamRealCost,
    realizedRoiPct: (c5SoldNetPrice / steamBuyPrice - balanceDiscount) * 100,
  };
});
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
const autoRunEnabled = computed(() => Boolean(dashboard.value.runtime?.enabled ?? dashboard.value.config.enabled));
const stickerSlabActive = computed(() => dashboard.value.config.stickerSlabStatus === "active");
const stickerActive = computed(() => dashboard.value.config.stickerStatus === "active");
const apiStatusLabel = computed(() => {
  if (apiOnline.value === true) return "后端 API 已启动";
  if (apiOnline.value === false) return "后端 API 未连接";
  return "后端 API 检查中";
});
const realExecutionLabel = computed(() => (
  dashboard.value.config.allowRealExecution ? "真实执行已开放" : "真实执行未开放"
));
const autoRunStatusLabel = computed(() => {
  if (dashboard.value.runtime?.preparing) return "后端 Worker 启动准备中";
  return autoRunEnabled.value ? `后端 Worker ${dashboard.value.runtime?.status || "运行中"}` : "后端 Worker 已关闭";
});
const nextAutoRunLabel = computed(() => {
  if (!autoRunEnabled.value) return "新机会任务已暂停";
  const nextAt = dashboard.value.runtime?.nextAttemptAt;
  return nextAt ? formatDateTime(nextAt) : "等待后端安排到期任务";
});
const lastRunLabel = computed(() => {
  const backendLastRun = dashboard.value.lastRun;
  if (backendLastRun?.generatedAt && backendLastRun?.summary) {
    return `${formatDateTime(backendLastRun.generatedAt)}｜${backendLastRun.summary}`;
  }
  if (dashboard.value.runtime?.lastRunAt) return `${formatDateTime(dashboard.value.runtime.lastRunAt)}｜${dashboard.value.runtime.lastRunSummary || "后端任务已运行"}`;
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

function isoToBeijingInput(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 19);
}

function beijingInputNow(): string {
  return new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString().slice(0, 19);
}

function beijingInputToIso(value: string): string {
  return `${value}+08:00`;
}

function manualAccountInitials(name: string): string {
  const normalized = String(name || "").trim();
  if (!normalized) return "--";
  const parts = normalized.split(/[^a-zA-Z0-9]+/).filter(Boolean);
  if (parts.length >= 2) return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  return normalized.slice(0, 2).toUpperCase();
}

function resolveTradeSteamAccountId(trade: ProfitTrade): string {
  const note = trade.note ?? {};
  const recordedId = String(note.steamAccountId ?? "").trim();
  const recordedSteamId = String(note.steamId ?? "").trim();
  const recordedName = String(note.steamAccountName ?? "").trim().toLowerCase();
  const matched = manualEntryAccounts.value.find((account) => (
    (recordedId && account.accountId === recordedId)
    || (recordedSteamId && account.steamId === recordedSteamId)
    || (recordedName && account.name.trim().toLowerCase() === recordedName)
  ));
  return matched?.accountId ?? "";
}

function openCreateManualRecord(): void {
  const now = beijingInputNow();
  manualRecordEditingTradeId.value = null;
  manualRecordError.value = "";
  manualItemQuery.value = "";
  manualItemSuggestions.value = [];
  manualItemSearchOpen.value = false;
  manualRecordForm.value = {
    marketHashName: "",
    name: "",
    steamAccountId: "",
    steamBuyPrice: "",
    balanceDiscount: String(dashboard.value.config.balanceDiscount ?? 0.69),
    c5SoldNetPrice: "",
    steamBoughtAt: now,
    completedAt: now,
    aAssetId: "",
    bAssetId: "",
    memo: "",
  };
  manualRecordOpen.value = true;
}

function openEditManualRecord(trade: ProfitTrade): void {
  if (trade.status !== "completed") return;
  manualRecordEditingTradeId.value = trade.id;
  manualRecordError.value = "";
  manualItemQuery.value = trade.name && trade.name !== trade.marketHashName
    ? `${trade.name} / ${trade.marketHashName}`
    : trade.marketHashName;
  manualItemSuggestions.value = [];
  manualItemSearchOpen.value = false;
  manualRecordForm.value = {
    marketHashName: trade.marketHashName,
    name: trade.name && trade.name !== trade.marketHashName ? trade.name : "",
    steamAccountId: resolveTradeSteamAccountId(trade),
    steamBuyPrice: String(trade.steamBuyPrice ?? ""),
    balanceDiscount: String(trade.steamBalanceDiscount ?? dashboard.value.config.balanceDiscount ?? 0.69),
    c5SoldNetPrice: String(trade.c5SoldNetPrice ?? ""),
    steamBoughtAt: isoToBeijingInput(trade.steamBoughtAt),
    completedAt: isoToBeijingInput(trade.completedAt),
    aAssetId: String(trade.aAssetId ?? ""),
    bAssetId: String(trade.bAssetId ?? ""),
    memo: String(trade.note?.manualEditedMemo ?? trade.note?.manualCreatedMemo ?? ""),
  };
  manualRecordOpen.value = true;
}

function closeManualRecord(): void {
  if (manualRecordSaving.value) return;
  manualRecordOpen.value = false;
  manualRecordError.value = "";
}

async function searchManualItems(): Promise<void> {
  manualItemSearchBusy.value = true;
  try {
    const payload = await fetchJson(
      `/api/profit-trade/items/search?query=${encodeURIComponent(manualItemQuery.value.trim())}&limit=20`,
    ) as { items?: ManualItemSuggestion[] };
    manualItemSuggestions.value = payload.items ?? [];
    manualItemSearchOpen.value = true;
    apiOnline.value = true;
  } catch (error) {
    if (error instanceof TypeError) apiOnline.value = false;
    manualRecordError.value = `物品搜索失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    manualItemSearchBusy.value = false;
  }
}

function onManualItemInput(): void {
  manualRecordForm.value.marketHashName = "";
  manualRecordForm.value.name = "";
  manualItemSearchOpen.value = true;
  if (manualItemSearchTimer) clearTimeout(manualItemSearchTimer);
  manualItemSearchTimer = setTimeout(() => void searchManualItems(), 220);
}

function chooseManualItem(item: ManualItemSuggestion): void {
  manualRecordForm.value.marketHashName = item.marketHashName;
  manualRecordForm.value.name = item.name !== item.marketHashName ? item.name : "";
  manualItemQuery.value = item.name !== item.marketHashName
    ? `${item.name} / ${item.marketHashName}`
    : item.marketHashName;
  manualItemSearchOpen.value = false;
  manualRecordError.value = "";
}

async function saveManualRecord(): Promise<void> {
  const form = manualRecordForm.value;
  const steamBuyPrice = Number(form.steamBuyPrice);
  const balanceDiscount = Number(form.balanceDiscount);
  const c5SoldNetPrice = Number(form.c5SoldNetPrice);
  if (!form.marketHashName.trim()) {
    manualRecordError.value = "请先搜索并选择一个标准物品名称，不能只输入自由文本";
    return;
  }
  if (!Number.isFinite(steamBuyPrice) || steamBuyPrice <= 0 || !Number.isFinite(c5SoldNetPrice) || c5SoldNetPrice <= 0) {
    manualRecordError.value = "Steam 买入价和 C5 实际到手必须大于 0";
    return;
  }
  if (!Number.isFinite(balanceDiscount) || balanceDiscount <= 0 || balanceDiscount > 1) {
    manualRecordError.value = "余额折扣必须大于 0 且不超过 1";
    return;
  }
  if (!form.steamBoughtAt || !form.completedAt) {
    manualRecordError.value = "Steam 购买时间和结算时间都必须填写";
    return;
  }
  if (new Date(beijingInputToIso(form.completedAt)) < new Date(beijingInputToIso(form.steamBoughtAt))) {
    manualRecordError.value = "结算时间不能早于 Steam 购买时间";
    return;
  }
  manualRecordSaving.value = true;
  manualRecordError.value = "";
  const editing = manualRecordEditingTradeId.value !== null;
  try {
    await fetchJson(
      editing
        ? "/api/profit-trade/manual-record/update"
        : "/api/profit-trade/manual-record/create",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...(editing ? { tradeId: manualRecordEditingTradeId.value } : {}),
          marketHashName: form.marketHashName.trim(),
          name: form.name.trim() || null,
          steamAccountId: form.steamAccountId || null,
          steamBuyPrice,
          balanceDiscount,
          c5SoldNetPrice,
          steamBoughtAt: beijingInputToIso(form.steamBoughtAt),
          completedAt: beijingInputToIso(form.completedAt),
          aAssetId: form.aAssetId.trim() || null,
          bAssetId: form.bAssetId.trim() || null,
          memo: form.memo.trim() || null,
        }),
      },
    );
    apiOnline.value = true;
    manualRecordOpen.value = false;
    message.value = editing ? "已保存人工修正" : "已新增手工流水";
    resetCompletedPage();
    await loadDashboard();
  } catch (error) {
    if (error instanceof TypeError) apiOnline.value = false;
    manualRecordError.value = `保存失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    manualRecordSaving.value = false;
  }
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

function reservedBalanceKey(account: ManualEntryAccount): string {
  return String(account.steamId || account.accountId || "").trim();
}

function syncReservedBalanceDrafts(): void {
  const configured = dashboard.value.config.accountReservedBalances || {};
  reservedBalanceDrafts.value = Object.fromEntries(
    dashboard.value.manualEntryOptions.accounts.map((account) => {
      const key = reservedBalanceKey(account);
      const configuredValue = configured[key]
        ?? configured[account.accountId]
        ?? configured[account.name]
        ?? 0;
      return [account.accountId, String(configuredValue)];
    }),
  );
}

async function loadDashboard(): Promise<void> {
  loading.value = true;
  message.value = "";
  try {
    const payload = (await fetchJson("/api/profit-trade/dashboard")) as ProfitDashboard;
    let runtimeState = payload.runtime;
    try {
      const runtimePayload = await fetchJson("/api/runtime/state?executor=profit_trade") as {
        state?: ProfitDashboard["runtime"];
      };
      runtimeState = runtimePayload.state || runtimeState;
    } catch {
      // The dashboard remains usable while the shared runtime endpoint starts up.
    }
    dashboard.value = {
      ...payload,
      runtime: runtimeState,
      listingsCircuit: payload.listingsCircuit || { status: "closed", isBlocking: false },
    };
    dailySteamBudgetDraft.value = String(dashboard.value.config.dailySteamBudget ?? 1000);
    syncReservedBalanceDrafts();
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
  const nextEnabled = runtimeConfirmEnabled.value ?? !autoRunEnabled.value;
  message.value = "";
  runtimeBusy.value = true;
  try {
    await fetchJson("/api/runtime/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ executor: "profit_trade", enabled: nextEnabled }),
    });
    apiOnline.value = true;
    runtimeConfirmEnabled.value = null;
    message.value = nextEnabled ? "Profit Trade 后端 Worker 已提交开启，正在执行 Cookie 门禁。" : "Profit Trade 新机会已停止；已有流水继续安全闭环。";
    await loadDashboard();
  } catch (error) {
    apiOnline.value = false;
    message.value = `API未连接，无法${nextEnabled ? "开启" : "关闭"}后端开关`;
  } finally {
    runtimeBusy.value = false;
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

async function saveAccountReservedBalances(): Promise<void> {
  const nextBalances: Record<string, number> = {};
  for (const account of dashboard.value.manualEntryOptions.accounts) {
    const rawValue = reservedBalanceDrafts.value[account.accountId] ?? "0";
    const value = Number(rawValue);
    if (!Number.isFinite(value) || value < 0) {
      message.value = `${account.name} 的保留余额必须是大于等于 0 的数字`;
      return;
    }
    if (value > 0) nextBalances[reservedBalanceKey(account)] = value;
  }
  message.value = "";
  try {
    await fetchJson("/api/profit-trade/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountReservedBalances: nextBalances }),
    });
    apiOnline.value = true;
    await loadDashboard();
    message.value = "Steam 账号保留余额已保存，下一次 Profit Trade 买入立即生效";
  } catch (error) {
    apiOnline.value = false;
    message.value = `Steam 账号保留余额保存失败：${error instanceof Error ? error.message : String(error)}`;
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
  window.addEventListener("profit-trade:config-changed", handleSharedConfigChange);
});

onUnmounted(() => {
  if (manualItemSearchTimer) clearTimeout(manualItemSearchTimer);
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
        <button class="secondary-button" type="button" @click="refreshSales">
          刷新C5状态
        </button>
        <button class="secondary-button" type="button" @click="sendDailyReport">
          发送日报
        </button>
        <button class="primary-button" type="button" :disabled="runtimeBusy" @click="runtimeConfirmEnabled = !autoRunEnabled">
          {{ autoRunEnabled ? "关闭执行器" : "开启执行器" }}
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
        <strong>{{ autoRunEnabled ? "已开启" : "已关闭" }}</strong>
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
      <article class="execution-status-item" :class="{ online: autoRunEnabled }">
        <span>后端 Worker</span>
        <strong>{{ autoRunStatusLabel }}</strong>
      </article>
      <article class="execution-status-item">
        <span>下一次执行</span>
        <strong>{{ nextAutoRunLabel }}</strong>
      </article>
      <article class="execution-status-item wide">
        <span>上一次后端任务</span>
        <strong>{{ lastRunLabel }}</strong>
      </article>
    </section>

    <div v-if="runtimeConfirmEnabled !== null" class="runtime-confirm-backdrop" @click.self="runtimeConfirmEnabled = null">
      <section class="runtime-confirm-dialog" role="dialog" aria-modal="true">
        <span><FolioIcon :name="runtimeConfirmEnabled ? 'shield' : 'warning'" :size="22" /></span>
        <h2>{{ runtimeConfirmEnabled ? "开启 Profit Trade 执行器" : "关闭 Profit Trade 执行器" }}</h2>
        <p v-if="runtimeConfirmEnabled">后端会先通过共享 Cookie 门禁。全部账号 Cookie 有效后，才开放新机会、新锁 A 和新买 B。</p>
        <p v-else>关闭后停止新机会、新锁 A 和新买 B；已有 Steam 终态确认、已买 B 后续 C5 上架、售出同步、安全改价和收益结算仍会继续，并可能产生真实写操作。</p>
        <div><button class="secondary-button" type="button" @click="runtimeConfirmEnabled = null">取消</button><button class="primary-button" type="button" :disabled="runtimeBusy" @click="toggleEnabled">{{ runtimeBusy ? "提交中…" : "确认" }}</button></div>
      </section>
    </div>

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
        <form class="wallet-reserve-panel" @submit.prevent="saveAccountReservedBalances">
          <div class="wallet-reserve-heading">
            <div>
              <span>账号资金边界</span>
              <h3>Steam 余额保留</h3>
            </div>
            <button class="mini-action protect-action" type="submit">保存保留金额</button>
          </div>
          <p>Profit Trade 只使用“实际钱包余额 − 保留金额”。这里不会冻结 Steam 钱包，也不影响其他执行器。</p>
          <div class="wallet-reserve-list">
            <label
              v-for="account in dashboard.manualEntryOptions.accounts"
              :key="account.accountId"
              class="wallet-reserve-account"
            >
              <span>
                <strong>{{ account.name }}</strong>
                <small>{{ account.steamId || account.accountId }}</small>
              </span>
              <span class="wallet-reserve-input">
                <em>CNY</em>
                <input
                  v-model.trim="reservedBalanceDrafts[account.accountId]"
                  type="number"
                  min="0"
                  step="0.01"
                  inputmode="decimal"
                  autocomplete="off"
                  aria-label="Profit Trade 保留余额"
                >
              </span>
            </label>
          </div>
          <p v-if="!dashboard.manualEntryOptions.accounts.length" class="wallet-reserve-empty">
            暂未读取到本地 Steam 账号，不能保存账号保留余额。
          </p>
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
          <span v-if="dashboard.listingsCircuit.isBlocking">Steam listings 冷却期间不会创建新的买 B 流水或锁定 A；观察区仍会继续更新行情。</span>
          <span v-else>候选、买入、上架和人工处理中的流水会显示在这里。</span>
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

            <p v-if="dashboard.listingsCircuit.isBlocking && trade.stepIndex <= 2" class="trade-circuit-note">
              Steam listings 冷却中；这笔买入前流水会安全停止并释放 A，不会发送新的购买请求。下次探测
              {{ formatDateTime(dashboard.listingsCircuit.nextProbeAt || dashboard.listingsCircuit.cooldownUntil) }}。
            </p>

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
            <button class="primary-button" type="button" @click="openCreateManualRecord">
              新增手工流水
            </button>
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
            <button
              class="completed-edit-button"
              type="button"
              title="编辑本地记录"
              aria-label="编辑本地记录"
              @click="openEditManualRecord(trade)"
            >
              <FolioIcon name="edit" :size="16" />
            </button>
            <div class="completed-main">
              <div class="completed-card-head">
                <strong>{{ trade.name || trade.marketHashName }}</strong>
                <span v-if="trade.recordOrigin === 'manual_backfill'" class="manual-record-badge">手工补录</span>
                <span v-else-if="trade.manuallyEdited" class="manual-record-badge corrected">人工修正</span>
              </div>
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
    <div v-if="manualRecordOpen" class="manual-record-backdrop" @click.self="closeManualRecord">
      <section class="manual-record-modal" role="dialog" aria-modal="true" aria-labelledby="manual-record-title">
        <header class="manual-record-header">
          <div>
            <span>{{ manualRecordEditingTradeId === null ? "历史补录" : "本地修正" }}</span>
            <h2 id="manual-record-title">
              {{ manualRecordEditingTradeId === null ? "新增手工流水" : "编辑已完结流水" }}
            </h2>
          </div>
          <button class="mini-action" type="button" :disabled="manualRecordSaving" @click="closeManualRecord">关闭</button>
        </header>
        <p class="manual-record-notice">
          只修正本地记录，不会修改 Steam/C5，也不会触发购买、上架、资产锁或占用自动执行预算。
        </p>
        <form class="manual-record-form" @submit.prevent="saveManualRecord">
          <label class="wide-field manual-item-field">
            <span>搜索物品名称 *</span>
            <div class="manual-item-search">
              <FolioIcon name="scan" :size="17" />
              <input
                v-model="manualItemQuery"
                autocomplete="off"
                placeholder="输入中文名或英文名，例如：次时代、M4A4"
                @input="onManualItemInput"
                @focus="searchManualItems"
              >
              <em v-if="manualItemSearchBusy">搜索中…</em>
              <div v-if="manualItemSearchOpen" class="manual-item-suggestions">
                <button
                  v-for="item in manualItemSuggestions"
                  :key="item.marketHashName"
                  type="button"
                  @click="chooseManualItem(item)"
                >
                  <strong>{{ item.name }}</strong>
                  <small>{{ item.marketHashName }}</small>
                </button>
                <p v-if="!manualItemSearchBusy && manualItemSuggestions.length === 0">没有匹配结果，请换一部分名称再试</p>
              </div>
            </div>
            <small v-if="manualRecordForm.marketHashName" class="manual-item-selected">
              已选择标准名称：{{ manualRecordForm.marketHashName }}
            </small>
          </label>
          <fieldset class="manual-account-picker wide-field">
            <legend>Steam 买入账号</legend>
            <div class="manual-account-heading">
              <div>
                <span>交付目标</span>
                <strong>买入 B 的 Steam 账号</strong>
              </div>
              <button
                class="manual-account-unrecorded"
                :class="{ selected: manualRecordForm.steamAccountId === '' }"
                type="button"
                @click="manualRecordForm.steamAccountId = ''"
              >
                未记录
              </button>
            </div>
            <div class="manual-account-cards">
              <button
                v-for="account in manualEntryAccounts"
                :key="account.accountId"
                type="button"
                :class="{ selected: manualRecordForm.steamAccountId === account.accountId }"
                @click="manualRecordForm.steamAccountId = account.accountId"
              >
                <i>{{ manualAccountInitials(account.name) }}</i>
                <span>
                  <strong>{{ account.name }}</strong>
                  <small>{{ account.steamId || "SteamID 未记录" }}</small>
                </span>
                <b>{{ manualRecordForm.steamAccountId === account.accountId ? "已选择" : "选择" }}</b>
              </button>
              <p v-if="manualEntryAccounts.length === 0">
                后端没有返回安全账号列表，请确认 Profit Trade API 已加载当前版本。
              </p>
            </div>
          </fieldset>
          <label>
            <span>Steam 买入价 *</span>
            <input v-model="manualRecordForm.steamBuyPrice" type="number" min="0.01" step="0.01" required>
          </label>
          <label>
            <span>该笔余额折扣 *</span>
            <input v-model="manualRecordForm.balanceDiscount" type="number" min="0.0001" max="1" step="0.0001" required>
          </label>
          <label>
            <span>C5 实际售出到手 *</span>
            <input v-model="manualRecordForm.c5SoldNetPrice" type="number" min="0.01" step="0.01" required>
          </label>
          <label>
            <span>Steam 购买时间（北京时间）*</span>
            <input v-model="manualRecordForm.steamBoughtAt" type="datetime-local" step="1" required>
          </label>
          <label>
            <span>C5 售出 / 结算时间（北京时间）*</span>
            <input v-model="manualRecordForm.completedAt" type="datetime-local" step="1" required>
          </label>
          <label>
            <span>A assetId（可选）</span>
            <input v-model.trim="manualRecordForm.aAssetId" autocomplete="off">
          </label>
          <label>
            <span>B assetId（可选）</span>
            <input v-model.trim="manualRecordForm.bAssetId" autocomplete="off">
          </label>
          <label class="wide-field">
            <span>备注（可选）</span>
            <textarea v-model.trim="manualRecordForm.memo" rows="3" maxlength="1000" />
          </label>
          <dl class="manual-record-preview wide-field">
            <div><dt>真实成本</dt><dd>{{ formatMoney(manualRecordPreview.steamRealCost) }}</dd></div>
            <div><dt>实际利润</dt><dd :class="signedClass(manualRecordPreview.realizedProfit)">{{ formatMoney(manualRecordPreview.realizedProfit) }}</dd></div>
            <div><dt>实际 ROI</dt><dd :class="signedClass(manualRecordPreview.realizedRoiPct)">{{ formatPct(manualRecordPreview.realizedRoiPct) }}</dd></div>
          </dl>
          <p v-if="manualRecordError" class="manual-record-error wide-field">{{ manualRecordError }}</p>
          <footer class="manual-record-actions wide-field">
            <button class="secondary-button" type="button" :disabled="manualRecordSaving" @click="closeManualRecord">取消</button>
            <button class="primary-button" type="submit" :disabled="manualRecordSaving">
              {{ manualRecordSaving ? "保存中…" : "保存本地记录" }}
            </button>
          </footer>
        </form>
      </section>
    </div>
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

.wallet-reserve-panel {
  margin-top: 14px;
  border: 1px solid #dfe8e1;
  border-radius: 14px;
  padding: 13px;
  display: grid;
  gap: 11px;
  background: #f7faf8;
}

.wallet-reserve-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.wallet-reserve-heading > div {
  display: grid;
  gap: 2px;
}

.wallet-reserve-heading span {
  color: var(--folio-green);
  font-size: 10px;
  font-weight: 800;
  letter-spacing: .08em;
}

.wallet-reserve-heading h3 {
  margin: 0;
  color: var(--folio-ink);
  font-size: 15px;
}

.wallet-reserve-panel > p {
  margin: 0;
  color: var(--folio-muted);
  font-size: 11px;
  line-height: 1.55;
}

.wallet-reserve-list {
  display: grid;
  gap: 7px;
}

.wallet-reserve-account {
  min-width: 0;
  border: 1px solid var(--folio-line);
  border-radius: 11px;
  padding: 9px 10px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 128px;
  align-items: center;
  gap: 10px;
  background: #fff;
}

.wallet-reserve-account > span:first-child {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.wallet-reserve-account strong,
.wallet-reserve-account small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wallet-reserve-account strong {
  color: var(--folio-ink);
  font-size: 12px;
}

.wallet-reserve-account small {
  color: var(--folio-muted);
  font-size: 9px;
}

.wallet-reserve-input {
  position: relative;
}

.wallet-reserve-input em {
  position: absolute;
  left: 9px;
  top: 8px;
  z-index: 1;
  color: var(--folio-muted);
  font-size: 9px;
  font-style: normal;
  font-weight: 700;
}

.wallet-reserve-input input {
  width: 100%;
  min-height: 34px;
  box-sizing: border-box;
  border: 1px solid #dfe4df;
  border-radius: 9px;
  padding: 5px 8px 5px 38px;
  color: var(--folio-ink);
  background: #fff;
  font: inherit;
  font-size: 12px;
  text-align: right;
  outline: none;
}

.wallet-reserve-input input:focus {
  border-color: var(--folio-green);
  box-shadow: 0 0 0 3px rgba(35, 106, 76, .1);
}

.wallet-reserve-empty {
  color: var(--folio-red) !important;
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
  position: relative;
  border: 1px solid #dce2e8;
  border-radius: 8px;
  padding: 12px 52px 12px 12px;
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
.trade-circuit-note{margin:0;padding:9px 11px;border:1px solid #dfc77e;border-radius:10px;color:#665526;background:#fff9e9;font-size:11px}
.completed-profit-summary dd.positive,.completed-trade-row dd.positive{color:var(--folio-green)}.completed-profit-summary dd.negative,.completed-trade-row dd.negative{color:var(--folio-red)}
.trade-stack,.completed-zone{min-width:0}.completed-toolbar{grid-template-columns:repeat(2,minmax(0,1fr))}.completed-profit-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.completed-filter-actions,.completed-pagination{justify-content:flex-start}
.panel-title-row{gap:12px}.panel-title-row>.primary-button{margin-left:auto}.completed-card-head{display:flex;align-items:center;gap:8px;min-width:0}.completed-card-head strong{min-width:0}.manual-record-badge{display:inline-flex;align-items:center;min-height:24px;padding:3px 8px;border:1px solid #bfd7c7;border-radius:999px;color:var(--folio-green);background:var(--folio-green-soft);font-size:11px;font-weight:700;white-space:nowrap}.manual-record-badge.corrected{border-color:#d9d2b9;color:#725f20;background:#fbf7e8}.completed-edit-button{position:absolute;top:12px;right:12px;width:32px;height:32px;padding:0;border:1px solid var(--folio-line);border-radius:9px;display:grid;place-items:center;color:var(--folio-muted);background:#fff;box-shadow:0 2px 8px rgba(28,48,37,.06);cursor:pointer;transition:border-color .15s ease,color .15s ease,background .15s ease}.completed-edit-button:hover{border-color:#bfd7c7;color:var(--folio-green-dark);background:var(--folio-green-soft)}
.manual-record-backdrop{position:fixed;z-index:1000;inset:0;display:grid;place-items:center;padding:24px;background:rgba(20,34,27,.46);backdrop-filter:blur(4px)}.manual-record-modal{width:min(920px,calc(100vw - 48px));max-height:calc(100vh - 48px);overflow:auto;border:1px solid var(--folio-line);border-radius:20px;padding:22px;background:#fff;box-shadow:0 24px 70px rgba(19,49,36,.22)}.manual-record-header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.manual-record-header span{color:var(--folio-green);font-size:11px;font-weight:800;letter-spacing:.08em}.manual-record-header h2{margin:4px 0 0;color:var(--folio-ink);font-size:22px}.manual-record-notice{margin:16px 0;padding:11px 13px;border:1px solid #cfe0d4;border-radius:12px;color:#315d48;background:var(--folio-green-soft);font-size:13px;line-height:1.6}.manual-record-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.manual-record-form label{display:grid;gap:7px;color:var(--folio-muted);font-size:12px;font-weight:700}.manual-record-form .wide-field{grid-column:1/-1}.manual-record-form input,.manual-record-form select,.manual-record-form textarea{width:100%;box-sizing:border-box;border:1px solid #dfe4df;border-radius:11px;padding:10px 11px;color:var(--folio-ink);background:#fff;font:inherit;outline:none}.manual-record-form textarea{resize:vertical;line-height:1.5}.manual-record-form input:focus,.manual-record-form select:focus,.manual-record-form textarea:focus{border-color:var(--folio-green);box-shadow:0 0 0 3px rgba(35,106,76,.1)}.manual-item-search{position:relative}.manual-item-search>svg{position:absolute;z-index:2;left:12px;top:12px;color:var(--folio-green)}.manual-item-search>input{padding-left:39px;padding-right:82px}.manual-item-search>em{position:absolute;right:12px;top:11px;color:var(--folio-muted);font-size:11px;font-style:normal;font-weight:600}.manual-item-suggestions{position:absolute;z-index:30;top:46px;left:0;right:0;max-height:290px;overflow:auto;border:1px solid var(--folio-line);border-radius:12px;padding:6px;background:#fff;box-shadow:0 18px 50px rgba(25,48,36,.14)}.manual-item-suggestions button{width:100%;padding:9px 10px;border:0;border-radius:8px;display:grid;gap:3px;text-align:left;background:#fff;cursor:pointer}.manual-item-suggestions button:hover{background:var(--folio-green-soft)}.manual-item-suggestions strong{color:var(--folio-ink);font-size:13px}.manual-item-suggestions small{color:var(--folio-muted);font-size:11px}.manual-item-suggestions p{margin:5px;padding:10px;color:var(--folio-muted);font-size:12px;font-weight:500}.manual-item-selected{color:var(--folio-green)!important;font-size:11px;font-weight:600}.manual-account-picker{margin:0;border:1px solid var(--folio-line);border-radius:14px;padding:13px;background:var(--folio-surface-soft)}.manual-account-picker legend{padding:0 5px;color:var(--folio-muted);font-size:12px;font-weight:700}.manual-account-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.manual-account-heading>div{display:grid;gap:2px}.manual-account-heading span{color:var(--folio-green);font-size:10px;font-weight:800;letter-spacing:.06em}.manual-account-heading strong{color:var(--folio-ink);font-size:14px}.manual-account-unrecorded{border:1px solid var(--folio-line);border-radius:9px;padding:6px 10px;color:var(--folio-muted);background:#fff;font-size:11px;font-weight:700}.manual-account-unrecorded.selected{border-color:#9fc5ad;color:var(--folio-green-dark);background:var(--folio-green-soft)}.manual-account-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.manual-account-cards>button{position:relative;min-width:0;min-height:92px;padding:10px;border:1px solid #dce5de;border-radius:11px;display:grid;grid-template-columns:32px minmax(0,1fr);grid-template-rows:auto auto;gap:6px 8px;text-align:left;background:#fff;cursor:pointer}.manual-account-cards>button:hover{border-color:#9fc5ad;background:#fbfdfb}.manual-account-cards>button.selected{border-color:var(--folio-green);background:#f1f7f3;box-shadow:inset 0 0 0 1px var(--folio-green)}.manual-account-cards i{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;color:#fff;background:#718d7d;font-size:10px;font-style:normal;font-weight:800}.manual-account-cards>button.selected i{background:var(--folio-green)}.manual-account-cards span{min-width:0;display:grid;gap:2px}.manual-account-cards strong,.manual-account-cards small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.manual-account-cards strong{color:var(--folio-ink);font-size:11px}.manual-account-cards small{color:var(--folio-muted);font-size:9px}.manual-account-cards b{grid-column:1/3;justify-self:end;color:var(--folio-green);font-size:9px}.manual-account-cards>p{grid-column:1/-1;margin:4px;padding:10px;color:var(--folio-red);font-size:12px;text-align:center}.manual-record-preview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:2px 0 0}.manual-record-preview div{border:1px solid var(--folio-line);border-radius:12px;padding:11px;background:var(--folio-surface-soft)}.manual-record-preview dt{color:var(--folio-muted);font-size:11px}.manual-record-preview dd{margin:4px 0 0;color:var(--folio-ink);font-size:16px;font-weight:800}.manual-record-preview dd.positive{color:var(--folio-green)}.manual-record-preview dd.negative{color:var(--folio-red)}.manual-record-error{margin:0;padding:10px 12px;border:1px solid #ebceca;border-radius:11px;color:var(--folio-red);background:var(--folio-red-soft);font-size:13px}.manual-record-actions{display:flex;justify-content:flex-end;gap:10px;padding-top:4px}
.runtime-confirm-backdrop{position:fixed;z-index:1100;inset:0;display:grid;place-items:center;padding:24px;background:rgba(20,34,27,.46);backdrop-filter:blur(4px)}.runtime-confirm-dialog{width:min(470px,calc(100vw - 48px));border:1px solid var(--folio-line);border-radius:19px;padding:24px;background:#fff;box-shadow:0 24px 70px rgba(19,49,36,.22)}.runtime-confirm-dialog>span{display:grid;place-items:center;width:44px;height:44px;border-radius:12px;color:var(--folio-green);background:var(--folio-green-soft)}.runtime-confirm-dialog h2{margin:15px 0 8px;color:var(--folio-ink);font-size:21px}.runtime-confirm-dialog p{margin:0;color:var(--folio-muted);font-size:13px;line-height:1.7}.runtime-confirm-dialog>div{display:flex;justify-content:flex-end;gap:9px;margin-top:19px}
</style>
