import { computed, onMounted, onUnmounted, ref } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
import ProfitTradeLongBuyStrategyPanel from "../components/ProfitTradeLongBuyStrategyPanel.vue";
import ProfitTradeRoiWatch from "../components/ProfitTradeRoiWatch.vue";
import { requiresLongBuyConfigConfirmation, resolveProfitTradeLongBuyStrategyState, usesProfitTradeRuntimeToggle, } from "../components/profit_trade_long_buy_strategy";
const fallbackDashboard = {
    generatedAt: "",
    config: {
        enabled: false,
        allowRealExecution: false,
        longBuyEnabled: false,
        longBuyAllowRealExecution: false,
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
        protectedMarketHashNameItems: [],
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
const dashboard = ref(fallbackDashboard);
const listingsCooling = computed(() => dashboard.value.listingsCircuit.status === "open");
const loading = ref(false);
const apiOnline = ref(null);
const message = ref("");
const manualProtectedAssetId = ref("");
const manualProtectedMarketHashName = ref("");
const manualProtectedSteamId = ref("");
const protectionListOpen = ref(false);
const protectionItemQuery = ref("");
const protectionItemSuggestions = ref([]);
const protectionItemSearchOpen = ref(false);
const protectionItemSearchBusy = ref(false);
const protectionItemHasMore = ref(false);
const protectionItemNextOffset = ref(0);
const protectionError = ref("");
const dailySteamBudgetDraft = ref("1000");
const reservedBalanceDrafts = ref({});
const manualSettleInputs = ref({});
const lastRunStorageKey = "profitTrade.lastRun.v1";
const runtimeBusy = ref(false);
const runOnceBusy = ref(false);
const runtimeConfirmEnabled = ref(null);
const runtimeConfirmError = ref("");
const configToggleBusy = ref(null);
const longBuyWriteConfirm = ref(null);
const longBuyWriteConfirmError = ref("");
const countdownNow = ref(Date.now());
const lastRunAt = ref(null);
const lastRunResult = ref("");
const completedDateFrom = ref("");
const completedDateTo = ref("");
const completedDataset = ref({
    generatedAt: "",
    summary: { count: 0, realizedProfit: 0, steamBuyTotal: 0 },
    items: [],
});
const completedAllSummary = ref({
    count: 0,
    realizedProfit: 0,
    steamBuyTotal: 0,
});
const completedPage = ref(1);
const completedPageSize = 10;
const manualRecordOpen = ref(false);
const manualRecordSaving = ref(false);
const manualRecordEditingTradeId = ref(null);
const manualRecordError = ref("");
const manualItemQuery = ref("");
const manualItemSuggestions = ref([]);
const manualItemSearchOpen = ref(false);
const manualItemSearchBusy = ref(false);
const manualItemHasMore = ref(false);
const manualItemNextOffset = ref(0);
let manualItemSearchTimer = null;
let protectionItemSearchTimer = null;
let countdownTimer = null;
let runtimeScheduleTimer = null;
const manualRecordForm = ref({
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
const completedTrades = computed(() => [...completedDataset.value.items]
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
        if (time === null)
            return !hasFilter;
        if (from !== null && time < from)
            return false;
        if (to !== null && time > to)
            return false;
        return true;
    });
});
const completedTotalProfit = computed(() => completedAllSummary.value.realizedProfit);
const completedFilteredProfit = computed(() => completedFilteredTrades.value.reduce((total, trade) => total + (Number(trade.realizedProfit) || 0), 0));
const completedTotalSteamBuy = computed(() => completedAllSummary.value.steamBuyTotal);
const completedFilteredSteamBuy = computed(() => completedFilteredTrades.value.reduce((total, trade) => total + (Number(trade.steamBuyPrice) || 0), 0));
const completedProfitSummaryLabel = computed(() => (completedHasDateFilter.value ? "当前筛选总收益" : "总收益"));
const completedSteamBuySummaryLabel = computed(() => (completedHasDateFilter.value ? "筛选已结算 Steam买入" : "已结算 Steam买入总额"));
const completedTotalPages = computed(() => Math.max(1, Math.ceil(completedFilteredTrades.value.length / completedPageSize)));
const completedCurrentPage = computed(() => Math.min(Math.max(1, completedPage.value), completedTotalPages.value));
const completedPagedTrades = computed(() => {
    const start = (completedCurrentPage.value - 1) * completedPageSize;
    return completedFilteredTrades.value.slice(start, start + completedPageSize);
});
const completedPageRangeLabel = computed(() => {
    const total = completedFilteredTrades.value.length;
    if (total === 0)
        return "0 / 0";
    const start = (completedCurrentPage.value - 1) * completedPageSize + 1;
    const end = Math.min(start + completedPageSize - 1, total);
    return `${start}-${end} / ${total}`;
});
const protectedAssetCount = computed(() => dashboard.value.config.protectedAssetIds?.length ?? 0);
const protectedNameCount = computed(() => dashboard.value.config.protectedMarketHashNames?.length ?? 0);
const protectedSteamCount = computed(() => dashboard.value.config.protectedSteamIds?.length ?? 0);
const protectedAssetPreview = computed(() => dashboard.value.config.protectedAssetIds ?? []);
const protectedNamePreview = computed(() => {
    const detailed = dashboard.value.config.protectedMarketHashNameItems ?? [];
    if (detailed.length)
        return detailed;
    return (dashboard.value.config.protectedMarketHashNames ?? []).map((marketHashName) => ({
        marketHashName,
        name: marketHashName,
    }));
});
const protectedSteamPreview = computed(() => ((dashboard.value.config.protectedSteamIds ?? []).map((steamId) => {
    const account = manualEntryAccounts.value.find((item) => item.steamId === steamId);
    return {
        accountId: account?.accountId ?? null,
        name: account?.name ?? "未导入账号",
        steamId,
    };
})));
const autoRunEnabled = computed(() => Boolean(dashboard.value.runtime?.enabled ?? dashboard.value.config.enabled));
const profitCycleRunning = computed(() => Boolean(runOnceBusy.value || dashboard.value.runtime?.taskRunning));
const stickerSlabActive = computed(() => dashboard.value.config.stickerSlabStatus === "active");
const stickerActive = computed(() => dashboard.value.config.stickerStatus === "active");
const apiStatusLabel = computed(() => {
    if (apiOnline.value === true)
        return "后端 API 已启动";
    if (apiOnline.value === false)
        return "后端 API 未连接";
    return "后端 API 检查中";
});
const realExecutionLabel = computed(() => (dashboard.value.config.allowRealExecution ? "真实执行已开放" : "真实执行未开放"));
const longBuyStrategyState = computed(() => (resolveProfitTradeLongBuyStrategyState(dashboard.value.config)));
const longBuyRemoteWritesEnabled = computed(() => longBuyStrategyState.value.canWriteSteam);
const longBuyObservationMode = computed(() => longBuyStrategyState.value.mode === "observe");
const longBuyExecutionLabel = computed(() => {
    if (longBuyStrategyState.value.mode === "observe")
        return "观察模式（不写 Steam 求购）";
    return longBuyStrategyState.value.label;
});
const longBuyExecutionDetail = computed(() => longBuyStrategyState.value.detail);
const autoRunStatusLabel = computed(() => {
    if (profitCycleRunning.value)
        return "Profit Trade 本轮执行中";
    if (dashboard.value.runtime?.preparing)
        return "后端 Worker 启动准备中";
    return autoRunEnabled.value ? "后端 10 分钟循环运行中" : "后端 Worker 已关闭";
});
const autoRunCountdown = computed(() => {
    if (!autoRunEnabled.value)
        return "";
    const nextAt = dashboard.value.runtime?.nextAttemptAt;
    if (!nextAt)
        return "";
    const target = new Date(nextAt).getTime();
    if (!Number.isFinite(target))
        return "";
    return formatCountdown(target - countdownNow.value);
});
const nextAutoRunLabel = computed(() => {
    if (!autoRunEnabled.value)
        return "新机会任务已暂停";
    if (profitCycleRunning.value)
        return "本轮执行中，完成后重新安排10分钟倒计时";
    const nextAt = dashboard.value.runtime?.nextAttemptAt;
    if (!nextAt)
        return "等待后端安排到期任务";
    const target = new Date(nextAt).getTime();
    if (!Number.isFinite(target))
        return "等待后端安排到期任务";
    return `${formatDateTime(nextAt)}（${autoRunCountdown.value}）`;
});
const lastRunLabel = computed(() => {
    const backendLastRun = dashboard.value.lastRun;
    const runtimeLastRunAt = dashboard.value.runtime?.lastRunAt;
    const backendTime = backendLastRun?.generatedAt
        ? new Date(backendLastRun.generatedAt).getTime()
        : Number.NEGATIVE_INFINITY;
    const runtimeTime = runtimeLastRunAt
        ? new Date(runtimeLastRunAt).getTime()
        : Number.NEGATIVE_INFINITY;
    if (runtimeLastRunAt && runtimeTime >= backendTime) {
        return `${formatDateTime(runtimeLastRunAt)}｜${dashboard.value.runtime?.lastRunSummary || "后端任务已运行"}`;
    }
    if (backendLastRun?.generatedAt && backendLastRun?.summary) {
        return `${formatDateTime(backendLastRun.generatedAt)}｜${backendLastRun.summary}`;
    }
    if (!lastRunAt.value || !lastRunResult.value)
        return "暂无";
    return `${formatDateTime(lastRunAt.value)}｜${lastRunResult.value}`;
});
function formatMoney(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value)))
        return "-";
    return `CNY ${Number(value).toFixed(2)}`;
}
function formatPct(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value)))
        return "-";
    return `${Number(value).toFixed(2)}%`;
}
function formatDateTime(value) {
    if (value === null || value === undefined || value === "")
        return "-";
    const date = typeof value === "number" ? new Date(value) : new Date(value);
    if (Number.isNaN(date.getTime()))
        return "-";
    return date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
    });
}
function formatCountdown(milliseconds) {
    const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
async function refreshRuntimeSchedule() {
    try {
        const payload = await fetchJson("/api/runtime/state?executor=profit_trade");
        if (payload.state) {
            const previousLastRunAt = dashboard.value.runtime?.lastRunAt;
            dashboard.value = {
                ...dashboard.value,
                runtime: {
                    ...dashboard.value.runtime,
                    ...payload.state,
                },
            };
            if (payload.state.lastRunAt
                && payload.state.lastRunAt !== previousLastRunAt) {
                void loadDashboard();
                window.dispatchEvent(new CustomEvent("profit-trade:refresh-observability"));
            }
        }
        apiOnline.value = true;
    }
    catch {
        apiOnline.value = false;
    }
}
function parseDateStart(value) {
    if (!value)
        return null;
    const date = new Date(`${value}T00:00:00`);
    return Number.isNaN(date.getTime()) ? null : date.getTime();
}
function parseDateEnd(value) {
    if (!value)
        return null;
    const date = new Date(`${value}T23:59:59.999`);
    return Number.isNaN(date.getTime()) ? null : date.getTime();
}
function completedTradePurchaseTimeMs(trade) {
    const raw = trade.steamBoughtAt;
    if (!raw)
        return null;
    const time = new Date(raw).getTime();
    return Number.isNaN(time) ? null : time;
}
function isoToBeijingInput(value) {
    if (!value)
        return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime()))
        return "";
    return new Date(date.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 19);
}
function beijingInputNow() {
    return new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString().slice(0, 19);
}
function beijingInputToIso(value) {
    return `${value}+08:00`;
}
function manualAccountInitials(name) {
    const normalized = String(name || "").trim();
    if (!normalized)
        return "--";
    const parts = normalized.split(/[^a-zA-Z0-9]+/).filter(Boolean);
    if (parts.length >= 2)
        return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
    return normalized.slice(0, 2).toUpperCase();
}
function resolveTradeSteamAccountId(trade) {
    const note = trade.note ?? {};
    const recordedId = String(note.steamAccountId ?? "").trim();
    const recordedSteamId = String(note.steamId ?? "").trim();
    const recordedName = String(note.steamAccountName ?? "").trim().toLowerCase();
    const matched = manualEntryAccounts.value.find((account) => ((recordedId && account.accountId === recordedId)
        || (recordedSteamId && account.steamId === recordedSteamId)
        || (recordedName && account.name.trim().toLowerCase() === recordedName)));
    return matched?.accountId ?? "";
}
function openCreateManualRecord() {
    const now = beijingInputNow();
    manualRecordEditingTradeId.value = null;
    manualRecordError.value = "";
    manualItemQuery.value = "";
    manualItemSuggestions.value = [];
    manualItemHasMore.value = false;
    manualItemNextOffset.value = 0;
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
function openEditManualRecord(trade) {
    if (trade.status !== "completed")
        return;
    manualRecordEditingTradeId.value = trade.id;
    manualRecordError.value = "";
    manualItemQuery.value = trade.name && trade.name !== trade.marketHashName
        ? `${trade.name} / ${trade.marketHashName}`
        : trade.marketHashName;
    manualItemSuggestions.value = [];
    manualItemHasMore.value = false;
    manualItemNextOffset.value = 0;
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
function closeManualRecord() {
    if (manualRecordSaving.value)
        return;
    manualRecordOpen.value = false;
    manualRecordError.value = "";
}
function mergeItemSuggestions(current, incoming) {
    const merged = new Map(current.map((item) => [item.marketHashName, item]));
    for (const item of incoming)
        merged.set(item.marketHashName, item);
    return [...merged.values()];
}
async function searchManualItems(append = false) {
    manualItemSearchBusy.value = true;
    const query = manualItemQuery.value;
    try {
        const page = await fetchProfitTradeItemSuggestions(query, append ? manualItemNextOffset.value : 0);
        if (query !== manualItemQuery.value)
            return;
        manualItemSuggestions.value = append
            ? mergeItemSuggestions(manualItemSuggestions.value, page.items)
            : page.items;
        manualItemHasMore.value = Boolean(page.pagination?.hasMore);
        manualItemNextOffset.value = Number(page.pagination?.nextOffset ?? 0);
        manualItemSearchOpen.value = true;
        apiOnline.value = true;
    }
    catch (error) {
        if (error instanceof TypeError)
            apiOnline.value = false;
        manualRecordError.value = `物品搜索失败：${error instanceof Error ? error.message : String(error)}`;
    }
    finally {
        manualItemSearchBusy.value = false;
    }
}
async function fetchProfitTradeItemSuggestions(query, offset = 0) {
    const payload = await fetchJson(`/api/profit-trade/items/search?query=${encodeURIComponent(query.trim())}&limit=50&offset=${offset}`);
    return { items: payload.items ?? [], pagination: payload.pagination };
}
async function searchProtectionItems(append = false) {
    protectionItemSearchBusy.value = true;
    protectionError.value = "";
    const query = protectionItemQuery.value;
    try {
        const page = await fetchProfitTradeItemSuggestions(query, append ? protectionItemNextOffset.value : 0);
        if (query !== protectionItemQuery.value)
            return;
        protectionItemSuggestions.value = append
            ? mergeItemSuggestions(protectionItemSuggestions.value, page.items)
            : page.items;
        protectionItemHasMore.value = Boolean(page.pagination?.hasMore);
        protectionItemNextOffset.value = Number(page.pagination?.nextOffset ?? 0);
        protectionItemSearchOpen.value = true;
        apiOnline.value = true;
    }
    catch (error) {
        if (error instanceof TypeError)
            apiOnline.value = false;
        protectionError.value = `物品搜索失败：${error instanceof Error ? error.message : String(error)}`;
    }
    finally {
        protectionItemSearchBusy.value = false;
    }
}
function onProtectionItemInput() {
    manualProtectedMarketHashName.value = "";
    protectionItemHasMore.value = false;
    protectionItemNextOffset.value = 0;
    protectionItemSearchOpen.value = true;
    if (protectionItemSearchTimer)
        clearTimeout(protectionItemSearchTimer);
    protectionItemSearchTimer = setTimeout(() => void searchProtectionItems(), 220);
}
function chooseProtectionItem(item) {
    manualProtectedMarketHashName.value = item.marketHashName;
    protectionItemQuery.value = item.name !== item.marketHashName
        ? `${item.name} / ${item.marketHashName}`
        : item.marketHashName;
    protectionItemSearchOpen.value = false;
    protectionError.value = "";
}
function openProtectionList() {
    protectionError.value = "";
    protectionItemQuery.value = "";
    manualProtectedMarketHashName.value = "";
    protectionItemSuggestions.value = [];
    protectionItemHasMore.value = false;
    protectionItemNextOffset.value = 0;
    protectionItemSearchOpen.value = false;
    manualProtectedSteamId.value = "";
    protectionListOpen.value = true;
}
function closeProtectionList() {
    protectionListOpen.value = false;
    protectionItemSearchOpen.value = false;
    protectionError.value = "";
}
function onManualItemInput() {
    manualRecordForm.value.marketHashName = "";
    manualRecordForm.value.name = "";
    manualItemHasMore.value = false;
    manualItemNextOffset.value = 0;
    manualItemSearchOpen.value = true;
    if (manualItemSearchTimer)
        clearTimeout(manualItemSearchTimer);
    manualItemSearchTimer = setTimeout(() => void searchManualItems(), 220);
}
function chooseManualItem(item) {
    manualRecordForm.value.marketHashName = item.marketHashName;
    manualRecordForm.value.name = item.name !== item.marketHashName ? item.name : "";
    manualItemQuery.value = item.name !== item.marketHashName
        ? `${item.name} / ${item.marketHashName}`
        : item.marketHashName;
    manualItemSearchOpen.value = false;
    manualRecordError.value = "";
}
async function saveManualRecord() {
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
        await fetchJson(editing
            ? "/api/profit-trade/manual-record/update"
            : "/api/profit-trade/manual-record/create", {
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
        });
        apiOnline.value = true;
        manualRecordOpen.value = false;
        message.value = editing ? "已保存人工修正" : "已新增手工流水";
        resetCompletedPage();
        await loadDashboard();
    }
    catch (error) {
        if (error instanceof TypeError)
            apiOnline.value = false;
        manualRecordError.value = `保存失败：${error instanceof Error ? error.message : String(error)}`;
    }
    finally {
        manualRecordSaving.value = false;
    }
}
function resetCompletedPage() {
    completedPage.value = 1;
}
async function loadCompletedTrades() {
    const params = new URLSearchParams();
    const from = parseDateStart(completedDateFrom.value);
    const to = parseDateEnd(completedDateTo.value);
    if (from !== null)
        params.set("boughtFrom", new Date(from).toISOString());
    if (to !== null)
        params.set("boughtTo", new Date(to).toISOString());
    const suffix = params.size ? `?${params.toString()}` : "";
    const payload = await fetchJson(`/api/profit-trade/completed${suffix}`);
    completedDataset.value = payload;
    if (from === null && to === null) {
        completedAllSummary.value = { ...payload.summary };
    }
    resetCompletedPage();
}
function localDateInputValue(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}
async function setCompletedDatePreset(days) {
    if (days === "all") {
        completedDateFrom.value = "";
        completedDateTo.value = "";
        await loadCompletedTrades();
        return;
    }
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - Math.max(0, days - 1));
    completedDateFrom.value = localDateInputValue(start);
    completedDateTo.value = localDateInputValue(end);
    await loadCompletedTrades();
}
function goCompletedPage(direction) {
    completedPage.value = Math.min(completedTotalPages.value, Math.max(1, completedCurrentPage.value + direction));
}
function noteText(trade, key) {
    const value = trade.note?.[key];
    if (value === null || value === undefined || value === "")
        return "-";
    return String(value);
}
function noteNumber(trade, key) {
    const value = Number(trade.note?.[key]);
    return Number.isFinite(value) ? value : null;
}
function balanceDiscountPct(trade) {
    if (trade.steamBalanceDiscount === null || trade.steamBalanceDiscount === undefined)
        return null;
    return Number(trade.steamBalanceDiscount) * 100;
}
function steamAccountLabel(trade) {
    const name = noteText(trade, "steamAccountName");
    const id = noteText(trade, "steamAccountId");
    if (name === "-" && id === "-")
        return "未记录";
    if (name === "-")
        return id;
    if (id === "-")
        return name;
    return `${name} / ${id}`;
}
function duplicateBuyText(trade) {
    const duplicate = trade.note?.extraDuplicateBuyDuringRepair;
    if (!duplicate || typeof duplicate !== "object")
        return "";
    const data = duplicate;
    const account = String(data.steamAccountName || "-");
    const assetId = String(data.assetId || "-");
    const price = Number(data.price);
    const priceText = Number.isFinite(price) ? ` / ${formatMoney(price)}` : "";
    return `${account} / ${assetId}${priceText}`;
}
function signedClass(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric === 0)
        return "neutral";
    return numeric > 0 ? "positive" : "negative";
}
function isStickerSlab(trade) {
    const hash = trade.marketHashName.toLowerCase();
    const name = String(trade.name || "");
    return hash.startsWith("sticker slab |") || name.includes("印花板");
}
function canManualSettle(trade) {
    return trade.status === "c5_listed" || (trade.status === "manual_required"
        && trade.stepIndex >= 4
        && Number(trade.steamBuyPrice) > 0);
}
function hasTrackedSteamBuyOrder(trade) {
    const buyOrderId = String(trade.note?.steamBuyOrderId ?? "").trim();
    return buyOrderId !== "" || Boolean(trade.note?.steamBuyUnverifiedAt);
}
function dismissActionLabel(trade) {
    return hasTrackedSteamBuyOrder(trade) ? "撤销求购并关闭" : "已知晓并隐藏";
}
function statusLabel(status) {
    const labels = {
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
function stepClass(step, trade) {
    if (trade.status === "failed" || trade.status === "manual_required") {
        return step.index <= trade.stepIndex ? "attention" : "pending";
    }
    if (step.index < trade.stepIndex)
        return "done";
    if (step.index === trade.stepIndex)
        return "current";
    return "pending";
}
class ApiRequestError extends Error {
    constructor(status, statusText, detail) {
        super(`${status} ${statusText}${detail ? ` ${detail}` : ""}`);
        this.name = "ApiRequestError";
        this.status = status;
    }
}
async function fetchJson(url, init) {
    const response = await fetch(url, init);
    if (!response.ok) {
        let detail = "";
        try {
            const payload = await response.json();
            detail = payload.error ?? "";
        }
        catch {
            detail = "";
        }
        throw new ApiRequestError(response.status, response.statusText, detail);
    }
    return response.json();
}
function reservedBalanceKey(account) {
    return String(account.steamId || account.accountId || "").trim();
}
function syncReservedBalanceDrafts() {
    const configured = dashboard.value.config.accountReservedBalances || {};
    reservedBalanceDrafts.value = Object.fromEntries(dashboard.value.manualEntryOptions.accounts.map((account) => {
        const key = reservedBalanceKey(account);
        const configuredValue = configured[key]
            ?? configured[account.accountId]
            ?? configured[account.name]
            ?? 0;
        return [account.accountId, String(configuredValue)];
    }));
}
async function loadDashboard() {
    loading.value = true;
    message.value = "";
    try {
        const payload = (await fetchJson("/api/profit-trade/dashboard"));
        let runtimeState = payload.runtime;
        try {
            const runtimePayload = await fetchJson("/api/runtime/state?executor=profit_trade");
            runtimeState = runtimePayload.state || runtimeState;
        }
        catch {
            // The dashboard remains usable while the shared runtime endpoint starts up.
        }
        dashboard.value = {
            ...payload,
            runtime: runtimeState,
            listingsCircuit: payload.listingsCircuit || { status: "closed", isBlocking: false },
        };
        await loadCompletedTrades();
        dailySteamBudgetDraft.value = String(dashboard.value.config.dailySteamBudget ?? 1000);
        syncReservedBalanceDrafts();
        apiOnline.value = true;
        window.dispatchEvent(new CustomEvent("profit-trade:dashboard-status", {
            detail: { allowRealExecution: dashboard.value.config.allowRealExecution },
        }));
    }
    catch {
        apiOnline.value = false;
        dashboard.value = fallbackDashboard;
        message.value = "API未连接：无法读取当前真实状态，页面不会使用静态运营数据替代。";
    }
    finally {
        loading.value = false;
    }
}
async function toggleEnabled() {
    const nextEnabled = runtimeConfirmEnabled.value ?? !autoRunEnabled.value;
    message.value = "";
    runtimeConfirmError.value = "";
    runtimeBusy.value = true;
    try {
        await fetchJson("/api/runtime/toggle", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ executor: "profit_trade", enabled: nextEnabled }),
        });
        apiOnline.value = true;
        runtimeConfirmEnabled.value = null;
        runtimeConfirmError.value = "";
        await loadDashboard();
        if (apiOnline.value) {
            message.value = nextEnabled ? "Profit Trade 后端 10 分钟循环已开启，正在执行 Cookie 门禁；门禁通过后立即运行第一轮。" : "Profit Trade 新机会与 10 分钟循环已停止；已有流水继续安全闭环。";
        }
    }
    catch (error) {
        apiOnline.value = error instanceof ApiRequestError;
        const detail = error instanceof Error ? error.message : String(error);
        runtimeConfirmError.value = `${nextEnabled ? "开启" : "关闭"}失败：${detail}`;
        message.value = runtimeConfirmError.value;
    }
    finally {
        runtimeBusy.value = false;
    }
}
async function runOnce() {
    if (profitCycleRunning.value) {
        message.value = "Profit Trade 本轮已经在执行，请等待完成后再启动下一轮。";
        return;
    }
    runOnceBusy.value = true;
    message.value = "";
    try {
        const previousLastRunAt = dashboard.value.runtime?.lastRunAt || null;
        const queued = await fetchJson("/api/profit-trade/run-once", { method: "POST" });
        if (!queued.alreadyRunning && (!queued.ok || !queued.queued)) {
            throw new Error("后端未能排入本轮任务");
        }
        apiOnline.value = true;
        const deadline = Date.now() + 30 * 60 * 1000;
        let consecutiveReadFailures = 0;
        while (Date.now() < deadline) {
            let runtimePayload;
            try {
                runtimePayload = await fetchJson("/api/runtime/state?executor=profit_trade");
                consecutiveReadFailures = 0;
            }
            catch (error) {
                consecutiveReadFailures += 1;
                if (consecutiveReadFailures >= 5)
                    throw error;
                await new Promise(resolve => window.setTimeout(resolve, 1000));
                continue;
            }
            const state = runtimePayload.state;
            if (state) {
                dashboard.value = {
                    ...dashboard.value,
                    runtime: { ...dashboard.value.runtime, ...state },
                };
                const completedNewRun = Boolean(state.lastRunAt
                    && state.lastRunAt !== previousLastRunAt
                    && !state.taskRunning);
                if (completedNewRun)
                    break;
            }
            await new Promise(resolve => window.setTimeout(resolve, 1000));
        }
        if (dashboard.value.runtime?.lastRunAt === previousLastRunAt
            || dashboard.value.runtime?.taskRunning) {
            throw new Error("后端执行超过30分钟，请到实时日志查看当前任务状态");
        }
        await loadDashboard();
        window.dispatchEvent(new CustomEvent("profit-trade:refresh-observability"));
        if (apiOnline.value) {
            const errorCount = Number(dashboard.value.lastRun?.errorCount || 0);
            message.value = errorCount > 0
                ? `Profit Trade 本轮已结束，但有 ${errorCount} 个错误：${dashboard.value.lastRun?.errors?.join("；") || "请查看实时日志"}`
                : "Profit Trade 本轮已完成，观察池和执行状态已经刷新；下一轮仍按10分钟计划执行。";
        }
    }
    catch (error) {
        apiOnline.value = error instanceof ApiRequestError;
        const detail = error instanceof Error ? error.message : String(error);
        message.value = `执行一轮失败：${detail}`;
    }
    finally {
        runOnceBusy.value = false;
    }
}
function openRuntimeConfirm() {
    runtimeConfirmError.value = "";
    runtimeConfirmEnabled.value = !autoRunEnabled.value;
}
function closeRuntimeConfirm() {
    if (runtimeBusy.value)
        return;
    runtimeConfirmEnabled.value = null;
    runtimeConfirmError.value = "";
}
const profitTradeConfigToggleLabels = {
    enabled: "Profit Trade 总功能",
    allowRealExecution: "普通真实执行",
    longBuyEnabled: "长期求购功能",
    longBuyAllowRealExecution: "长期求购 Steam 写入",
};
async function updateProfitTradeConfigToggle(key, nextEnabled) {
    if (configToggleBusy.value || runtimeBusy.value)
        return;
    const label = profitTradeConfigToggleLabels[key];
    message.value = "";
    configToggleBusy.value = key;
    const togglesRuntime = usesProfitTradeRuntimeToggle(key);
    if (togglesRuntime)
        runtimeBusy.value = true;
    try {
        if (togglesRuntime) {
            await fetchJson("/api/runtime/toggle", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ executor: "profit_trade", enabled: nextEnabled }),
            });
        }
        else {
            await fetchJson("/api/profit-trade/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ [key]: nextEnabled }),
            });
        }
        apiOnline.value = true;
        await loadDashboard();
        if (apiOnline.value)
            message.value = `${label}已${nextEnabled ? "开启" : "关闭"}`;
    }
    catch (error) {
        if (!(error instanceof ApiRequestError))
            apiOnline.value = false;
        const detail = error instanceof Error ? error.message : String(error);
        message.value = `${label}更新失败：${detail}`;
        if (key === "longBuyAllowRealExecution")
            longBuyWriteConfirmError.value = message.value;
    }
    finally {
        configToggleBusy.value = null;
        if (togglesRuntime)
            runtimeBusy.value = false;
    }
}
function requestLongBuyConfigToggle(key, nextEnabled) {
    if (configToggleBusy.value || runtimeBusy.value)
        return;
    if (requiresLongBuyConfigConfirmation(key)) {
        longBuyWriteConfirmError.value = "";
        longBuyWriteConfirm.value = nextEnabled;
        return;
    }
    void updateProfitTradeConfigToggle(key, nextEnabled);
}
function closeLongBuyWriteConfirm() {
    if (configToggleBusy.value)
        return;
    longBuyWriteConfirm.value = null;
    longBuyWriteConfirmError.value = "";
}
async function confirmLongBuyWriteToggle() {
    if (longBuyWriteConfirm.value === null)
        return;
    const nextEnabled = longBuyWriteConfirm.value;
    await updateProfitTradeConfigToggle("longBuyAllowRealExecution", nextEnabled);
    if (!configToggleBusy.value && !longBuyWriteConfirmError.value) {
        longBuyWriteConfirm.value = null;
    }
}
async function setItemTypeStatus(key, status) {
    message.value = "";
    try {
        await fetchJson("/api/profit-trade/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ [key]: status }),
        });
        apiOnline.value = true;
        await loadDashboard();
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `品类状态更新失败：${error instanceof Error ? error.message : String(error)}`;
    }
}
async function saveDailySteamBudget() {
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
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `每日余额上限保存失败：${error instanceof Error ? error.message : String(error)}`;
    }
}
async function saveAccountReservedBalances() {
    const nextBalances = {};
    for (const account of dashboard.value.manualEntryOptions.accounts) {
        const rawValue = reservedBalanceDrafts.value[account.accountId] ?? "0";
        const value = Number(rawValue);
        if (!Number.isFinite(value) || value < 0) {
            message.value = `${account.name} 的保留余额必须是大于等于 0 的数字`;
            return;
        }
        if (value > 0)
            nextBalances[reservedBalanceKey(account)] = value;
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
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `Steam 账号保留余额保存失败：${error instanceof Error ? error.message : String(error)}`;
    }
}
async function manualSettleTrade(trade) {
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
    }
    catch (error) {
        apiOnline.value = true;
        message.value = `${trade.tradeNo} 手动完结失败：${error instanceof Error ? error.message : String(error)}`;
    }
}
async function dismissTrade(trade) {
    message.value = "";
    try {
        const result = (await fetchJson("/api/profit-trade/dismiss", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                tradeId: trade.id,
                reason: "user acknowledged and hid this trade",
            }),
        }));
        apiOnline.value = true;
        message.value = result.dismissed === false
            ? `${trade.tradeNo} 未隐藏：${result.message ?? "Steam 求购已成交，流水已恢复"}`
            : `${trade.tradeNo} ${hasTrackedSteamBuyOrder(trade) ? "求购已确认撤销并关闭" : "已隐藏"}`;
        await loadDashboard();
    }
    catch (error) {
        apiOnline.value = true;
        message.value = `${trade.tradeNo} 安全关闭失败：${error instanceof Error ? error.message : String(error)}`;
        await loadDashboard();
    }
}
async function toggleStickerSlabStatus() {
    await setItemTypeStatus("stickerSlabStatus", stickerSlabActive.value ? "blocked" : "active");
}
async function toggleStickerStatus() {
    await setItemTypeStatus("stickerStatus", stickerActive.value ? "blocked" : "active");
}
async function sendDailyReport() {
    message.value = "";
    try {
        await fetchJson("/api/profit-trade/serverchan/daily-report", { method: "POST" });
        apiOnline.value = true;
        message.value = "ServerChan日报已发送";
    }
    catch (error) {
        apiOnline.value = false;
        message.value = "API未连接或ServerChan未配置，日报未发送";
    }
}
async function scanOpportunities() {
    message.value = "";
    loading.value = true;
    try {
        const payload = await fetchJson("/api/profit-trade/scan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ record: true, lock: false, limit: 20, scanMaxItems: dashboard.value.config.scanMaxItems }),
        });
        apiOnline.value = true;
        const created = payload.report?.createdTradeIds?.length ?? 0;
        const opportunities = payload.report?.opportunityCount ?? 0;
        message.value = `扫描完成：机会 ${opportunities} 个，写入候选 ${created} 笔`;
        await loadDashboard();
        window.dispatchEvent(new CustomEvent("profit-trade:refresh-observability"));
    }
    catch (error) {
        apiOnline.value = false;
        message.value = "API未连接或扫描失败";
    }
    finally {
        loading.value = false;
    }
}
async function refreshSales() {
    message.value = "";
    loading.value = true;
    try {
        const payload = await fetchJson("/api/profit-trade/refresh-sales", {
            method: "POST",
        });
        apiOnline.value = true;
        const settled = payload.settledTradeIds?.length ?? 0;
        const skipped = payload.skippedTradeIds?.length ?? 0;
        const errors = payload.errors?.length ?? 0;
        message.value = `C5状态刷新完成：结算 ${settled} 笔，跳过 ${skipped} 笔，错误 ${errors} 个`;
        await loadDashboard();
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `刷新C5状态失败：${error instanceof Error ? error.message : String(error)}`;
    }
    finally {
        loading.value = false;
    }
}
async function lockTrade(trade) {
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
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `${trade.tradeNo} 锁定失败`;
    }
}
async function buyTrade(trade) {
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
    }
    catch (error) {
        apiOnline.value = true;
        message.value = `${trade.tradeNo} 买入B失败：${error instanceof Error ? error.message : String(error)}`;
    }
}
async function listC5Trade(trade) {
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
    }
    catch (error) {
        apiOnline.value = true;
        message.value = `${trade.tradeNo} 上架C5失败：${error instanceof Error ? error.message : String(error)}`;
    }
}
async function updateProtection(action, kind, value) {
    const normalizedValue = String(value || "").trim();
    if (!normalizedValue) {
        message.value = "没有可保护的值";
        return false;
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
        return true;
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `保护名单更新失败：${error instanceof Error ? error.message : String(error)}`;
        return false;
    }
}
async function addManualProtectedAsset() {
    await updateProtection("add", "asset", manualProtectedAssetId.value);
    manualProtectedAssetId.value = "";
}
async function addManualProtectedMarketHashName() {
    if (!manualProtectedMarketHashName.value) {
        protectionError.value = "请先输入中文名或英文名，并从搜索结果中选择准确饰品。";
        return;
    }
    const added = await updateProtection("add", "marketHashName", manualProtectedMarketHashName.value);
    if (!added)
        return;
    manualProtectedMarketHashName.value = "";
    protectionItemQuery.value = "";
    protectionItemSuggestions.value = [];
    protectionItemSearchOpen.value = false;
}
async function addManualProtectedSteamId() {
    if (!manualProtectedSteamId.value) {
        protectionError.value = "请先选择一个当前已导入的 Steam 账号。";
        return;
    }
    const added = await updateProtection("add", "steamId", manualProtectedSteamId.value);
    if (!added)
        return;
    manualProtectedSteamId.value = "";
}
function handleSharedConfigChange() {
    void loadDashboard();
}
function saveLastRunState() {
    if (typeof window === "undefined")
        return;
    if (!lastRunAt.value || !lastRunResult.value) {
        window.localStorage.removeItem(lastRunStorageKey);
        return;
    }
    window.localStorage.setItem(lastRunStorageKey, JSON.stringify({
        at: lastRunAt.value,
        result: lastRunResult.value,
    }));
}
function restoreLastRun() {
    if (typeof window === "undefined")
        return;
    const raw = window.localStorage.getItem(lastRunStorageKey);
    if (!raw)
        return;
    try {
        const stored = JSON.parse(raw);
        const at = Number(stored.at);
        if (Number.isFinite(at) && stored.result) {
            lastRunAt.value = at;
            lastRunResult.value = String(stored.result);
        }
    }
    catch {
        window.localStorage.removeItem(lastRunStorageKey);
    }
}
onMounted(() => {
    restoreLastRun();
    void loadDashboard();
    countdownTimer = setInterval(() => {
        countdownNow.value = Date.now();
    }, 1000);
    runtimeScheduleTimer = setInterval(() => {
        void refreshRuntimeSchedule();
    }, 5000);
    window.addEventListener("profit-trade:config-changed", handleSharedConfigChange);
});
onUnmounted(() => {
    if (manualItemSearchTimer)
        clearTimeout(manualItemSearchTimer);
    if (protectionItemSearchTimer)
        clearTimeout(protectionItemSearchTimer);
    if (countdownTimer)
        clearInterval(countdownTimer);
    if (runtimeScheduleTimer)
        clearInterval(runtimeScheduleTimer);
    window.removeEventListener("profit-trade:config-changed", handleSharedConfigChange);
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['api-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['api-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['type-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['online']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['offline']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['formula-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['formula-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['formula-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['budget-form']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-account']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-account']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-account']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-account']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-account']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-input']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-input']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-input']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-summary-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-summary-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-summary-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-header']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-header']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-header']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kind-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kind-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kind-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kind-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kind-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-hint']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-trade-card']} */ ;
/** @type {__VLS_StyleScopedClasses['attention']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-head']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-settle-row']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-basis-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-track']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['done']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['current']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['attention']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['attention']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['positive']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['negative']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-summary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-filter-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-head']} */ ;
/** @type {__VLS_StyleScopedClasses['toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-badges']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-summary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['wide']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['api-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-basis-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['type-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['api-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['online']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['api-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['offline']} */ ;
/** @type {__VLS_StyleScopedClasses['status-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['attention']} */ ;
/** @type {__VLS_StyleScopedClasses['type-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['warning']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-summary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['danger']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['offline']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['online']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['observe']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['running']} */ ;
/** @type {__VLS_StyleScopedClasses['inline-status']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['budget-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-no']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-hash']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-filter-note']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['formula-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-head']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['formula-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['formula-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-settle-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-settle-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-trade-card']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-trade-card']} */ ;
/** @type {__VLS_StyleScopedClasses['attention']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-basis-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['dismiss-action']} */ ;
/** @type {__VLS_StyleScopedClasses['danger-button']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-track']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-track']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['done']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['current']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['done']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['current']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['attention']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['attention']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-error']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-error']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['positive']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['positive']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['negative']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['negative']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-stack']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-zone']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-filter-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-edit-button']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-header']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-header']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-picker']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-unrecorded']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['positive']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['negative']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cycle-running']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cycle-running']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cycle-running']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page profit-trade-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "page-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "toolbar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "api-pill" },
    ...{ class: ({ online: __VLS_ctx.apiOnline === true, offline: __VLS_ctx.apiOnline === false }) },
});
(__VLS_ctx.apiOnline === true ? "API已连接" : __VLS_ctx.apiOnline === false ? "API未连接" : "检查中");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.loadDashboard) },
    ...{ class: "secondary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.runOnce) },
    ...{ class: "secondary-button" },
    type: "button",
    disabled: (__VLS_ctx.profitCycleRunning),
});
(__VLS_ctx.profitCycleRunning ? "本轮执行中…" : "执行一轮");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.scanOpportunities) },
    ...{ class: "secondary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.refreshSales) },
    ...{ class: "secondary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.sendDailyReport) },
    ...{ class: "secondary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.openRuntimeConfirm) },
    ...{ class: "primary-button" },
    type: "button",
    disabled: (__VLS_ctx.runtimeBusy),
});
(__VLS_ctx.autoRunEnabled ? `关闭10分钟循环 ${__VLS_ctx.autoRunCountdown}` : "开启10分钟循环");
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "profit-summary-grid" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "profit-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.autoRunEnabled ? "已开启" : "已关闭");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "profit-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard.summary.activeCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "profit-metric" },
    ...{ class: ({ danger: __VLS_ctx.dashboard.summary.failedCount > 0 }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard.summary.failedCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "profit-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formatMoney(__VLS_ctx.dashboard.summary.realizedProfit));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "profit-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formatMoney(__VLS_ctx.dashboard.summary.expectedProfit));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "profit-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formatPct(__VLS_ctx.dashboard.config.minRoiPct));
(__VLS_ctx.formatMoney(__VLS_ctx.dashboard.config.minItemValue));
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "profit-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formatMoney(__VLS_ctx.dashboard.summary.dailySteamSpent));
(__VLS_ctx.formatMoney(__VLS_ctx.dashboard.summary.dailySteamRemaining));
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "execution-status-grid" },
    'aria-label': "执行状态",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "execution-status-item" },
    ...{ class: ({ online: __VLS_ctx.apiOnline === true, offline: __VLS_ctx.apiOnline === false }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.apiStatusLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "execution-status-item" },
    ...{ class: ({ online: __VLS_ctx.dashboard.config.allowRealExecution, offline: !__VLS_ctx.dashboard.config.allowRealExecution }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.realExecutionLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "execution-status-item" },
    ...{ class: ({
            online: __VLS_ctx.longBuyRemoteWritesEnabled,
            observe: __VLS_ctx.longBuyObservationMode,
            offline: !__VLS_ctx.dashboard.config.longBuyEnabled,
        }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.longBuyExecutionLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
(__VLS_ctx.longBuyExecutionDetail);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "execution-status-item" },
    ...{ class: ({ online: __VLS_ctx.autoRunEnabled }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.autoRunStatusLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "execution-status-item" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.nextAutoRunLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "execution-status-item wide" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.lastRunLabel);
/** @type {[typeof ProfitTradeLongBuyStrategyPanel, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(ProfitTradeLongBuyStrategyPanel, new ProfitTradeLongBuyStrategyPanel({
    ...{ 'onToggle': {} },
    config: (__VLS_ctx.dashboard.config),
    activeOrderCount: (__VLS_ctx.dashboard.summary.longBuyActiveOrders ?? 0),
    updatingKey: (__VLS_ctx.configToggleBusy ?? (__VLS_ctx.runtimeBusy ? 'enabled' : null)),
}));
const __VLS_1 = __VLS_0({
    ...{ 'onToggle': {} },
    config: (__VLS_ctx.dashboard.config),
    activeOrderCount: (__VLS_ctx.dashboard.summary.longBuyActiveOrders ?? 0),
    updatingKey: (__VLS_ctx.configToggleBusy ?? (__VLS_ctx.runtimeBusy ? 'enabled' : null)),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
let __VLS_3;
let __VLS_4;
let __VLS_5;
const __VLS_6 = {
    onToggle: (__VLS_ctx.requestLongBuyConfigToggle)
};
var __VLS_2;
if (__VLS_ctx.profitCycleRunning) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "profit-cycle-running" },
        role: "status",
        'aria-live': "polite",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "cycle-spinner" },
        'aria-hidden': "true",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
}
if (__VLS_ctx.runtimeConfirmEnabled !== null) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (__VLS_ctx.closeRuntimeConfirm) },
        ...{ class: "runtime-confirm-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "runtime-confirm-dialog" },
        role: "dialog",
        'aria-modal': "true",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_7 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (__VLS_ctx.runtimeConfirmEnabled ? 'shield' : 'warning'),
        size: (22),
    }));
    const __VLS_8 = __VLS_7({
        name: (__VLS_ctx.runtimeConfirmEnabled ? 'shield' : 'warning'),
        size: (22),
    }, ...__VLS_functionalComponentArgsRest(__VLS_7));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.runtimeConfirmEnabled ? "开启 Profit Trade 10分钟循环" : "关闭 Profit Trade 10分钟循环");
    if (__VLS_ctx.runtimeConfirmEnabled && __VLS_ctx.dashboard.runtime?.migrationHold) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    else if (__VLS_ctx.runtimeConfirmEnabled) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    if (__VLS_ctx.runtimeConfirmError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "runtime-confirm-error" },
            role: "alert",
        });
        (__VLS_ctx.runtimeConfirmError);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.closeRuntimeConfirm) },
        ...{ class: "secondary-button" },
        type: "button",
        disabled: (__VLS_ctx.runtimeBusy),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.toggleEnabled) },
        ...{ class: "primary-button" },
        type: "button",
        disabled: (__VLS_ctx.runtimeBusy),
    });
    (__VLS_ctx.runtimeBusy ? "提交中…" : "确认");
}
if (__VLS_ctx.longBuyWriteConfirm !== null) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (__VLS_ctx.closeLongBuyWriteConfirm) },
        ...{ class: "runtime-confirm-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "runtime-confirm-dialog" },
        role: "dialog",
        'aria-modal': "true",
        'aria-labelledby': "long-buy-write-confirm-title",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_10 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: (__VLS_ctx.longBuyWriteConfirm ? 'shield' : 'warning'),
        size: (22),
    }));
    const __VLS_11 = __VLS_10({
        name: (__VLS_ctx.longBuyWriteConfirm ? 'shield' : 'warning'),
        size: (22),
    }, ...__VLS_functionalComponentArgsRest(__VLS_10));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
        id: "long-buy-write-confirm-title",
    });
    (__VLS_ctx.longBuyWriteConfirm ? "开启长期 Steam 写入" : "关闭长期 Steam 写入");
    if (__VLS_ctx.longBuyWriteConfirm) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    if (__VLS_ctx.longBuyWriteConfirmError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "runtime-confirm-error" },
            role: "alert",
        });
        (__VLS_ctx.longBuyWriteConfirmError);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.closeLongBuyWriteConfirm) },
        ...{ class: "secondary-button" },
        type: "button",
        disabled: (Boolean(__VLS_ctx.configToggleBusy)),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.confirmLongBuyWriteToggle) },
        ...{ class: "primary-button" },
        type: "button",
        disabled: (Boolean(__VLS_ctx.configToggleBusy)),
    });
    (__VLS_ctx.configToggleBusy ? "提交中…" : "确认");
}
if (__VLS_ctx.protectionListOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (__VLS_ctx.closeProtectionList) },
        ...{ class: "protection-modal-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "protection-modal" },
        role: "dialog",
        'aria-modal': "true",
        'aria-labelledby': "protection-list-title",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "protection-modal-header" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
        id: "protection-list-title",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.closeProtectionList) },
        ...{ class: "modal-close-button" },
        type: "button",
        'aria-label': "关闭保护列表",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "protection-editor-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
        ...{ onSubmit: (__VLS_ctx.addManualProtectedAsset) },
        ...{ class: "protection-form" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        for: "protected-asset-id",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "protection-input-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        id: "protected-asset-id",
        value: (__VLS_ctx.manualProtectedAssetId),
        type: "text",
        autocomplete: "off",
        placeholder: "assetId",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ class: "mini-action protect-action" },
        type: "submit",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
        ...{ onSubmit: (__VLS_ctx.addManualProtectedMarketHashName) },
        ...{ class: "protection-form" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        for: "protected-market-hash-name",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "protection-input-row protection-search-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "manual-item-search" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_13 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "scan",
        size: (17),
    }));
    const __VLS_14 = __VLS_13({
        name: "scan",
        size: (17),
    }, ...__VLS_functionalComponentArgsRest(__VLS_13));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.onProtectionItemInput) },
        ...{ onFocus: (...[$event]) => {
                if (!(__VLS_ctx.protectionListOpen))
                    return;
                __VLS_ctx.searchProtectionItems(false);
            } },
        id: "protected-market-hash-name",
        value: (__VLS_ctx.protectionItemQuery),
        type: "text",
        autocomplete: "off",
        placeholder: "输入中文名或英文名搜索",
    });
    if (__VLS_ctx.protectionItemSearchBusy) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    }
    if (__VLS_ctx.protectionItemSearchOpen) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "manual-item-suggestions" },
        });
        for (const [item] of __VLS_getVForSourceType((__VLS_ctx.protectionItemSuggestions))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.protectionListOpen))
                            return;
                        if (!(__VLS_ctx.protectionItemSearchOpen))
                            return;
                        __VLS_ctx.chooseProtectionItem(item);
                    } },
                key: (item.marketHashName),
                type: "button",
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (item.name);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (item.marketHashName);
        }
        if (__VLS_ctx.protectionItemHasMore) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.protectionListOpen))
                            return;
                        if (!(__VLS_ctx.protectionItemSearchOpen))
                            return;
                        if (!(__VLS_ctx.protectionItemHasMore))
                            return;
                        __VLS_ctx.searchProtectionItems(true);
                    } },
                ...{ class: "catalog-load-more" },
                type: "button",
                disabled: (__VLS_ctx.protectionItemSearchBusy),
            });
            (__VLS_ctx.protectionItemSearchBusy ? "加载中…" : "加载更多结果");
        }
        if (!__VLS_ctx.protectionItemSearchBusy && __VLS_ctx.protectionItemSuggestions.length === 0) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ class: "mini-action protect-action" },
        type: "submit",
        disabled: (!__VLS_ctx.manualProtectedMarketHashName),
    });
    if (__VLS_ctx.manualProtectedMarketHashName) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
            ...{ class: "protection-selected-value" },
        });
        (__VLS_ctx.manualProtectedMarketHashName);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
        ...{ onSubmit: (__VLS_ctx.addManualProtectedSteamId) },
        ...{ class: "protection-form" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        for: "protected-steam-id",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "protection-input-row" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        id: "protected-steam-id",
        value: (__VLS_ctx.manualProtectedSteamId),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        value: "",
    });
    for (const [account] of __VLS_getVForSourceType((__VLS_ctx.manualEntryAccounts))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
            key: (account.accountId),
            value: (account.steamId || ''),
            disabled: (!account.steamId),
        });
        (account.name);
        (account.steamId || "未配置SteamID");
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ class: "mini-action protect-action" },
        type: "submit",
        disabled: (!__VLS_ctx.manualProtectedSteamId),
    });
    if (__VLS_ctx.protectionError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "protection-error" },
            role: "alert",
        });
        (__VLS_ctx.protectionError);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "protection-modal-groups" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "protection-modal-group protected-kinds" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.protectedNameCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "protected-kind-list" },
    });
    for (const [item] of __VLS_getVForSourceType((__VLS_ctx.protectedNamePreview))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.protectionListOpen))
                        return;
                    __VLS_ctx.updateProtection('remove', 'marketHashName', item.marketHashName);
                } },
            key: (item.marketHashName),
            ...{ class: "protected-kind-row" },
            type: "button",
            title: "点击移出保护",
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (item.name);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (item.marketHashName);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    }
    if (__VLS_ctx.protectedNameCount === 0) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
            ...{ class: "protection-empty" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "protection-modal-group" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.protectedAssetCount);
    for (const [assetId] of __VLS_getVForSourceType((__VLS_ctx.protectedAssetPreview))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.protectionListOpen))
                        return;
                    __VLS_ctx.updateProtection('remove', 'asset', assetId);
                } },
            key: (assetId),
            ...{ class: "protection-chip" },
            type: "button",
        });
        (assetId);
    }
    if (__VLS_ctx.protectedAssetCount === 0) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
            ...{ class: "protection-empty" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "protection-modal-group" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.protectedSteamCount);
    for (const [account] of __VLS_getVForSourceType((__VLS_ctx.protectedSteamPreview))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.protectionListOpen))
                        return;
                    __VLS_ctx.updateProtection('remove', 'steamId', account.steamId);
                } },
            key: (account.steamId),
            ...{ class: "protected-kind-row" },
            type: "button",
            title: "点击移出保护",
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (account.name);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (account.steamId);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    }
    if (__VLS_ctx.protectedSteamCount === 0) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
            ...{ class: "protection-empty" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "protection-modal-hint" },
    });
}
if (__VLS_ctx.message) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "inline-status" },
    });
    (__VLS_ctx.message);
}
/** @type {[typeof ProfitTradeRoiWatch, ]} */ ;
// @ts-ignore
const __VLS_16 = __VLS_asFunctionalComponent(ProfitTradeRoiWatch, new ProfitTradeRoiWatch({
    running: (__VLS_ctx.profitCycleRunning),
    executorEnabled: (__VLS_ctx.autoRunEnabled),
    allowRealExecution: (__VLS_ctx.dashboard.config.allowRealExecution),
}));
const __VLS_17 = __VLS_16({
    running: (__VLS_ctx.profitCycleRunning),
    executorEnabled: (__VLS_ctx.autoRunEnabled),
    allowRealExecution: (__VLS_ctx.dashboard.config.allowRealExecution),
}, ...__VLS_functionalComponentArgsRest(__VLS_16));
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "profit-layout" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "panel profit-settings" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "panel-title-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "soft-label" },
});
(__VLS_ctx.dashboard.generatedAt || "-");
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
    ...{ class: "settings-list" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard.config.allowRealExecution ? "允许" : "禁止");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard.config.maxBuyPerCycle);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.formatPct(__VLS_ctx.dashboard.config.balanceDiscountPct));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.formatMoney(__VLS_ctx.dashboard.config.dailySteamBudget));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard.config.reservationSeconds);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard.config.requireC5MarketDepth ? "必须通过" : "关闭");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard.config.c5MinOnSaleCount ?? 3);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard.config.requireC5RecentSales ? "必须通过" : "暂不启用");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.formatPct(__VLS_ctx.dashboard.config.manualReviewRoiPct ?? 20));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.formatPct(__VLS_ctx.dashboard.config.c5MaxListingPremiumPct ?? 3));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.protectedAssetCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.protectedNameCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.protectedSteamCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.dashboard.config.aiAudit.enabled ? __VLS_ctx.dashboard.config.aiAudit.provider : "未启用");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "formula-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.formatPct(__VLS_ctx.dashboard.config.balanceDiscountPct));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
