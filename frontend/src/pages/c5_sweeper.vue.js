import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import FolioIcon from "../components/FolioIcon.vue";
import OperationVisualAtom from "../components/OperationVisualAtom.vue";
const dashboard = ref(null);
const apiError = ref("");
const actionError = ref("");
const actionMessage = ref("");
const busy = ref(false);
const clock = ref(Date.now());
const creatingNew = ref(false);
const formDirty = ref(false);
const selectedRoundId = ref(null);
const receivingAccounts = ref([]);
const selectedReceivingAccountId = ref("");
const accountsBusy = ref(false);
const itemQuery = ref("");
const selectedItem = ref(null);
const itemSuggestions = ref([]);
const itemSearchOpen = ref(false);
const itemSearchBusy = ref(false);
const itemSearchHasMore = ref(false);
const itemSearchNextOffset = ref(0);
const maxPrice = ref(1.1);
const budget = ref(300);
const targetCount = ref(200);
const intervalSeconds = ref(60);
const confirmationOpen = ref(false);
const confirmation = ref("");
let pollTimer;
let clockTimer;
let searchTimer;
async function fetchJson(path, options) {
    const response = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok)
        throw new Error(payload.error || `请求失败 (${response.status})`);
    return payload;
}
function syncForm(round) {
    if (!round)
        return;
    selectedItem.value = { marketHashName: round.marketHashName, displayName: round.displayName };
    itemQuery.value = `${round.displayName} · ${round.marketHashName}`;
    maxPrice.value = round.maxPrice;
    budget.value = round.budget;
    targetCount.value = round.targetCount;
    intervalSeconds.value = round.intervalSeconds;
    selectedReceivingAccountId.value = round.receivingAccountId || "";
    formDirty.value = false;
}
function applyDashboard(value, forceForm = false) {
    if (!Array.isArray(value.rounds) || !("round" in value)) {
        throw new Error("8765 后端仍是旧版，请在后端终端按 Ctrl+C 后重新执行原启动命令");
    }
    dashboard.value = value;
    if (creatingNew.value)
        return;
    if (value.round)
        selectedRoundId.value = value.round.id;
    if (forceForm || !formDirty.value)
        syncForm(value.round);
}
async function loadDashboard(silent = false, roundId) {
    if (creatingNew.value && silent)
        return;
    const id = roundId === undefined ? selectedRoundId.value : roundId;
    const suffix = id ? `?roundId=${encodeURIComponent(id)}` : "";
    try {
        const payload = (await fetchJson(`/api/c5-sweeper/dashboard${suffix}`));
        applyDashboard(payload, !silent);
        apiError.value = "";
    }
    catch (error) {
        apiError.value = error instanceof Error ? error.message : String(error);
        if (!silent)
            actionError.value = apiError.value;
    }
}
async function loadReceivingAccounts(refresh = false) {
    accountsBusy.value = true;
    try {
        const payload = await fetchJson(`/api/c5-sweeper/accounts${refresh ? "?refresh=1" : ""}`);
        receivingAccounts.value = payload.accounts || [];
        if (!selectedReceivingAccountId.value) {
            selectedReceivingAccountId.value = receivingAccounts.value.find((row) => row.available)?.id || "";
        }
    }
    catch (error) {
        actionError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        accountsBusy.value = false;
    }
}
async function postAction(path, body, success) {
    busy.value = true;
    actionError.value = "";
    actionMessage.value = "";
    try {
        const payload = await fetchJson(path, { method: "POST", body: JSON.stringify(body) });
        if (payload.dashboard) {
            creatingNew.value = false;
            applyDashboard(payload.dashboard, true);
        }
        actionMessage.value = success;
        return payload;
    }
    catch (error) {
        actionError.value = error instanceof Error ? error.message : String(error);
        return null;
    }
    finally {
        busy.value = false;
    }
}
async function searchItems(query = itemQuery.value, append = false) {
    itemSearchBusy.value = true;
    try {
        const normalizedQuery = query.trim();
        const offset = append ? itemSearchNextOffset.value : 0;
        const payload = await fetchJson(`/api/c5-sweeper/items?query=${encodeURIComponent(normalizedQuery)}&limit=20&offset=${offset}`);
        if (normalizedQuery !== itemQuery.value.trim())
            return;
        const incoming = payload.items || [];
        if (append) {
            const merged = new Map(itemSuggestions.value.map((item) => [item.marketHashName, item]));
            for (const item of incoming)
                merged.set(item.marketHashName, item);
            itemSuggestions.value = [...merged.values()];
        }
        else {
            itemSuggestions.value = incoming;
        }
        itemSearchHasMore.value = Boolean(payload.pagination?.hasMore);
        itemSearchNextOffset.value = Number(payload.pagination?.nextOffset ?? 0);
        itemSearchOpen.value = true;
    }
    catch (error) {
        actionError.value = error instanceof Error ? error.message : String(error);
    }
    finally {
        itemSearchBusy.value = false;
    }
}
function onItemInput() {
    selectedItem.value = null;
    formDirty.value = true;
    itemSearchOpen.value = true;
    itemSearchHasMore.value = false;
    itemSearchNextOffset.value = 0;
    if (searchTimer)
        window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => searchItems(), 250);
}
function chooseItem(item) {
    selectedItem.value = item;
    itemQuery.value = `${item.displayName} · ${item.marketHashName}`;
    itemSearchOpen.value = false;
    formDirty.value = true;
}
const displayRound = computed(() => creatingNew.value ? null : dashboard.value?.round || null);
const displayCounts = computed(() => creatingNew.value
    ? { accepted: 0, delivered: 0, pending: 0, failed: 0 }
    : dashboard.value?.counts || { accepted: 0, delivered: 0, pending: 0, failed: 0 });
