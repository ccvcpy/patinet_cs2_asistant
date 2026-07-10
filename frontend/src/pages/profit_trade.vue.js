import { computed, onMounted, onUnmounted, ref } from "vue";
const fallbackDashboard = {
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
const dashboard = ref(fallbackDashboard);
const loading = ref(false);
const apiOnline = ref(null);
const message = ref("");
const manualProtectedAssetId = ref("");
const manualProtectedMarketHashName = ref("");
const manualProtectedSteamId = ref("");
const dailySteamBudgetDraft = ref("1000");
const manualSettleInputs = ref({});
const autoRunIntervalMs = 10 * 60 * 1000;
const autoRunStorageKey = "profitTrade.autoRun.v1";
const lastRunStorageKey = "profitTrade.lastRun.v1";
const autoRunTimer = ref(null);
const autoRunTickTimer = ref(null);
const autoRunActive = ref(false);
const autoRunNextAt = ref(null);
const autoRunInFlight = ref(false);
const lastRunAt = ref(null);
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
        if (time === null)
            return !hasFilter;
        if (from !== null && time < from)
            return false;
        if (to !== null && time > to)
            return false;
        return true;
    });
});
const completedTotalProfit = computed(() => completedTrades.value.reduce((total, trade) => total + (Number(trade.realizedProfit) || 0), 0));
const completedFilteredProfit = computed(() => completedFilteredTrades.value.reduce((total, trade) => total + (Number(trade.realizedProfit) || 0), 0));
const completedTotalSteamBuy = computed(() => completedTrades.value.reduce((total, trade) => total + (Number(trade.steamBuyPrice) || 0), 0));
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
const protectedNamePreview = computed(() => dashboard.value.config.protectedMarketHashNames ?? []);
const protectedSteamPreview = computed(() => dashboard.value.config.protectedSteamIds ?? []);
const autoRunEnabled = computed(() => autoRunActive.value);
const stickerSlabActive = computed(() => dashboard.value.config.stickerSlabStatus === "active");
const stickerActive = computed(() => dashboard.value.config.stickerStatus === "active");
const autoRunCountdown = computed(() => {
    if (!autoRunNextAt.value)
        return "";
    const remainingSeconds = Math.max(0, Math.ceil((autoRunNextAt.value - nowMs.value) / 1000));
    const minutes = Math.floor(remainingSeconds / 60);
    const seconds = remainingSeconds % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
});
const apiStatusLabel = computed(() => {
    if (apiOnline.value === true)
        return "后端 API 已启动";
    if (apiOnline.value === false)
        return "后端 API 未连接";
    return "后端 API 检查中";
});
const realExecutionLabel = computed(() => (dashboard.value.config.allowRealExecution ? "真实执行已开放" : "真实执行未开放"));
const autoRunStatusLabel = computed(() => {
    if (autoRunInFlight.value)
        return "浏览器循环执行中";
    return autoRunActive.value ? "浏览器循环运行中" : "浏览器循环未运行";
});
const nextAutoRunLabel = computed(() => {
    if (autoRunInFlight.value)
        return "本轮执行中";
    if (!autoRunActive.value)
        return "未计划";
    if (!autoRunNextAt.value)
        return "等待安排";
    return `${formatDateTime(autoRunNextAt.value)}（${autoRunCountdown.value}）`;
});
const lastRunLabel = computed(() => {
    const backendLastRun = dashboard.value.lastRun;
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
function resetCompletedPage() {
    completedPage.value = 1;
}
function setCompletedDatePreset(days) {
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
async function fetchJson(url, init) {
    const response = await fetch(url, init);
    if (!response.ok) {
        let detail = "";
        try {
            const payload = await response.json();
            detail = payload.error ? ` ${payload.error}` : "";
        }
        catch {
            detail = "";
        }
        throw new Error(`${response.status} ${response.statusText}${detail}`);
    }
    return response.json();
}
async function loadDashboard() {
    loading.value = true;
    message.value = "";
    try {
        dashboard.value = (await fetchJson("/api/profit-trade/dashboard"));
        dailySteamBudgetDraft.value = String(dashboard.value.config.dailySteamBudget ?? 1000);
        apiOnline.value = true;
    }
    catch {
        apiOnline.value = false;
        try {
            dashboard.value = (await fetchJson("/profit_trade_dashboard.json"));
            dailySteamBudgetDraft.value = String(dashboard.value.config.dailySteamBudget ?? 1000);
        }
        catch {
            dashboard.value = fallbackDashboard;
        }
    }
    finally {
        loading.value = false;
    }
}
async function toggleEnabled() {
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
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `API未连接，无法${nextEnabled ? "开启" : "关闭"}后端开关`;
    }
}
async function toggleRealExecution() {
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
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `真实执行开关失败：${error instanceof Error ? error.message : String(error)}`;
    }
}
async function toggleRepriceExecution() {
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
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `C5改价开关失败：${error instanceof Error ? error.message : String(error)}`;
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
    }
    catch (error) {
        apiOnline.value = false;
        message.value = "API未连接或扫描失败";
    }
    finally {
        loading.value = false;
    }
}
async function runOnce() {
    message.value = "";
    loading.value = true;
    try {
        const payload = await fetchJson("/api/profit-trade/run-once", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ scanMaxItems: dashboard.value.config.scanMaxItems }),
        });
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
    }
    catch (error) {
        apiOnline.value = false;
        const summary = `失败：${error instanceof Error ? error.message : String(error)}`;
        lastRunAt.value = Date.now();
        lastRunResult.value = summary;
        saveLastRunState();
        message.value = `执行一轮失败：${summary.replace(/^失败：/, "")}`;
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
    }
    catch (error) {
        apiOnline.value = false;
        message.value = `保护名单更新失败：${error instanceof Error ? error.message : String(error)}`;
    }
}
async function addManualProtectedAsset() {
    await updateProtection("add", "asset", manualProtectedAssetId.value);
    manualProtectedAssetId.value = "";
}
async function addManualProtectedMarketHashName() {
    await updateProtection("add", "marketHashName", manualProtectedMarketHashName.value);
    manualProtectedMarketHashName.value = "";
}
async function addManualProtectedSteamId() {
    await updateProtection("add", "steamId", manualProtectedSteamId.value);
    manualProtectedSteamId.value = "";
}
function saveAutoRunState() {
    if (typeof window === "undefined")
        return;
    if (!autoRunActive.value || !autoRunNextAt.value) {
        window.localStorage.removeItem(autoRunStorageKey);
        return;
    }
    window.localStorage.setItem(autoRunStorageKey, JSON.stringify({
        enabled: true,
        nextAt: autoRunNextAt.value,
    }));
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
function clearAutoRunTimers() {
    if (autoRunTimer.value !== null) {
        clearTimeout(autoRunTimer.value);
        autoRunTimer.value = null;
    }
    if (autoRunTickTimer.value !== null) {
        clearInterval(autoRunTickTimer.value);
        autoRunTickTimer.value = null;
    }
}
function scheduleAutoRun(nextAt) {
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
async function runScheduledOnce() {
    if (autoRunInFlight.value)
        return;
    autoRunInFlight.value = true;
    try {
        await runOnce();
    }
    finally {
        autoRunInFlight.value = false;
        if (autoRunActive.value) {
            scheduleAutoRun(Date.now() + autoRunIntervalMs);
        }
    }
}
async function startAutoRun() {
    if (autoRunActive.value)
        return;
    autoRunActive.value = true;
    autoRunNextAt.value = null;
    nowMs.value = Date.now();
    message.value = "循环执行已开启：正在立刻执行第一轮";
    await runScheduledOnce();
}
function stopAutoRun() {
    clearAutoRunTimers();
    autoRunActive.value = false;
    autoRunNextAt.value = null;
    autoRunInFlight.value = false;
    nowMs.value = Date.now();
    saveAutoRunState();
    message.value = "循环执行已停止";
}
function toggleAutoRun() {
    if (autoRunEnabled.value) {
        stopAutoRun();
        return;
    }
    void startAutoRun();
}
function restoreAutoRun() {
    if (typeof window === "undefined")
        return;
    const raw = window.localStorage.getItem(autoRunStorageKey);
    if (!raw)
        return;
    try {
        const stored = JSON.parse(raw);
        if (!stored.enabled)
            return;
        const nextAt = Number(stored.nextAt);
        const restoredNextAt = Number.isFinite(nextAt) && nextAt > 0
            ? Math.max(nextAt, Date.now())
            : Date.now() + autoRunIntervalMs;
        scheduleAutoRun(restoredNextAt);
        message.value = "循环执行已恢复：页面刷新后继续计时";
    }
    catch {
        window.localStorage.removeItem(autoRunStorageKey);
    }
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
    restoreAutoRun();
});
onUnmounted(() => {
    clearAutoRunTimers();
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
/** @type {__VLS_StyleScopedClasses['online']} */ ;
/** @type {__VLS_StyleScopedClasses['execution-status-item']} */ ;
/** @type {__VLS_StyleScopedClasses['offline']} */ ;
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
/** @type {__VLS_StyleScopedClasses['protection-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
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
    ...{ onClick: (__VLS_ctx.scanOpportunities) },
    ...{ class: "secondary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.runOnce) },
    ...{ class: "secondary-button" },
    type: "button",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.toggleAutoRun) },
    ...{ class: "secondary-button" },
    ...{ class: ({ active: __VLS_ctx.autoRunEnabled }) },
    type: "button",
});
(__VLS_ctx.autoRunEnabled ? `停止循环 ${__VLS_ctx.autoRunCountdown}` : "循环执行10分钟");
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
    ...{ onClick: (__VLS_ctx.toggleEnabled) },
    ...{ class: "primary-button" },
    type: "button",
});
(__VLS_ctx.dashboard.config.enabled ? "关闭执行器" : "开启执行器");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.toggleRepriceExecution) },
    ...{ class: "secondary-button" },
    ...{ class: ({ active: __VLS_ctx.dashboard.config.allowRepriceExecution }) },
    type: "button",
});
(__VLS_ctx.dashboard.config.allowRepriceExecution ? "禁止仅C5改价" : "仅允许C5改价");
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.toggleRealExecution) },
    ...{ class: "secondary-button danger-button" },
    type: "button",
});
(__VLS_ctx.dashboard.config.allowRealExecution ? "禁止真实执行" : "允许真实执行");
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "profit-summary-grid" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "profit-metric" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard.config.enabled ? "已开启" : "已关闭");
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
    ...{ class: ({ online: __VLS_ctx.autoRunEnabled, running: __VLS_ctx.autoRunInFlight }) },
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
if (__VLS_ctx.message) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "inline-status" },
    });
    (__VLS_ctx.message);
}
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
(__VLS_ctx.dashboard.config.scanMaxItems);
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
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "protection-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
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
    ...{ class: "protection-input-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    id: "protected-market-hash-name",
    value: (__VLS_ctx.manualProtectedMarketHashName),
    type: "text",
    autocomplete: "off",
    placeholder: "market_hash_name",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "mini-action protect-action" },
    type: "submit",
});
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
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    id: "protected-steam-id",
    value: (__VLS_ctx.manualProtectedSteamId),
    type: "text",
    autocomplete: "off",
    placeholder: "SteamId64",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ class: "mini-action protect-action" },
    type: "submit",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "protection-group" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
for (const [assetId] of __VLS_getVForSourceType((__VLS_ctx.protectedAssetPreview))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.updateProtection('remove', 'asset', assetId);
            } },
        key: (assetId),
        ...{ class: "protection-chip" },
        type: "button",
    });
    (assetId);
}
if (__VLS_ctx.protectedAssetCount === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "protection-group" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
for (const [marketHashName] of __VLS_getVForSourceType((__VLS_ctx.protectedNamePreview))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.updateProtection('remove', 'marketHashName', marketHashName);
            } },
        key: (marketHashName),
        ...{ class: "protection-chip" },
        type: "button",
    });
    (marketHashName);
}
if (__VLS_ctx.protectedNameCount === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "protection-group" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
for (const [steamId] of __VLS_getVForSourceType((__VLS_ctx.protectedSteamPreview))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.updateProtection('remove', 'steamId', steamId);
            } },
        key: (steamId),
        ...{ class: "protection-chip" },
        type: "button",
    });
    (steamId);
}
if (__VLS_ctx.protectedSteamCount === 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
    ...{ class: "protection-hint" },
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
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
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
(__VLS_ctx.completedTrades.length);
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "completed-filter-note" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "completed-toolbar" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    ...{ onChange: (__VLS_ctx.resetCompletedPage) },
    type: "date",
});
(__VLS_ctx.completedDateFrom);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input, __VLS_intrinsicElements.input)({
    ...{ onChange: (__VLS_ctx.resetCompletedPage) },
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
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "completed-main" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (trade.name || trade.marketHashName);
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
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['danger-button']} */ ;
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
/** @type {__VLS_StyleScopedClasses['wide']} */ ;
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
/** @type {__VLS_StyleScopedClasses['protection-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-form']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-input-row']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protect-action']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-group']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['protection-hint']} */ ;
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
/** @type {__VLS_StyleScopedClasses['progress-track']} */ ;
/** @type {__VLS_StyleScopedClasses['step-row']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['trade-error']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-zone']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['soft-label']} */ ;
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
/** @type {__VLS_StyleScopedClasses['completed-main']} */ ;
/** @type {__VLS_StyleScopedClasses['completed-metrics']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            dashboard: dashboard,
            loading: loading,
            apiOnline: apiOnline,
            message: message,
            manualProtectedAssetId: manualProtectedAssetId,
            manualProtectedMarketHashName: manualProtectedMarketHashName,
            manualProtectedSteamId: manualProtectedSteamId,
            dailySteamBudgetDraft: dailySteamBudgetDraft,
            manualSettleInputs: manualSettleInputs,
            autoRunInFlight: autoRunInFlight,
            completedDateFrom: completedDateFrom,
            completedDateTo: completedDateTo,
            activeTrades: activeTrades,
            completedTrades: completedTrades,
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
            stickerSlabActive: stickerSlabActive,
            stickerActive: stickerActive,
            autoRunCountdown: autoRunCountdown,
            apiStatusLabel: apiStatusLabel,
            realExecutionLabel: realExecutionLabel,
            autoRunStatusLabel: autoRunStatusLabel,
            nextAutoRunLabel: nextAutoRunLabel,
            lastRunLabel: lastRunLabel,
            formatMoney: formatMoney,
            formatPct: formatPct,
            formatDateTime: formatDateTime,
            resetCompletedPage: resetCompletedPage,
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
            toggleRealExecution: toggleRealExecution,
            toggleRepriceExecution: toggleRepriceExecution,
            saveDailySteamBudget: saveDailySteamBudget,
            manualSettleTrade: manualSettleTrade,
            dismissTrade: dismissTrade,
            toggleStickerSlabStatus: toggleStickerSlabStatus,
            toggleStickerStatus: toggleStickerStatus,
            sendDailyReport: sendDailyReport,
            scanOpportunities: scanOpportunities,
            runOnce: runOnce,
            refreshSales: refreshSales,
            lockTrade: lockTrade,
            buyTrade: buyTrade,
            listC5Trade: listC5Trade,
            updateProtection: updateProtection,
            addManualProtectedAsset: addManualProtectedAsset,
            addManualProtectedMarketHashName: addManualProtectedMarketHashName,
            addManualProtectedSteamId: addManualProtectedSteamId,
            toggleAutoRun: toggleAutoRun,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