(__VLS_ctx.formatPct(__VLS_ctx.dashboard.config.minRoiPct));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "type-status-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.toggleStickerSlabStatus) },
    ...{ class: "status-toggle" },
    ...{ class: ({ active: __VLS_ctx.stickerSlabActive }) },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.stickerSlabActive ? "参与扫描" : "已屏蔽");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.toggleStickerStatus) },
    ...{ class: "status-toggle" },
    ...{ class: ({ active: __VLS_ctx.stickerActive }) },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.stickerActive ? "参与扫描" : "已屏蔽");
__VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
    ...{ onSubmit: (__VLS_ctx.saveDailySteamBudget) },
    ...{ class: "budget-form" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    for: "daily-steam-budget",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "protection-input-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    id: "daily-steam-budget",
    type: "number",
    min: "0",
    step: "1",
    autocomplete: "off",
});
(__VLS_ctx.dailySteamBudgetDraft);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "mini-action protect-action" },
    type: "submit",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
    ...{ onSubmit: (__VLS_ctx.saveAccountReservedBalances) },
    ...{ class: "wallet-reserve-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "wallet-reserve-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "mini-action protect-action" },
    type: "submit",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "wallet-reserve-list" },
});
for (const [account] of __VLS_getVForSourceType((__VLS_ctx.dashboard.manualEntryOptions.accounts))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        key: (account.accountId),
        ...{ class: "wallet-reserve-account" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (account.name);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (account.steamId || account.accountId);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "wallet-reserve-input" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        type: "number",
        min: "0",
        step: "0.01",
        inputmode: "decimal",
        autocomplete: "off",
        'aria-label': "Profit Trade 保留余额",
    });
    (__VLS_ctx.reservedBalanceDrafts[account.accountId]);
}
if (!__VLS_ctx.dashboard.manualEntryOptions.accounts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "wallet-reserve-empty" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "protection-panel protection-summary-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
(__VLS_ctx.protectedNameCount);
(__VLS_ctx.protectedAssetCount);
(__VLS_ctx.protectedSteamCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.openProtectionList) },
    ...{ class: "secondary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "trade-stack" },
});
if (__VLS_ctx.loading) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel empty-state" },
    });
}
else if (__VLS_ctx.activeTrades.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel empty-state" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    if (__VLS_ctx.listingsCooling) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    }
    if (__VLS_ctx.dashboard.config.allowRealExecution) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    }
}
else {
    for (const [trade] of __VLS_getVForSourceType((__VLS_ctx.activeTrades))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
            key: (trade.id),
            ...{ class: "profit-trade-card" },
            ...{ class: ({ attention: trade.requiresManualAction }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "trade-head" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "trade-no" },
        });
        (trade.tradeNo);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
        (trade.name || trade.marketHashName);
        if (trade.name && trade.name !== trade.marketHashName) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                ...{ class: "trade-hash" },
            });
            (trade.marketHashName);
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "trade-badges" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "status-badge" },
            ...{ class: ({ attention: trade.requiresManualAction }) },
        });
        (__VLS_ctx.statusLabel(trade.status));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "roi-badge" },
        });
        (__VLS_ctx.formatPct(trade.realizedRoiPct ?? trade.expectedRoiPct));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "roi-basis-badge" },
        });
        (__VLS_ctx.formatPct(__VLS_ctx.balanceDiscountPct(trade)));
        if (__VLS_ctx.isStickerSlab(trade)) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "type-badge warning" },
            });
        }
        if (__VLS_ctx.noteText(trade, 'liquidityStatus') !== '-') {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "type-badge" },
            });
            (__VLS_ctx.noteText(trade, "liquidityStatus"));
        }
        if (trade.requiresManualAction) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(__VLS_ctx.loading))
                            return;
                        if (!!(__VLS_ctx.activeTrades.length === 0))
                            return;
                        if (!(trade.requiresManualAction))
                            return;
                        __VLS_ctx.dismissTrade(trade);
                    } },
                ...{ class: "mini-action dismiss-action" },
                type: "button",
            });
            (__VLS_ctx.dismissActionLabel(trade));
        }
        if (trade.aAssetId) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(__VLS_ctx.loading))
                            return;
                        if (!!(__VLS_ctx.activeTrades.length === 0))
                            return;
                        if (!(trade.aAssetId))
                            return;
                        __VLS_ctx.updateProtection('add', 'asset', trade.aAssetId);
                    } },
                ...{ class: "mini-action protect-action" },
                type: "button",
            });
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.loading))
                        return;
                    if (!!(__VLS_ctx.activeTrades.length === 0))
                        return;
                    __VLS_ctx.updateProtection('add', 'marketHashName', trade.marketHashName);
                } },
            ...{ class: "mini-action protect-action" },
            type: "button",
        });
        if (trade.status === 'candidate' || trade.status === 'audited') {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(__VLS_ctx.loading))
                            return;
                        if (!!(__VLS_ctx.activeTrades.length === 0))
                            return;
                        if (!(trade.status === 'candidate' || trade.status === 'audited'))
                            return;
                        __VLS_ctx.lockTrade(trade);
                    } },
                ...{ class: "mini-action" },
                type: "button",
            });
        }
        if (trade.status === 'locked') {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(__VLS_ctx.loading))
                            return;
                        if (!!(__VLS_ctx.activeTrades.length === 0))
                            return;
                        if (!(trade.status === 'locked'))
                            return;
                        __VLS_ctx.buyTrade(trade);
                    } },
                ...{ class: "mini-action" },
                type: "button",
            });
        }
        if (trade.status === 'steam_bought') {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(__VLS_ctx.loading))
                            return;
                        if (!!(__VLS_ctx.activeTrades.length === 0))
                            return;
                        if (!(trade.status === 'steam_bought'))
                            return;
                        __VLS_ctx.listC5Trade(trade);
                    } },
                ...{ class: "mini-action" },
                type: "button",
            });
        }
        if (__VLS_ctx.canManualSettle(trade)) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "manual-settle-row" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
                type: "number",
                min: "0",
                step: "0.01",
                placeholder: "最终到手",
            });
            (__VLS_ctx.manualSettleInputs[trade.id]);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!!(__VLS_ctx.loading))
                            return;
                        if (!!(__VLS_ctx.activeTrades.length === 0))
                            return;
                        if (!(__VLS_ctx.canManualSettle(trade)))
                            return;
                        __VLS_ctx.manualSettleTrade(trade);
                    } },
                ...{ class: "mini-action" },
                type: "button",
            });
        }
        if (__VLS_ctx.listingsCooling && trade.stepIndex <= 2) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                ...{ class: "trade-circuit-note" },
            });
            (__VLS_ctx.formatDateTime(__VLS_ctx.dashboard.listingsCircuit.cooldownUntil));
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "progress-track" },
            'aria-hidden': "true",
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ style: ({ width: `${trade.progressPct}%` }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.ol, __VLS_intrinsicElements.ol)({
            ...{ class: "step-row" },
        });
        for (const [step] of __VLS_getVForSourceType((__VLS_ctx.dashboard.steps))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.li, __VLS_intrinsicElements.li)({
                key: (step.key),
                ...{ class: (__VLS_ctx.stepClass(step, trade)) },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (step.index + 1);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
            (step.label);
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "trade-detail-grid" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (trade.aAssetId || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (trade.aSteamId || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (trade.c5ProductId || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.steamBuyPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.steamAccountLabel(trade));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.noteNumber(trade, "walletBalanceBefore")));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (trade.bAssetId || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (trade.steamListingId || "-");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.steamRealCost));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatPct(__VLS_ctx.balanceDiscountPct(trade)));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.c5ExpectedNetPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.c5SoldNetPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.noteNumber(trade, "c5CurrentSellPrice")));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.expectedProfit));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.realizedProfit));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.noteText(trade, "c5OnSaleCount"));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.noteText(trade, "liquidityStatus"));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (trade.updatedAt);
        if (__VLS_ctx.noteText(trade, 'manualReviewReason') !== '-') {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                ...{ class: "trade-warning" },
            });
            (__VLS_ctx.noteText(trade, "manualReviewReason"));
        }
        if (trade.error) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                ...{ class: "trade-error" },
            });
            (trade.error);
        }
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "completed-zone" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "panel-title-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "soft-label" },
});
(__VLS_ctx.completedFilteredTrades.length);
(__VLS_ctx.completedAllSummary.count);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.openCreateManualRecord) },
    ...{ class: "primary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "completed-filter-note" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "completed-toolbar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    ...{ onChange: (__VLS_ctx.loadCompletedTrades) },
    type: "date",
});
(__VLS_ctx.completedDateFrom);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    ...{ onChange: (__VLS_ctx.loadCompletedTrades) },
    type: "date",
});
(__VLS_ctx.completedDateTo);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "completed-filter-actions" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setCompletedDatePreset(1);
        } },
    ...{ class: "mini-action" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setCompletedDatePreset(7);
        } },
    ...{ class: "mini-action" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.setCompletedDatePreset('all');
        } },
    ...{ class: "mini-action" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
    ...{ class: "completed-profit-summary" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
(__VLS_ctx.completedProfitSummaryLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({
    ...{ class: (__VLS_ctx.signedClass(__VLS_ctx.completedFilteredProfit)) },
});
(__VLS_ctx.formatMoney(__VLS_ctx.completedFilteredProfit));
if (__VLS_ctx.completedHasDateFilter) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.completedTotalProfit));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
(__VLS_ctx.completedSteamBuySummaryLabel);
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.formatMoney(__VLS_ctx.completedFilteredSteamBuy));
if (__VLS_ctx.completedHasDateFilter) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.completedTotalSteamBuy));
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "completed-pagination" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.goCompletedPage(-1);
        } },
    ...{ class: "mini-action" },
    type: "button",
    disabled: (__VLS_ctx.completedCurrentPage <= 1),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.completedPageRangeLabel);