const displayMoney = computed(() => creatingNew.value
    ? { budget: Number(budget.value), committedAmount: 0, remainingBudget: Number(budget.value), averageAcceptedPrice: 0 }
    : dashboard.value?.money || { budget: 0, committedAmount: 0, remainingBudget: 0, averageAcceptedPrice: 0 });
const selectedReceivingAccount = computed(() => receivingAccounts.value.find((row) => row.id === selectedReceivingAccountId.value) || null);
const currentStatus = computed(() => displayRound.value?.status || "empty");
const canEdit = computed(() => !apiError.value && (creatingNew.value || ["draft", "paused"].includes(currentStatus.value)));
const hasOpenRound = computed(() => dashboard.value?.rounds.some((row) => !["completed", "stopped"].includes(row.status)) ?? false);
const canCreateNext = computed(() => !apiError.value && !hasOpenRound.value && !dashboard.value?.realExecutionRunning);
const quantityProgress = computed(() => {
    if (!displayRound.value)
        return 0;
    return Math.min(100, displayCounts.value.delivered / Math.max(1, displayRound.value.targetCount) * 100);
});
const budgetProgress = computed(() => {
    const total = Number(displayMoney.value.budget) || Number(budget.value) || 0;
    const used = Number(displayMoney.value.committedAmount) || 0;
    return total > 0 ? Math.min(100, used / total * 100) : 0;
});
const formAffordableCount = computed(() => {
    const price = Number(maxPrice.value);
    return price > 0 ? Math.floor(Number(budget.value) / price) : 0;
});
const formTargetCost = computed(() => Number(maxPrice.value) * Number(targetCount.value));
const countdown = computed(() => {
    const next = displayRound.value?.nextRunAt;
    if (!next || currentStatus.value !== "running")
        return "—";
    return `${Math.max(0, Math.ceil((new Date(next).getTime() - clock.value) / 1000))} 秒`;
});
const priceSafe = computed(() => {
    const live = displayRound.value?.lastPrice;
    return live != null && live <= Number(maxPrice.value);
});
const unresolvedCount = computed(() => (displayRound.value?.submissions || []).reduce((sum, row) => sum + (row.unresolvedProductIds?.length || 0), 0));
function markDirty() {
    formDirty.value = true;
}
function resetNewRound() {
    if (!canCreateNext.value) {
        actionError.value = "当前轮次尚未结束，请先继续或停止当前轮次。";
        return;
    }
    creatingNew.value = true;
    selectedRoundId.value = null;
    const defaultItem = dashboard.value?.recentItems?.[0] || { marketHashName: "Kilowatt Case", displayName: "千瓦武器箱" };
    selectedItem.value = defaultItem;
    itemQuery.value = `${defaultItem.displayName} · ${defaultItem.marketHashName}`;
    maxPrice.value = 1.1;
    budget.value = 300;
    targetCount.value = 200;
    intervalSeconds.value = 60;
    formDirty.value = false;
    actionError.value = "";
    selectedReceivingAccountId.value = receivingAccounts.value.find((row) => row.available)?.id || "";
    itemSearchOpen.value = false;
    actionMessage.value = "";
}
async function saveRound(startAfterSave = false) {
    if (!selectedItem.value) {
        actionError.value = "请从搜索结果中选择饰品；自定义 market_hash_name 也需要点击对应结果。";
        return;
    }
    if (!selectedReceivingAccountId.value) {
        actionError.value = "请选择接收武器箱的 Steam 账号。";
        return;
    }
    const roundId = creatingNew.value ? null : dashboard.value?.round?.id || null;
    const result = await postAction("/api/c5-sweeper/round", {
        roundId,
        marketHashName: selectedItem.value.marketHashName,
        displayName: selectedItem.value.displayName,
        receivingAccountId: selectedReceivingAccountId.value,
        maxPrice: Number(maxPrice.value),
        budget: Number(budget.value),
        targetCount: Number(targetCount.value),
        intervalSeconds: Number(intervalSeconds.value),
        delivery: 2,
    }, startAfterSave ? "轮次参数已保存，请输入确认词开启真实购买" : "轮次草稿已保存，不会自动购买");
    if (result && startAfterSave)
        confirmationOpen.value = true;
}
async function confirmStart() {
    const id = dashboard.value?.round?.id;
    if (!id)
        return;
    const result = await postAction("/api/c5-sweeper/start", {
        roundId: id,
        confirmation: confirmation.value,
    }, "本轮已启动：立即提交一次批量购买，之后每 60 秒提交下一批");
    if (result) {
        confirmationOpen.value = false;
        confirmation.value = "";
    }
}
async function selectRound(round) {
    creatingNew.value = false;
    selectedRoundId.value = round.id;
    formDirty.value = false;
    await loadDashboard(false, round.id);
    window.scrollTo({ top: 0, behavior: "smooth" });
}
async function pauseRound() {
    const id = dashboard.value?.round?.id;
    if (id)
        await postAction("/api/c5-sweeper/pause", { roundId: id }, "本轮已暂停，待交付订单仍会继续审计");
}
async function stopRound() {
    const id = dashboard.value?.round?.id;
    if (id)
        await postAction("/api/c5-sweeper/stop", { roundId: id }, "本轮已停止并归档，现在可以新建下一轮");
}
async function refreshRound() {
    const id = dashboard.value?.round?.id;
    if (id)
        await postAction("/api/c5-sweeper/refresh", { roundId: id }, "价格与交付状态已刷新，没有发起购买");
}
async function confirmNotBought() {
    const id = displayRound.value?.id;
    if (!id || unresolvedCount.value <= 0)
        return;
    const confirmed = window.confirm(`确认这 ${unresolvedCount.value} 件商品在 C5 未生成订单、未扣款？确认后本轮会自动继续扫货。`);
    if (!confirmed)
        return;
    await postAction("/api/c5-sweeper/confirm-not-bought", { roundId: id }, "已确认未成交，本轮自动继续");
}
function statusText(status) {
    return {
        empty: "等待新建轮次",
        draft: "草稿",
        paused: "已暂停",
        running: "运行中",
        stopped: "已停止",
        completed: "已完成",
    }[status || "empty"];
}
function stopReasonText(reason) {
    return {
        target_reached: "达到数量目标",
        budget_reached: "预算已用完",
        budget_limit: "剩余预算不足下一件",
        manual: "手动停止",
        buy_uncertain: "购买结果不确定，已暂停",
    }[reason || ""] || "—";
}
function orderStatus(value) {
    return { pending: "等待交付", delivered: "交付成功", failed: "交付失败" }[value];
}
function money(value) {
    return value == null ? "—" : `¥${Number(value).toFixed(2)}`;
}
function maskSteamId(value) {
    if (!value)
        return "—";
    return value.length > 10 ? `${value.slice(0, 7)}***${value.slice(-4)}` : value;
}
function accountInitials(value) {
    const text = (value || "ST").trim();
    return text.slice(0, 2).toUpperCase();
}
function dateTime(value) {
    if (!value)
        return "—";
    return new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(new Date(value));
}
onMounted(async () => {
    await loadDashboard();
    await loadReceivingAccounts(true);
    if (!dashboard.value?.round)
        resetNewRound();
    pollTimer = window.setInterval(() => loadDashboard(true), 2000);
    clockTimer = window.setInterval(() => { clock.value = Date.now(); }, 1000);
});
onBeforeUnmount(() => {
    if (pollTimer)
        window.clearInterval(pollTimer);
    if (clockTimer)
        window.clearInterval(clockTimer);
    if (searchTimer)
        window.clearTimeout(searchTimer);
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['details-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['form-body']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-logo']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['item-name']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['round-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['form-body']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['form-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-message']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['rounds-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-logo']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-logo']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['api']} */ ;
/** @type {__VLS_StyleScopedClasses['offline']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['amber']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['blue']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-logo']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['api']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['api']} */ ;
/** @type {__VLS_StyleScopedClasses['offline']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['notice']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['account-caption']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['amber']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['navy']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['blue']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['round-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['budget-field']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['account-picker']} */ ;
/** @type {__VLS_StyleScopedClasses['account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['archive-note']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-message']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['blue-text']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['running']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['completed']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['delivered']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['draft']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['paused']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['pending']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['stopped']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['failed']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-logo']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['item-name']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['round-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['account-picker']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['form-body']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['form-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['rounds-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['events-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['details-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-logo']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['api']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['api']} */ ;
/** @type {__VLS_StyleScopedClasses['offline']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['account-caption']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['green-text']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['green']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['navy']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['amber']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['blue']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['round-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-picker']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['budget-field']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['archive-note']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-message']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['blue-text']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['running']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['completed']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['delivered']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['draft']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['paused']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['pending']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['stopped']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['failed']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-visual-atom']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['api']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['api']} */ ;
/** @type {__VLS_StyleScopedClasses['offline']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['notice']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['account-caption']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['green-text']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['blue']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['round-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-picker']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['unavailable']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['unavailable']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['unavailable']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['budget-field']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['archive-note']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-message']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['blue-text']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['running']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['completed']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['delivered']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['draft']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['paused']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['pending']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['stopped']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['failed']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-visual-atom']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['account-picker']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "sweeper-page sweeper-page--folio-refresh sweeper-page--minimal-v2" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "hero" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "hero-title" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_0 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "case-scanner",
    size: (70),
}));
const __VLS_1 = __VLS_0({
    name: "case-scanner",
    size: (70),
}, ...__VLS_functionalComponentArgsRest(__VLS_0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "hero-status" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
    ...{ class: "status-icon api" },
    ...{ class: ({ offline: __VLS_ctx.apiError }) },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_3 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: "link",
    size: (15),
}));
const __VLS_4 = __VLS_3({
    name: "link",
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_3));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.apiError ? "未连接" : "已连接");
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
    ...{ class: "status-icon execution" },
    ...{ class: ({ active: __VLS_ctx.dashboard?.realExecutionRunning }) },
});
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_6 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.dashboard?.realExecutionRunning ? 'play' : 'pause'),
    size: (15),
}));
const __VLS_7 = __VLS_6({
    name: (__VLS_ctx.dashboard?.realExecutionRunning ? 'play' : 'pause'),
    size: (15),
}, ...__VLS_functionalComponentArgsRest(__VLS_6));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dashboard?.realExecutionRunning ? "运行中" : "已暂停");
if (__VLS_ctx.apiError) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "alert error" },
    });
    (__VLS_ctx.apiError);
}
if (__VLS_ctx.actionError) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "alert error" },
    });
    (__VLS_ctx.actionError);
}
if (__VLS_ctx.displayRound?.workerError) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "alert error" },
    });
    (__VLS_ctx.displayRound.workerError);
}
if (__VLS_ctx.actionMessage) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "alert notice" },
    });
    (__VLS_ctx.actionMessage);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "metrics" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-label" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_9 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "case-single",
    size: (38),
}));
const __VLS_10 = __VLS_9({
    name: "case-single",
    size: (38),
}, ...__VLS_functionalComponentArgsRest(__VLS_9));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({
    ...{ class: "item-name" },
});
(__VLS_ctx.selectedItem?.displayName || __VLS_ctx.displayRound?.displayName || "尚未选择");
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
(__VLS_ctx.selectedItem?.marketHashName || __VLS_ctx.displayRound?.marketHashName || "选择后才会保存");
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
    ...{ class: "account-caption" },
});
(__VLS_ctx.selectedReceivingAccount?.name || __VLS_ctx.displayRound?.receivingAccountName || "未选择账号");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-label" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_12 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "price-tag",
    size: (38),
}));
const __VLS_13 = __VLS_12({
    name: "price-tag",
    size: (38),
}, ...__VLS_functionalComponentArgsRest(__VLS_12));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.money(__VLS_ctx.displayRound?.lastPrice));
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
    ...{ class: (__VLS_ctx.priceSafe ? 'green-text' : '') },
});
(__VLS_ctx.displayRound?.lastPrice == null ? "等待读取" : __VLS_ctx.priceSafe ? "符合最高价" : "高于最高价");
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-label" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_15 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "budget-wallet",
    size: (38),
}));
const __VLS_16 = __VLS_15({
    name: "budget-wallet",
    size: (38),
}, ...__VLS_functionalComponentArgsRest(__VLS_15));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.money(__VLS_ctx.displayRound?.budget ?? Number(__VLS_ctx.budget)));
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-label" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_18 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "clock",
    size: (38),
}));
const __VLS_19 = __VLS_18({
    name: "clock",
    size: (38),
}, ...__VLS_functionalComponentArgsRest(__VLS_18));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.money(__VLS_ctx.displayMoney.committedAmount));
__VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
(__VLS_ctx.budgetProgress.toFixed(1));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "mini-track" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
    ...{ style: ({ width: `${__VLS_ctx.budgetProgress}%` }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-label" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_21 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "delivery-success",
    size: (38),
}));
const __VLS_22 = __VLS_21({
    name: "delivery-success",
    size: (38),
}, ...__VLS_functionalComponentArgsRest(__VLS_21));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.displayCounts.delivered);
(__VLS_ctx.displayRound?.targetCount ?? __VLS_ctx.targetCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "mini-track blue" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
    ...{ style: ({ width: `${__VLS_ctx.quantityProgress}%` }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "metric-label" },
});
/** @type {[typeof OperationVisualAtom, ]} */ ;
// @ts-ignore
const __VLS_24 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
    name: "refresh",
    size: (38),
}));
const __VLS_25 = __VLS_24({
    name: "refresh",
    size: (38),
}, ...__VLS_functionalComponentArgsRest(__VLS_24));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.countdown);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "workspace" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "panel form-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
(__VLS_ctx.creatingNew ? "新建扫货轮次" : `编辑第 ${__VLS_ctx.dashboard?.round?.roundNumber ?? "—"} 轮`);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "round-chip" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "field item-field" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "search-wrap" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onInput: (__VLS_ctx.onItemInput) },
    ...{ onFocus: (...[$event]) => {
            __VLS_ctx.searchItems(__VLS_ctx.itemQuery);
        } },
    disabled: (!__VLS_ctx.canEdit),
    placeholder: "输入武器箱中文名或以 Case 结尾的名称",
});
(__VLS_ctx.itemQuery);
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.itemSearchBusy ? "···" : "⌕");
if (__VLS_ctx.itemSearchOpen && __VLS_ctx.canEdit) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "suggestions" },
    });
    for (const [item] of __VLS_getVForSourceType((__VLS_ctx.itemSuggestions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.itemSearchOpen && __VLS_ctx.canEdit))
                        return;
                    __VLS_ctx.chooseItem(item);
                } },
            key: (item.marketHashName),
            type: "button",
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (item.displayName);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (item.marketHashName);
        if (item.custom) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
        }
    }
    if (__VLS_ctx.itemSearchHasMore) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.itemSearchOpen && __VLS_ctx.canEdit))
                        return;
                    if (!(__VLS_ctx.itemSearchHasMore))
                        return;
                    __VLS_ctx.searchItems(__VLS_ctx.itemQuery, true);
                } },
            ...{ class: "catalog-load-more" },
            type: "button",
            disabled: (__VLS_ctx.itemSearchBusy),
        });
        (__VLS_ctx.itemSearchBusy ? "加载中…" : "加载更多结果");
    }
    if (!__VLS_ctx.itemSuggestions.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
}
if (__VLS_ctx.dashboard?.recentItems.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "recent-items" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    for (const [item] of __VLS_getVForSourceType((__VLS_ctx.dashboard.recentItems.slice(0, 4)))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.dashboard?.recentItems.length))
                        return;
                    __VLS_ctx.chooseItem(item);
                } },
            key: (item.marketHashName),
            type: "button",
            disabled: (!__VLS_ctx.canEdit),
        });
        (item.displayName);
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "account-picker" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "account-heading" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h3, __VLS_intrinsicElements.h3)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.loadReceivingAccounts(true);
        } },
    type: "button",
    disabled: (__VLS_ctx.accountsBusy),
});
(__VLS_ctx.accountsBusy ? "校验中…" : "重新校验 C5 绑定");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "account-cards" },
});
for (const [account] of __VLS_getVForSourceType((__VLS_ctx.receivingAccounts))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.selectedReceivingAccountId = account.id;
                __VLS_ctx.markDirty();
            } },
        key: (account.id),
        type: "button",
        disabled: (!__VLS_ctx.canEdit || !account.available),
        ...{ class: ({ selected: __VLS_ctx.selectedReceivingAccountId === account.id, unavailable: !account.available }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    (__VLS_ctx.accountInitials(account.name));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (account.name);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (account.steamIdMasked || __VLS_ctx.maskSteamId(account.steamId));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.em, __VLS_intrinsicElements.em)({});
    (account.available ? `C5 已绑定${account.c5Nickname ? ` · ${account.c5Nickname}` : ''}` : "不可用于接收");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.selectedReceivingAccountId === account.id ? "已选择" : "选择");
}
if (!__VLS_ctx.receivingAccounts.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (__VLS_ctx.accountsBusy ? "正在读取五个 Steam 账号…" : "没有读取到可用接收账号");
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "form-body" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "form-fields" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onInput: (__VLS_ctx.markDirty) },
    type: "number",
    min: "0.01",
    step: "0.01",
    disabled: (!__VLS_ctx.canEdit),
});
(__VLS_ctx.maxPrice);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "budget-field" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onInput: (__VLS_ctx.markDirty) },
    type: "number",
    min: "0.01",
    step: "0.01",
    disabled: (!__VLS_ctx.canEdit),
});
(__VLS_ctx.budget);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onInput: (__VLS_ctx.markDirty) },
    type: "number",
    min: "1",
    step: "1",
    disabled: (!__VLS_ctx.canEdit),
});
(__VLS_ctx.targetCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "number",
    disabled: true,
});
(__VLS_ctx.intervalSeconds);
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "calculation" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.formAffordableCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.money(__VLS_ctx.formTargetCost));
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
if (__VLS_ctx.apiError) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "archive-note" },
    });
}
else if (__VLS_ctx.canEdit) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "form-actions" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.apiError))
                    return;
                if (!(__VLS_ctx.canEdit))
                    return;
                __VLS_ctx.saveRound(true);
            } },
        ...{ class: "primary" },
        type: "button",
        disabled: (__VLS_ctx.busy),
    });
    (__VLS_ctx.currentStatus === "paused" ? "保存并继续本轮" : "保存并开始本轮");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!!(__VLS_ctx.apiError))
                    return;
                if (!(__VLS_ctx.canEdit))
                    return;
                __VLS_ctx.saveRound(false);
            } },
        ...{ class: "secondary" },
        type: "button",
        disabled: (__VLS_ctx.busy),
    });
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "archive-note" },
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
    ...{ class: "panel runtime-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
(__VLS_ctx.displayRound ? `第 ${__VLS_ctx.displayRound.roundNumber} 轮 · ${__VLS_ctx.statusText(__VLS_ctx.currentStatus)}` : "等待新轮次");
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "attempt-chip" },
});
(__VLS_ctx.displayRound?.attemptCount ?? 0);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "runtime-content" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "progress-ring" },
    ...{ style: ({ '--progress': `${__VLS_ctx.quantityProgress * 3.6}deg` }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.quantityProgress.toFixed(0));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.displayCounts.delivered);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.displayCounts.pending);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