(__VLS_ctx.completedCurrentPage);
(__VLS_ctx.completedTotalPages);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.goCompletedPage(1);
        } },
    ...{ class: "mini-action" },
    type: "button",
    disabled: (__VLS_ctx.completedCurrentPage >= __VLS_ctx.completedTotalPages),
});
if (__VLS_ctx.completedTrades.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel empty-state" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
}
else if (__VLS_ctx.completedFilteredTrades.length === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel empty-state" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
}
else {
    for (const [trade] of __VLS_getVForSourceType((__VLS_ctx.completedPagedTrades))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
            key: (`completed-${trade.id}`),
            ...{ class: "completed-trade-row" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!!(__VLS_ctx.completedTrades.length === 0))
                        return;
                    if (!!(__VLS_ctx.completedFilteredTrades.length === 0))
                        return;
                    __VLS_ctx.openEditManualRecord(trade);
                } },
            ...{ class: "completed-edit-button" },
            type: "button",
            title: "编辑本地记录",
            'aria-label': "编辑本地记录",
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_19 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "edit",
            size: (16),
        }));
        const __VLS_20 = __VLS_19({
            name: "edit",
            size: (16),
        }, ...__VLS_functionalComponentArgsRest(__VLS_19));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "completed-main" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "completed-card-head" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (trade.name || trade.marketHashName);
        if (trade.recordOrigin === 'manual_backfill') {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "manual-record-badge" },
            });
        }
        else if (trade.manuallyEdited) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "manual-record-badge corrected" },
            });
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (trade.tradeNo);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.steamAccountLabel(trade));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.formatDateTime(trade.steamBoughtAt));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.formatDateTime(trade.completedAt || trade.updatedAt));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.formatMoney(__VLS_ctx.noteNumber(trade, "walletBalanceBefore")));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (trade.bAssetId || "-");
        if (__VLS_ctx.duplicateBuyText(trade)) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.duplicateBuyText(trade));
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
            ...{ class: "completed-metrics" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.steamBuyPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.steamRealCost));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatMoney(trade.c5SoldNetPrice));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
        (__VLS_ctx.formatPct(__VLS_ctx.balanceDiscountPct(trade)));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({
            ...{ class: (__VLS_ctx.signedClass(trade.realizedRoiPct)) },
        });
        (__VLS_ctx.formatPct(trade.realizedRoiPct));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({
            ...{ class: (__VLS_ctx.signedClass(trade.realizedProfit)) },
        });
        (__VLS_ctx.formatMoney(trade.realizedProfit));
    }
}
if (__VLS_ctx.manualRecordOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (__VLS_ctx.closeManualRecord) },
        ...{ class: "manual-record-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "manual-record-modal" },
        role: "dialog",
        'aria-modal': "true",
        'aria-labelledby': "manual-record-title",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
        ...{ class: "manual-record-header" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.manualRecordEditingTradeId === null ? "历史补录" : "本地修正");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({
        id: "manual-record-title",
    });
    (__VLS_ctx.manualRecordEditingTradeId === null ? "新增手工流水" : "编辑已完结流水");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.closeManualRecord) },
        ...{ class: "mini-action" },
        type: "button",
        disabled: (__VLS_ctx.manualRecordSaving),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "manual-record-notice" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.form, __VLS_intrinsicElements.form)({
        ...{ onSubmit: (__VLS_ctx.saveManualRecord) },
        ...{ class: "manual-record-form" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "wide-field manual-item-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "manual-item-search" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_22 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "scan",
        size: (17),
    }));
    const __VLS_23 = __VLS_22({
        name: "scan",
        size: (17),
    }, ...__VLS_functionalComponentArgsRest(__VLS_22));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        ...{ onInput: (__VLS_ctx.onManualItemInput) },
        ...{ onFocus: (...[$event]) => {
                if (!(__VLS_ctx.manualRecordOpen))
                    return;
                __VLS_ctx.searchManualItems(false);
            } },
        autocomplete: "off",
        placeholder: "输入中文名或英文名，例如：次时代、M4A4",
    });
    (__VLS_ctx.manualItemQuery);
    if (__VLS_ctx.manualItemSearchBusy) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    }
    if (__VLS_ctx.manualItemSearchOpen) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "manual-item-suggestions" },
        });
        for (const [item] of __VLS_getVForSourceType((__VLS_ctx.manualItemSuggestions))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.manualRecordOpen))
                            return;
                        if (!(__VLS_ctx.manualItemSearchOpen))
                            return;
                        __VLS_ctx.chooseManualItem(item);
                    } },
                key: (item.marketHashName),
                type: "button",
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (item.name);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (item.marketHashName);
        }
        if (__VLS_ctx.manualItemHasMore) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
                ...{ onClick: (...[$event]) => {
                        if (!(__VLS_ctx.manualRecordOpen))
                            return;
                        if (!(__VLS_ctx.manualItemSearchOpen))
                            return;
                        if (!(__VLS_ctx.manualItemHasMore))
                            return;
                        __VLS_ctx.searchManualItems(true);
                    } },
                ...{ class: "catalog-load-more" },
                type: "button",
                disabled: (__VLS_ctx.manualItemSearchBusy),
            });
            (__VLS_ctx.manualItemSearchBusy ? "加载中…" : "加载更多结果");
        }
        if (!__VLS_ctx.manualItemSearchBusy && __VLS_ctx.manualItemSuggestions.length === 0) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        }
    }
    if (__VLS_ctx.manualRecordForm.marketHashName) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
            ...{ class: "manual-item-selected" },
        });
        (__VLS_ctx.manualRecordForm.marketHashName);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.fieldset, __VLS_intrinsicElements.fieldset)({
        ...{ class: "manual-account-picker wide-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.legend, __VLS_intrinsicElements.legend)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "manual-account-heading" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.manualRecordOpen))
                    return;
                __VLS_ctx.manualRecordForm.steamAccountId = '';
            } },
        ...{ class: "manual-account-unrecorded" },
        ...{ class: ({ selected: __VLS_ctx.manualRecordForm.steamAccountId === '' }) },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "manual-account-cards" },
    });
    for (const [account] of __VLS_getVForSourceType((__VLS_ctx.manualEntryAccounts))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.manualRecordOpen))
                        return;
                    __VLS_ctx.manualRecordForm.steamAccountId = account.accountId;
                } },
            key: (account.accountId),
            type: "button",
            ...{ class: ({ selected: __VLS_ctx.manualRecordForm.steamAccountId === account.accountId }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
        (__VLS_ctx.manualAccountInitials(account.name));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (account.name);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (account.steamId || "SteamID 未记录");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (__VLS_ctx.manualRecordForm.steamAccountId === account.accountId ? "已选择" : "选择");
    }
    if (__VLS_ctx.manualEntryAccounts.length === 0) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        type: "number",
        min: "0.01",
        step: "0.01",
        required: true,
    });
    (__VLS_ctx.manualRecordForm.steamBuyPrice);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        type: "number",
        min: "0.0001",
        max: "1",
        step: "0.0001",
        required: true,
    });
    (__VLS_ctx.manualRecordForm.balanceDiscount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        type: "number",
        min: "0.01",
        step: "0.01",
        required: true,
    });
    (__VLS_ctx.manualRecordForm.c5SoldNetPrice);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        type: "datetime-local",
        step: "1",
        required: true,
    });
    (__VLS_ctx.manualRecordForm.steamBoughtAt);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        type: "datetime-local",
        step: "1",
        required: true,
    });
    (__VLS_ctx.manualRecordForm.completedAt);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        autocomplete: "off",
    });
    (__VLS_ctx.manualRecordForm.aAssetId);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
        autocomplete: "off",
    });
    (__VLS_ctx.manualRecordForm.bAssetId);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "wide-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.textarea)({
        value: (__VLS_ctx.manualRecordForm.memo),
        rows: "3",
        maxlength: "1000",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
        ...{ class: "manual-record-preview wide-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.formatMoney(__VLS_ctx.manualRecordPreview.steamRealCost));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({
        ...{ class: (__VLS_ctx.signedClass(__VLS_ctx.manualRecordPreview.realizedProfit)) },
    });
    (__VLS_ctx.formatMoney(__VLS_ctx.manualRecordPreview.realizedProfit));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({
        ...{ class: (__VLS_ctx.signedClass(__VLS_ctx.manualRecordPreview.realizedRoiPct)) },
    });
    (__VLS_ctx.formatPct(__VLS_ctx.manualRecordPreview.realizedRoiPct));
    if (__VLS_ctx.manualRecordError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "manual-record-error wide-field" },
        });
        (__VLS_ctx.manualRecordError);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "manual-record-actions wide-field" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.closeManualRecord) },
        ...{ class: "secondary-button" },
        type: "button",
        disabled: (__VLS_ctx.manualRecordSaving),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ class: "primary-button" },
        type: "submit",
        disabled: (__VLS_ctx.manualRecordSaving),
    });
    (__VLS_ctx.manualRecordSaving ? "保存中…" : "保存本地记录");
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-trade-page']} */ ;
/** @type {__VLS_StyleScopedClasses['page-header']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['api-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-summary-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-metric']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['wide']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-cycle-running']} */ ;
/** @type {__VLS_StyleScopedClasses['cycle-spinner']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-error']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-dialog']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-confirm-error']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-header']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-close-button']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-editor-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-search-row']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-selected-value']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-error']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-groups']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kinds']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kind-list']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kind-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protected-kind-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-modal-hint']} */ ;
/** @type {__VLS_StyleScopedClasses['inline-status']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-settings']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['settings-list']} */ ;
/** @type {__VLS_StyleScopedClasses['formula-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['type-status-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['status-toggle']} */ ;
/** @type {__VLS_StyleScopedClasses['budget-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-list']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-account']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-input']} */ ;
/** @type {__VLS_StyleScopedClasses['wallet-reserve-empty']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-summary-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-stack']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['profit-trade-card']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-head']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-no']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-hash']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-badges']} */ ;
/** @type {__VLS_StyleScopedClasses['status-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['roi-basis-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['type-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['warning']} */ ;
/** @type {__VLS_StyleScopedClasses['type-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['dismiss-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-settle-row']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-circuit-note']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-track']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-error']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-zone']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-filter-note']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-filter-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-profit-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-trade-row']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-edit-button']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-main']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-card-head']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-badge']} */ ;
/** @type {__VLS_StyleScopedClasses['corrected']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-header']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-notice']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-form']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-field']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-field']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-search']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-item-selected']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-picker']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-field']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-unrecorded']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-field']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-preview']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-field']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-error']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-field']} */ ;
/** @type {__VLS_StyleScopedClasses['manual-record-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-field']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            ProfitTradeLongBuyStrategyPanel: ProfitTradeLongBuyStrategyPanel,
            ProfitTradeRoiWatch: ProfitTradeRoiWatch,
            dashboard: dashboard,
            listingsCooling: listingsCooling,
            loading: loading,
            apiOnline: apiOnline,
            message: message,
            manualProtectedAssetId: manualProtectedAssetId,
            manualProtectedMarketHashName: manualProtectedMarketHashName,
            manualProtectedSteamId: manualProtectedSteamId,
            protectionListOpen: protectionListOpen,
            protectionItemQuery: protectionItemQuery,
            protectionItemSuggestions: protectionItemSuggestions,
            protectionItemSearchOpen: protectionItemSearchOpen,
            protectionItemSearchBusy: protectionItemSearchBusy,
            protectionItemHasMore: protectionItemHasMore,
            protectionError: protectionError,
            dailySteamBudgetDraft: dailySteamBudgetDraft,
            reservedBalanceDrafts: reservedBalanceDrafts,
            manualSettleInputs: manualSettleInputs,
            runtimeBusy: runtimeBusy,
            runtimeConfirmEnabled: runtimeConfirmEnabled,
            runtimeConfirmError: runtimeConfirmError,
            configToggleBusy: configToggleBusy,
            longBuyWriteConfirm: longBuyWriteConfirm,
            longBuyWriteConfirmError: longBuyWriteConfirmError,
            completedDateFrom: completedDateFrom,
            completedDateTo: completedDateTo,
            completedAllSummary: completedAllSummary,
            manualRecordOpen: manualRecordOpen,
            manualRecordSaving: manualRecordSaving,
            manualRecordEditingTradeId: manualRecordEditingTradeId,
            manualRecordError: manualRecordError,
            manualItemQuery: manualItemQuery,
            manualItemSuggestions: manualItemSuggestions,
            manualItemSearchOpen: manualItemSearchOpen,
            manualItemSearchBusy: manualItemSearchBusy,
            manualItemHasMore: manualItemHasMore,
            manualRecordForm: manualRecordForm,
            activeTrades: activeTrades,
            completedTrades: completedTrades,
            manualEntryAccounts: manualEntryAccounts,
            manualRecordPreview: manualRecordPreview,
            completedHasDateFilter: completedHasDateFilter,
            completedFilteredTrades: completedFilteredTrades,
            completedTotalProfit: completedTotalProfit,
            completedFilteredProfit: completedFilteredProfit,
            completedTotalSteamBuy: completedTotalSteamBuy,
            completedFilteredSteamBuy: completedFilteredSteamBuy,
            completedProfitSummaryLabel: completedProfitSummaryLabel,
            completedSteamBuySummaryLabel: completedSteamBuySummaryLabel,
            completedTotalPages: completedTotalPages,
            completedCurrentPage: completedCurrentPage,
            completedPagedTrades: completedPagedTrades,
            completedPageRangeLabel: completedPageRangeLabel,
            protectedAssetCount: protectedAssetCount,
            protectedNameCount: protectedNameCount,
            protectedSteamCount: protectedSteamCount,
            protectedAssetPreview: protectedAssetPreview,
            protectedNamePreview: protectedNamePreview,
            protectedSteamPreview: protectedSteamPreview,
            autoRunEnabled: autoRunEnabled,
            profitCycleRunning: profitCycleRunning,
            stickerSlabActive: stickerSlabActive,
            stickerActive: stickerActive,
            apiStatusLabel: apiStatusLabel,
            realExecutionLabel: realExecutionLabel,
            longBuyRemoteWritesEnabled: longBuyRemoteWritesEnabled,
            longBuyObservationMode: longBuyObservationMode,
            longBuyExecutionLabel: longBuyExecutionLabel,
            longBuyExecutionDetail: longBuyExecutionDetail,
            autoRunStatusLabel: autoRunStatusLabel,
            autoRunCountdown: autoRunCountdown,
            nextAutoRunLabel: nextAutoRunLabel,
            lastRunLabel: lastRunLabel,
            formatMoney: formatMoney,
            formatPct: formatPct,
            formatDateTime: formatDateTime,
            manualAccountInitials: manualAccountInitials,
            openCreateManualRecord: openCreateManualRecord,
            openEditManualRecord: openEditManualRecord,
            closeManualRecord: closeManualRecord,
            searchManualItems: searchManualItems,
            searchProtectionItems: searchProtectionItems,
            onProtectionItemInput: onProtectionItemInput,
            chooseProtectionItem: chooseProtectionItem,
            openProtectionList: openProtectionList,
            closeProtectionList: closeProtectionList,
            onManualItemInput: onManualItemInput,
            chooseManualItem: chooseManualItem,
            saveManualRecord: saveManualRecord,
            loadCompletedTrades: loadCompletedTrades,
            setCompletedDatePreset: setCompletedDatePreset,
            goCompletedPage: goCompletedPage,
            noteText: noteText,
            noteNumber: noteNumber,
            balanceDiscountPct: balanceDiscountPct,
            steamAccountLabel: steamAccountLabel,
            duplicateBuyText: duplicateBuyText,
            signedClass: signedClass,
            isStickerSlab: isStickerSlab,
            canManualSettle: canManualSettle,
            dismissActionLabel: dismissActionLabel,
            statusLabel: statusLabel,
            stepClass: stepClass,
            loadDashboard: loadDashboard,
            toggleEnabled: toggleEnabled,
            runOnce: runOnce,
            openRuntimeConfirm: openRuntimeConfirm,
            closeRuntimeConfirm: closeRuntimeConfirm,
            requestLongBuyConfigToggle: requestLongBuyConfigToggle,
            closeLongBuyWriteConfirm: closeLongBuyWriteConfirm,
            confirmLongBuyWriteToggle: confirmLongBuyWriteToggle,
            saveDailySteamBudget: saveDailySteamBudget,
            saveAccountReservedBalances: saveAccountReservedBalances,
            manualSettleTrade: manualSettleTrade,
            dismissTrade: dismissTrade,
            toggleStickerSlabStatus: toggleStickerSlabStatus,
            toggleStickerStatus: toggleStickerStatus,
            sendDailyReport: sendDailyReport,
            scanOpportunities: scanOpportunities,
            refreshSales: refreshSales,
            lockTrade: lockTrade,
            buyTrade: buyTrade,
            listC5Trade: listC5Trade,
            updateProtection: updateProtection,
            addManualProtectedAsset: addManualProtectedAsset,
            addManualProtectedMarketHashName: addManualProtectedMarketHashName,
            addManualProtectedSteamId: addManualProtectedSteamId,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