(__VLS_ctx.displayCounts.failed);
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({
    ...{ class: "blue-text" },
});
(__VLS_ctx.money(__VLS_ctx.displayMoney.remainingBudget));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "wide-track" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({
    ...{ style: ({ width: `${__VLS_ctx.quantityProgress}%` }) },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.displayCounts.delivered);
(__VLS_ctx.displayRound?.targetCount ?? __VLS_ctx.targetCount);
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "runtime-message" },
});
(__VLS_ctx.displayRound?.lastMessage || "保存轮次后才会读取行情；打开页面不会自动购买。");
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "runtime-meta" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.displayRound?.receivingAccountName || __VLS_ctx.selectedReceivingAccount?.name || "未选择");
__VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
(__VLS_ctx.maskSteamId(__VLS_ctx.displayRound?.receivingSteamId || __VLS_ctx.selectedReceivingAccount?.steamId));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dateTime(__VLS_ctx.displayRound?.lastRunAt));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.dateTime(__VLS_ctx.displayRound?.nextRunAt));
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
(__VLS_ctx.stopReasonText(__VLS_ctx.displayRound?.stopReason));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "runtime-actions" },
});
if (__VLS_ctx.currentStatus === 'paused' && __VLS_ctx.displayRound?.stopReason === 'buy_uncertain' && __VLS_ctx.unresolvedCount > 0) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.confirmNotBought) },
        ...{ class: "dark" },
        type: "button",
        disabled: (__VLS_ctx.busy),
    });
    (__VLS_ctx.unresolvedCount);
}
if (__VLS_ctx.currentStatus === 'running') {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.pauseRound) },
        ...{ class: "dark" },
        type: "button",
        disabled: (__VLS_ctx.busy),
    });
}
if (__VLS_ctx.dashboard?.round) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.stopRound) },
        ...{ class: "outline-red" },
        type: "button",
        disabled: (__VLS_ctx.busy || ['completed', 'stopped'].includes(__VLS_ctx.currentStatus)),
    });
}
if (__VLS_ctx.dashboard?.round) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.refreshRound) },
        ...{ class: "secondary" },
        type: "button",
        disabled: (__VLS_ctx.busy),
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel rounds-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.resetNewRound) },
    ...{ class: "new-round" },
    type: "button",
    disabled: (!__VLS_ctx.canCreateNext),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "table-scroll" },
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
__VLS_asFunctionalElement(__VLS_intrinsicElements.th, __VLS_intrinsicElements.th)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
for (const [row] of __VLS_getVForSourceType((__VLS_ctx.dashboard?.rounds))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
        key: (row.id),
        ...{ class: ({ selected: row.id === __VLS_ctx.dashboard?.round?.id && !__VLS_ctx.creatingNew }) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (row.roundNumber);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (row.displayName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (row.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (row.receivingAccountName || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.maskSteamId(row.receivingSteamId));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "status-pill" },
        ...{ class: (row.status) },
    });
    (__VLS_ctx.statusText(row.status));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (__VLS_ctx.money(row.budget));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (__VLS_ctx.money(row.committedAmount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (row.deliveredCount);
    (row.targetCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (__VLS_ctx.money(row.averageAcceptedPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    (__VLS_ctx.dateTime(row.startedAt || row.createdAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.selectRound(row);
            } },
        type: "button",
        ...{ class: "open-round" },
    });
    (row.status === 'running' ? "打开本轮" : "查看详情");
}
if (!__VLS_ctx.dashboard?.rounds.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
        colspan: "10",
        ...{ class: "empty" },
    });
}
if (__VLS_ctx.dashboard?.round) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "details-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel detail-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.dashboard.round.roundNumber);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "detail-scroll" },
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
    __VLS_asFunctionalElement(__VLS_intrinsicElements.tbody, __VLS_intrinsicElements.tbody)({});
    for (const [order] of __VLS_getVForSourceType((__VLS_ctx.dashboard.orders))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({
            key: (order.id),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.dateTime(order.acceptedAt));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (order.receivingAccountName || "—");
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (__VLS_ctx.maskSteamId(order.receivingSteamId));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            ...{ class: "mono" },
        });
        (order.orderAssetId);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (__VLS_ctx.money(order.actualPay));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "order-pill" },
            ...{ class: (order.status) },
        });
        (__VLS_ctx.orderStatus(order.status));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({});
        (order.failedDesc || order.failedCode || "—");
    }
    if (!__VLS_ctx.dashboard.orders.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.tr, __VLS_intrinsicElements.tr)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.td, __VLS_intrinsicElements.td)({
            colspan: "6",
            ...{ class: "empty" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ class: "panel events-panel" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "timeline" },
    });
    for (const [event] of __VLS_getVForSourceType((__VLS_ctx.dashboard.events))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (`${event.at}-${event.status}`),
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
        (__VLS_ctx.dateTime(event.at));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        (event.message);
    }
    if (!__VLS_ctx.dashboard.events.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "empty" },
        });
    }
}
if (__VLS_ctx.confirmationOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.confirmationOpen))
                    return;
                __VLS_ctx.confirmationOpen = false;
            } },
        ...{ class: "modal-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "confirm-modal" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "confirm-heading" },
    });
    /** @type {[typeof OperationVisualAtom, ]} */ ;
    // @ts-ignore
    const __VLS_27 = __VLS_asFunctionalComponent(OperationVisualAtom, new OperationVisualAtom({
        name: "warning",
        size: (52),
    }));
    const __VLS_28 = __VLS_27({
        name: "warning",
        size: (52),
    }, ...__VLS_functionalComponentArgsRest(__VLS_27));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.dashboard?.round?.roundNumber);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    (__VLS_ctx.dashboard?.round?.intervalSeconds);
    (__VLS_ctx.money(__VLS_ctx.dashboard?.round?.maxPrice));
    (__VLS_ctx.money(__VLS_ctx.dashboard?.round?.budget));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "confirm-account" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.accountInitials(__VLS_ctx.dashboard?.round?.receivingAccountName));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
    (__VLS_ctx.dashboard?.round?.receivingAccountName || "未选择");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
    (__VLS_ctx.maskSteamId(__VLS_ctx.dashboard?.round?.receivingSteamId));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onKeyup: (__VLS_ctx.confirmStart) },
        autocomplete: "off",
        placeholder: "开始扫货",
    });
    (__VLS_ctx.confirmation);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.confirmationOpen))
                    return;
                __VLS_ctx.confirmationOpen = false;
            } },
        ...{ class: "secondary" },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.confirmStart) },
        ...{ class: "primary" },
        type: "button",
        disabled: (__VLS_ctx.busy),
    });
}
/** @type {__VLS_StyleScopedClasses['sweeper-page']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--folio-refresh']} */ ;
/** @type {__VLS_StyleScopedClasses['sweeper-page--minimal-v2']} */ ;
/** @type {__VLS_StyleScopedClasses['hero']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-title']} */ ;
/** @type {__VLS_StyleScopedClasses['hero-status']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['api']} */ ;
/** @type {__VLS_StyleScopedClasses['status-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['execution']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['error']} */ ;
/** @type {__VLS_StyleScopedClasses['alert']} */ ;
/** @type {__VLS_StyleScopedClasses['notice']} */ ;
/** @type {__VLS_StyleScopedClasses['metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['item-name']} */ ;
/** @type {__VLS_StyleScopedClasses['account-caption']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['mini-track']} */ ;
/** @type {__VLS_StyleScopedClasses['blue']} */ ;
/** @type {__VLS_StyleScopedClasses['metric-label']} */ ;
/** @type {__VLS_StyleScopedClasses['workspace']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['form-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['round-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['field']} */ ;
/** @type {__VLS_StyleScopedClasses['item-field']} */ ;
/** @type {__VLS_StyleScopedClasses['search-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['suggestions']} */ ;
/** @type {__VLS_StyleScopedClasses['catalog-load-more']} */ ;
/** @type {__VLS_StyleScopedClasses['recent-items']} */ ;
/** @type {__VLS_StyleScopedClasses['account-picker']} */ ;
/** @type {__VLS_StyleScopedClasses['account-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['account-cards']} */ ;
/** @type {__VLS_StyleScopedClasses['form-body']} */ ;
/** @type {__VLS_StyleScopedClasses['form-fields']} */ ;
/** @type {__VLS_StyleScopedClasses['budget-field']} */ ;
/** @type {__VLS_StyleScopedClasses['calculation']} */ ;
/** @type {__VLS_StyleScopedClasses['archive-note']} */ ;
/** @type {__VLS_StyleScopedClasses['form-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['archive-note']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-chip']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-content']} */ ;
/** @type {__VLS_StyleScopedClasses['progress-ring']} */ ;
/** @type {__VLS_StyleScopedClasses['blue-text']} */ ;
/** @type {__VLS_StyleScopedClasses['wide-track']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-message']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-meta']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['dark']} */ ;
/** @type {__VLS_StyleScopedClasses['outline-red']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['rounds-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['new-round']} */ ;
/** @type {__VLS_StyleScopedClasses['table-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['open-round']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['details-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-scroll']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['order-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['events-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-heading']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-account']} */ ;
/** @type {__VLS_StyleScopedClasses['secondary']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            FolioIcon: FolioIcon,
            OperationVisualAtom: OperationVisualAtom,
            dashboard: dashboard,
            apiError: apiError,
            actionError: actionError,
            actionMessage: actionMessage,
            busy: busy,
            creatingNew: creatingNew,
            receivingAccounts: receivingAccounts,
            selectedReceivingAccountId: selectedReceivingAccountId,
            accountsBusy: accountsBusy,
            itemQuery: itemQuery,
            selectedItem: selectedItem,
            itemSuggestions: itemSuggestions,
            itemSearchOpen: itemSearchOpen,
            itemSearchBusy: itemSearchBusy,
            itemSearchHasMore: itemSearchHasMore,
            maxPrice: maxPrice,
            budget: budget,
            targetCount: targetCount,
            intervalSeconds: intervalSeconds,
            confirmationOpen: confirmationOpen,
            confirmation: confirmation,
            loadReceivingAccounts: loadReceivingAccounts,
            searchItems: searchItems,
            onItemInput: onItemInput,
            chooseItem: chooseItem,
            displayRound: displayRound,
            displayCounts: displayCounts,
            displayMoney: displayMoney,
            selectedReceivingAccount: selectedReceivingAccount,
            currentStatus: currentStatus,
            canEdit: canEdit,
            canCreateNext: canCreateNext,
            quantityProgress: quantityProgress,
            budgetProgress: budgetProgress,
            formAffordableCount: formAffordableCount,
            formTargetCost: formTargetCost,
            countdown: countdown,
            priceSafe: priceSafe,
            unresolvedCount: unresolvedCount,
            markDirty: markDirty,
            resetNewRound: resetNewRound,
            saveRound: saveRound,
            confirmStart: confirmStart,
            selectRound: selectRound,
            pauseRound: pauseRound,
            stopRound: stopRound,
            refreshRound: refreshRound,
            confirmNotBought: confirmNotBought,
            statusText: statusText,
            stopReasonText: stopReasonText,
            orderStatus: orderStatus,
            money: money,
            maskSteamId: maskSteamId,
            accountInitials: accountInitials,
            dateTime: dateTime,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
