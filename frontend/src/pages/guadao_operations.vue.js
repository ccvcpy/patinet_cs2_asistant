import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import FolioIcon from "../components/FolioIcon.vue";
import { formatCountdown, formatLocal, responseError, unwrapPayload } from "./guadao_shared";
const steps = ["锁定资产", "Steam 上架", "挂单确认", "Steam 在售", "创建补仓", "C5 订单确认", "闭环"];
const operations = ref([]);
const total = ref(0);
const summary = ref({});
const accountOptions = ref([]);
const itemOptions = ref([]);
const selectedId = ref(null);
const loading = ref(false);
const error = ref("");
const keyword = ref("");
const account = ref("");
const marketHashName = ref("");
const itemSearch = ref("");
const itemMenuOpen = ref(false);
const itemFilterRoot = ref(null);
const status = ref("");
const startAt = ref("");
const endAt = ref("");
const page = ref(1);
const pageSize = ref(10);
const route = useRoute();
const runtime = ref({});
const selectedOperationIds = ref([]);
const batchDialog = ref(null);
const batchSubmitting = ref(false);
const batchError = ref("");
const batchResponse = ref(null);
const batchPrice = ref("");
const batchExecuteNow = ref(true);
const batchConfirmed = ref(false);
const batchReason = ref("C5 当前价格不合适，人工重新冻结补仓价格");
const manualSource = ref("其他平台");
const manualMemo = ref("");
const manualExternalRef = ref("");
const manualCompletedAt = ref("");
keyword.value = String(route.query.q || "");
let timer = null;
const selected = computed(() => operations.value.find(row => row.id === selectedId.value) || operations.value[0] || null);
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const accounts = computed(() => [...new Set([...accountOptions.value, ...operations.value.map(row => row.accountName).filter(Boolean)])]);
const selectedItem = computed(() => itemOptions.value.find(row => row.marketHashName === marketHashName.value) || null);
const itemOptionTotal = computed(() => itemOptions.value.reduce((totalCount, row) => totalCount + Number(row.count || 0), 0));
const filteredItemOptions = computed(() => {
    const queryText = itemSearch.value.trim().toLocaleLowerCase();
    if (!queryText)
        return itemOptions.value;
    return itemOptions.value.filter(row => `${row.displayName || ""} ${row.marketHashName}`.toLocaleLowerCase().includes(queryText));
});
const batchModeEnabled = computed(() => status.value === "sold");
const selectedOperations = computed(() => operations.value.filter(row => selectedOperationIds.value.includes(Number(row.id))));
const selectablePageOperations = computed(() => operations.value.filter(row => row.status === "sold" && row.batchActionEligible));
const selectedMarketNames = computed(() => [...new Set(selectedOperations.value.map(row => row.marketHashName).filter(Boolean))]);
const batchSameItem = computed(() => selectedMarketNames.value.length === 1);
const selectedSteamNetTotal = computed(() => selectedOperations.value.reduce((sum, row) => sum + Number(row.steamNetAmount || 0), 0));
const allSelectableSelected = computed(() => selectablePageOperations.value.length > 0 && selectablePageOperations.value.every(row => selectedOperationIds.value.includes(Number(row.id))));
const previewPrice = computed(() => Number(batchPrice.value));
const ratioPreviews = computed(() => selectedOperations.value.map(row => ({ id: Number(row.id), oldRatio: row.currentRebuyRatio ?? row.maxRebuyRatioAtOpen ?? row.listingRatioAtOpen ?? null, newRatio: previewPrice.value > 0 && Number(row.steamNetAmount) > 0 ? previewPrice.value / Number(row.steamNetAmount) : null })));
const ratioPreviewRange = computed(() => { const values = ratioPreviews.value.map(row => row.newRatio).filter((value) => value != null); if (!values.length)
    return "—"; const min = Math.min(...values); const max = Math.max(...values); return Math.abs(max - min) < 0.000001 ? pct(min) : `${pct(min)} ～ ${pct(max)}`; });
const metricCards = computed(() => [
    ["全部", summary.value.total ?? total.value], ["挂单待确认", summary.value.pendingConfirmation ?? 0], ["Steam 在售", summary.value.steamListed ?? 0], ["已卖出待补仓", summary.value.pendingRebuy ?? 0], ["C5 补仓待查证据", summary.value.c5EvidencePending ?? summary.value.submissionUnconfirmed ?? 0], ["C5 已购买待收货", summary.value.deliveryPending ?? 0], ["已闭环", summary.value.completed ?? 0],
]);
function pct(value) { return value == null ? "—" : `${(value * (value <= 1 ? 100 : 1)).toFixed(2)}%`; }
function money(value) { return value == null ? "—" : `¥ ${Number(value).toFixed(2)}`; }
function stageTone(row) { if (row.status === "completed")
    return "success"; if (row.status?.includes("failed") || row.status === "manual_required")
    return "danger"; return "warning"; }
function showRebuyFailureHistory(row) { return Boolean(row && ["sold", "delivery_pending"].includes(row.status || "") && (row.failedRebuyCount || 0) > 0); }
function attemptTone(attempt) { if (attempt.status === "completed")
    return "success"; if (attempt.status?.includes("failed"))
    return "danger"; return "warning"; }
function attemptPrice(attempt) { return money(attempt.actualPrice ?? attempt.expectedPrice); }
function relatedLogsTo(row) { return { path: "/guadao/logs", query: { operationId: String(row.operationId || row.id), marketHashName: row.marketHashName || "", account: row.accountName || "" } }; }
function itemLabel(row) { return row?.displayName || row?.marketHashName || "全部挂刀物品"; }
function selectItem(value) { marketHashName.value = value; itemSearch.value = ""; itemMenuOpen.value = false; }
function handleDocumentPointer(event) { if (!itemFilterRoot.value?.contains(event.target))
    itemMenuOpen.value = false; }
function localDateTimeValue(date = new Date()) { const offset = date.getTimezoneOffset() * 60000; return new Date(date.getTime() - offset).toISOString().slice(0, 19); }
function requestId() { return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`; }
function isSelected(row) { return selectedOperationIds.value.includes(Number(row.id)); }
function toggleOperation(row) { if (!batchModeEnabled.value || !row.batchActionEligible)
    return; const id = Number(row.id); selectedOperationIds.value = isSelected(row) ? selectedOperationIds.value.filter(value => value !== id) : [...selectedOperationIds.value, id]; }
function togglePageSelection() { const ids = selectablePageOperations.value.map(row => Number(row.id)); selectedOperationIds.value = allSelectableSelected.value ? selectedOperationIds.value.filter(id => !ids.includes(id)) : [...new Set([...selectedOperationIds.value, ...ids])]; }
function clearBatchSelection() { selectedOperationIds.value = []; batchDialog.value = null; batchConfirmed.value = false; batchError.value = ""; }
function openBatch(kind) { if (!selectedOperations.value.length || !batchSameItem.value)
    return; const defaultPrice = Math.max(...selectedOperations.value.map(row => Number(row.frozenRebuyPrice || row.c5RebuyPrice || 0))); batchPrice.value = defaultPrice > 0 ? defaultPrice.toFixed(2) : ""; batchReason.value = kind === "manual" ? "C5 当前价格不合适，已在其他平台真实完成补仓" : "C5 当前价格不合适，人工重新冻结补仓价格"; manualCompletedAt.value = localDateTimeValue(); batchConfirmed.value = false; batchError.value = ""; batchDialog.value = kind; }
async function submitBatch() { if (!batchDialog.value || !batchConfirmed.value || batchSubmitting.value)
    return; batchSubmitting.value = true; batchError.value = ""; try {
    const isManual = batchDialog.value === "manual";
    const endpoint = isManual ? "/api/guadao/operations/batch-manual-complete" : "/api/guadao/operations/batch-refreeze-rebuy";
    const body = isManual ? { operationIds: selectedOperationIds.value, actualRebuyPrice: Number(batchPrice.value), source: manualSource.value, completedAt: new Date(manualCompletedAt.value).toISOString(), memo: manualMemo.value, externalOrderRef: manualExternalRef.value, confirmed: true, requestId: requestId(), reason: batchReason.value } : { operationIds: selectedOperationIds.value, rebuyPrice: Number(batchPrice.value), executeNow: batchExecuteNow.value, confirmed: true, requestId: requestId(), reason: batchReason.value };
    const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!response.ok)
        throw new Error(await responseError(response));
    batchResponse.value = unwrapPayload(await response.json());
    batchDialog.value = null;
    selectedOperationIds.value = [];
    await refresh();
}
catch (reason) {
    batchError.value = reason instanceof Error ? reason.message : String(reason);
}
finally {
    batchSubmitting.value = false;
} }
function closeBatchResult() { batchResponse.value = null; }
const runtimeText = computed(() => { const value = String(runtime.value?.runtimeStatus || runtime.value?.status || ""); if (value === "closing_only")
    return "存量闭环中"; if (value === "preparing")
    return "启动准备中"; return runtime.value?.enabled ? "运行中" : "已关闭"; });
async function refresh() {
    loading.value = true;
    try {
        const params = new URLSearchParams({ page: String(page.value), pageSize: String(pageSize.value) });
        if (keyword.value.trim())
            params.set("q", keyword.value.trim());
        if (account.value)
            params.set("account", account.value);
        if (marketHashName.value)
            params.set("marketHashName", marketHashName.value);
        if (status.value)
            params.set("status", status.value);
        if (startAt.value)
            params.set("startAt", new Date(startAt.value).toISOString());
        if (endAt.value)
            params.set("endAt", new Date(endAt.value).toISOString());
        const response = await fetch(`/api/guadao/operations?${params}`, { cache: "no-store" });
        if (!response.ok)
            throw new Error(await responseError(response));
        const data = unwrapPayload(await response.json());
        operations.value = data.operations || [];
        total.value = Number(data.total ?? operations.value.length);
        summary.value = data.summary || {};
        itemOptions.value = data.itemOptions || [];
        runtime.value = data.runtime || {};
        accountOptions.value = (data.accounts || []).map(row => row.name || row.id || "").filter(Boolean);
        selectedOperationIds.value = selectedOperationIds.value.filter(id => operations.value.some(row => Number(row.id) === id && row.status === "sold" && row.batchActionEligible));
        if (selectedId.value == null || !operations.value.some(row => row.id === selectedId.value))
            selectedId.value = operations.value[0]?.id ?? null;
        error.value = "";
    }
    catch (reason) {
        error.value = reason instanceof Error ? reason.message : String(reason);
    }
    finally {
        loading.value = false;
    }
}
function query() { page.value = 1; void refresh(); }
function startPolling() { if (timer === null)
    timer = setInterval(() => void refresh(), 15000); }
function stopPolling() { if (timer !== null)
    clearInterval(timer); timer = null; }
watch(() => route.query.q, value => { const next = String(value || ""); if (next === keyword.value)
    return; keyword.value = next; query(); });
watch(status, value => { if (value !== "sold")
    clearBatchSelection(); });
onMounted(() => { keyword.value = String(route.query.q || ""); document.addEventListener("pointerdown", handleDocumentPointer); void refresh(); startPolling(); });
onActivated(startPolling);
onDeactivated(stopPolling);
onUnmounted(() => { stopPolling(); document.removeEventListener("pointerdown", handleDocumentPointer); });
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['page-title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['page-title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-link']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['filters']} */ ;
/** @type {__VLS_StyleScopedClasses['filters']} */ ;
/** @type {__VLS_StyleScopedClasses['filters']} */ ;
/** @type {__VLS_StyleScopedClasses['filters']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-values']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['status-pill']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-values']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-values']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-values']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-values']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['done']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['done']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['done']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['next-task']} */ ;
/** @type {__VLS_StyleScopedClasses['next-task']} */ ;
/** @type {__VLS_StyleScopedClasses['next-task']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-link']} */ ;
/** @type {__VLS_StyleScopedClasses['filters']} */ ;
/** @type {__VLS_StyleScopedClasses['item-filter']} */ ;
/** @type {__VLS_StyleScopedClasses['date-range']} */ ;
/** @type {__VLS_StyleScopedClasses['item-select']} */ ;
/** @type {__VLS_StyleScopedClasses['item-select']} */ ;
/** @type {__VLS_StyleScopedClasses['item-select']} */ ;
/** @type {__VLS_StyleScopedClasses['item-menu']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['rebuy-failure-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['rebuy-failure-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['rebuy-attempt-history']} */ ;
/** @type {__VLS_StyleScopedClasses['rebuy-attempt-history']} */ ;
/** @type {__VLS_StyleScopedClasses['rebuy-attempt-history']} */ ;
/** @type {__VLS_StyleScopedClasses['rebuy-attempt-history']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-list']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-list']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-head']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-list']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-list']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-list']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-list']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-error']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-error']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-link']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-select-all']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-select-all']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-title']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-cancel']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['warning']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['warning']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-cancel']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-cancel']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-cancel']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-row']} */ ;
/** @type {__VLS_StyleScopedClasses['selected']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-selected']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-title']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-title']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-title']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-title']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-comparison']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-comparison']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-comparison']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-comparison']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-comparison']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-comparison']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['toggle-line']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-line']} */ ;
/** @type {__VLS_StyleScopedClasses['toggle-line']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-line']} */ ;
/** @type {__VLS_StyleScopedClasses['toggle-line']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-line']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['toggle-line']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-line']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-safe-note']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-safe-note']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-safe-note']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['api-error']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['success']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['success']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['failed']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-cancel']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-workbench']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-values']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-values']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.main, __VLS_intrinsicElements.main)({
    ...{ class: "page operations-page" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({
    ...{ class: "page-title-row" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
    ...{ class: "eyebrow" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.h1, __VLS_intrinsicElements.h1)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
const __VLS_0 = {}.RouterLink;
/** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    ...{ class: "runtime-link" },
    to: "/guadao/overview",
}));
const __VLS_2 = __VLS_1({
    ...{ class: "runtime-link" },
    to: "/guadao/overview",
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
__VLS_3.slots.default;
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.runtimeText);
var __VLS_3;
if (__VLS_ctx.error) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "api-error" },
    });
    (__VLS_ctx.error);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "operation-metrics" },
});
for (const [metric] of __VLS_getVForSourceType((__VLS_ctx.metricCards))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        key: (String(metric[0])),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (metric[0]);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (metric[1]);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "panel filters" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "filter-keyword" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    ...{ onKeyup: (__VLS_ctx.query) },
    placeholder: "物品 / 账号 / listingId / assetId",
});
(__VLS_ctx.keyword);
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "filter-account" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.account),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
for (const [name] of __VLS_getVForSourceType((__VLS_ctx.accounts))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
        key: (name),
        value: (name),
    });
    (name);
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ref: "itemFilterRoot",
    ...{ class: "item-filter" },
});
/** @type {typeof __VLS_ctx.itemFilterRoot} */ ;
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "item-select-wrap" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (...[$event]) => {
            __VLS_ctx.itemMenuOpen = !__VLS_ctx.itemMenuOpen;
        } },
    ...{ class: "item-select" },
    type: "button",
    'aria-expanded': (__VLS_ctx.itemMenuOpen),
    'aria-haspopup': "listbox",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
(__VLS_ctx.itemLabel(__VLS_ctx.selectedItem));
/** @type {[typeof FolioIcon, ]} */ ;
// @ts-ignore
const __VLS_4 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
    name: (__VLS_ctx.itemMenuOpen ? 'chevron-up' : 'chevron-down'),
    size: (14),
}));
const __VLS_5 = __VLS_4({
    name: (__VLS_ctx.itemMenuOpen ? 'chevron-up' : 'chevron-down'),
    size: (14),
}, ...__VLS_functionalComponentArgsRest(__VLS_4));
if (__VLS_ctx.itemMenuOpen) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onKeydown: (...[$event]) => {
                if (!(__VLS_ctx.itemMenuOpen))
                    return;
                __VLS_ctx.itemMenuOpen = false;
            } },
        ...{ class: "item-menu" },
        role: "listbox",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        type: "search",
        placeholder: "输入中文名或 marketHashName 搜索",
        autofocus: true,
    });
    (__VLS_ctx.itemSearch);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "item-options" },
    });
    if (!__VLS_ctx.itemSearch.trim()) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.itemMenuOpen))
                        return;
                    if (!(!__VLS_ctx.itemSearch.trim()))
                        return;
                    __VLS_ctx.selectItem('');
                } },
            type: "button",
            ...{ class: ({ selected: !__VLS_ctx.marketHashName }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (__VLS_ctx.itemOptionTotal);
    }
    for (const [option] of __VLS_getVForSourceType((__VLS_ctx.filteredItemOptions))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.itemMenuOpen))
                        return;
                    __VLS_ctx.selectItem(option.marketHashName);
                } },
            key: (option.marketHashName),
            type: "button",
            ...{ class: ({ selected: __VLS_ctx.marketHashName === option.marketHashName }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (__VLS_ctx.itemLabel(option));
        if (option.displayName && option.displayName !== option.marketHashName) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
            (option.marketHashName);
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.b, __VLS_intrinsicElements.b)({});
        (option.count);
    }
    if (__VLS_ctx.itemSearch.trim() && !__VLS_ctx.filteredItemOptions.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "filter-status" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
    value: (__VLS_ctx.status),
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "listing_pending",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "listed",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "sold",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "c5_submission_unconfirmed",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "delivery_pending",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "completed",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.option, __VLS_intrinsicElements.option)({
    value: "manual_required",
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
    ...{ class: "filter-date" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "date-range" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "datetime-local",
});
(__VLS_ctx.startAt);
__VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
__VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
    type: "datetime-local",
});
(__VLS_ctx.endAt);
__VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
    ...{ onClick: (__VLS_ctx.query) },
    ...{ class: "primary-button filter-submit" },
    type: "button",
    disabled: (__VLS_ctx.loading),
});
(__VLS_ctx.loading ? "查询中…" : "查询流水");
if (__VLS_ctx.batchModeEnabled) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "panel batch-toolbar" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "batch-select-all" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        ...{ onChange: (__VLS_ctx.togglePageSelection) },
        type: "checkbox",
        checked: (__VLS_ctx.allSelectableSelected),
        disabled: (!__VLS_ctx.selectablePageOperations.length),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "batch-summary" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.selectedOperations.length);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.selectedOperations.length ? (__VLS_ctx.batchSameItem ? `${__VLS_ctx.selectedOperations[0]?.displayName || __VLS_ctx.selectedOperations[0]?.marketHashName} · 同一品类` : "已选择多个品类，不能共用一个补仓价") : "只可选择已卖出待补仓且无 C5 未决订单的流水");
    if (__VLS_ctx.selectedOperations.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (__VLS_ctx.money(__VLS_ctx.selectedSteamNetTotal));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.batchModeEnabled))
                    return;
                __VLS_ctx.openBatch('refreeze');
            } },
        ...{ class: "batch-button primary" },
        type: "button",
        disabled: (!__VLS_ctx.selectedOperations.length || !__VLS_ctx.batchSameItem),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.batchModeEnabled))
                    return;
                __VLS_ctx.openBatch('manual');
            } },
        ...{ class: "batch-button warning" },
        type: "button",
        disabled: (!__VLS_ctx.selectedOperations.length || !__VLS_ctx.batchSameItem),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.clearBatchSelection) },
        ...{ class: "batch-cancel" },
        type: "button",
        disabled: (!__VLS_ctx.selectedOperations.length),
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
    ...{ class: "operation-workbench" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "panel operation-list" },
});
for (const [row] of __VLS_getVForSourceType((__VLS_ctx.operations))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.selectedId = row.id;
            } },
        key: (row.id),
        ...{ class: (['operation-row', { selected: __VLS_ctx.selected?.id === row.id, 'batch-selected': __VLS_ctx.isSelected(row) }]) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "operation-summary" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "operation-title" },
    });
    if (__VLS_ctx.batchModeEnabled) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            ...{ onClick: (...[$event]) => {
                    if (!(__VLS_ctx.batchModeEnabled))
                        return;
                    __VLS_ctx.toggleOperation(row);
                } },
            type: "checkbox",
            checked: (__VLS_ctx.isSelected(row)),
            disabled: (!row.batchActionEligible),
            title: (row.batchActionBlockReason || '选择此流水'),
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (row.displayName || row.marketHashName || "未命名饰品");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (row.accountName || "账号未记录");
    if (__VLS_ctx.batchModeEnabled && !row.batchActionEligible) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        (row.batchActionBlockReason);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: (['status-pill', __VLS_ctx.stageTone(row)]) },
    });
    (row.stage || row.status || "状态未知");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "operation-values" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.pct(row.currentRebuyRatio ?? row.listingRatioAtOpen));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.money(row.frozenRebuyPrice));
    (__VLS_ctx.pct(row.maxRebuyRatioAtOpen));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (row.manualRebuyRefrozenAt ? "人工重新冻结" : (row.ratioRuleSource || "全局"));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (row.nextTaskLabel || __VLS_ctx.formatCountdown(row.nextAttemptAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
    (__VLS_ctx.formatLocal(row.updatedAt));
    if (__VLS_ctx.showRebuyFailureHistory(row)) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "rebuy-failure-banner" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (row.failedRebuyCount);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (row.rebuyAttemptCount);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "stepper" },
        'aria-label': (`${row.displayName || row.marketHashName} 执行进度`),
    });
    for (const [label, index] of __VLS_getVForSourceType((__VLS_ctx.steps))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (label),
            ...{ class: ({ done: (row.stepIndex || 0) > index, active: (row.stepIndex || 0) === index }) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
        ((row.stepIndex || 0) > index ? "✓" : index + 1);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (label);
    }
}
if (!__VLS_ctx.operations.length) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
    (__VLS_ctx.loading ? "正在读取流水…" : "当前筛选没有后端流水记录。");
}
if (__VLS_ctx.total) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({
        ...{ class: "pagination" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.total);
    (__VLS_ctx.page);
    (__VLS_ctx.pageCount);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.select, __VLS_intrinsicElements.select)({
        ...{ onChange: (...[$event]) => {
                if (!(__VLS_ctx.total))
                    return;
                __VLS_ctx.page = 1;
                __VLS_ctx.refresh();
            } },
        value: (__VLS_ctx.pageSize),
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
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.total))
                    return;
                __VLS_ctx.page--;
                __VLS_ctx.refresh();
            } },
        disabled: (__VLS_ctx.page <= 1),
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.total))
                    return;
                __VLS_ctx.page++;
                __VLS_ctx.refresh();
            } },
        disabled: (__VLS_ctx.page >= __VLS_ctx.pageCount),
    });
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.aside, __VLS_intrinsicElements.aside)({
    ...{ class: "panel operation-detail" },
});
if (__VLS_ctx.selected) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "detail-head" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.selected.displayName || __VLS_ctx.selected.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "mono" },
    });
    (__VLS_ctx.selected.operationId || __VLS_ctx.selected.id);
    if (__VLS_ctx.isSelected(__VLS_ctx.selected)) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({
            ...{ class: "batch-detail-tag" },
        });
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: (['status-pill', __VLS_ctx.stageTone(__VLS_ctx.selected)]) },
    });
    (__VLS_ctx.selected.stage || __VLS_ctx.selected.status);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({
        ...{ class: "detail-grid" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.assetId || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.accountName || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.steamId || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.listingId || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.c5OrderId || "未生成");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.c5TradeOrderId || "未生成");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.c5OutTradeNo || "—");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.selected.manualRebuyRefrozenAt ? "人工重新冻结" : (__VLS_ctx.selected.ratioRuleSource || "全局"));
    (__VLS_ctx.selected.ratioRuleId ? ` · ${__VLS_ctx.selected.ratioRuleId}` : "");
    (__VLS_ctx.selected.ratioRuleVersion ? ` · v${__VLS_ctx.selected.ratioRuleVersion}` : "");
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.pct(__VLS_ctx.selected.listingRatioAtOpen));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.pct(__VLS_ctx.selected.currentRebuyRatio ?? __VLS_ctx.selected.maxRebuyRatioAtOpen));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.selected.frozenRebuyPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.selected.steamListPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.selected.steamNetAmount));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.money(__VLS_ctx.selected.c5RebuyPrice));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.formatLocal(__VLS_ctx.selected.steamSoldAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.formatLocal(__VLS_ctx.selected.c5OrderSubmittedAt));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
    (__VLS_ctx.formatLocal(__VLS_ctx.selected.c5DeliveryDeadlineAt));
    if (__VLS_ctx.showRebuyFailureHistory(__VLS_ctx.selected)) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
            ...{ class: "rebuy-attempt-history" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (__VLS_ctx.selected.failedRebuyCount);
        (__VLS_ctx.selected.rebuyAttemptCount);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "attempt-list" },
        });
        for (const [attempt] of __VLS_getVForSourceType((__VLS_ctx.selected.rebuyAttempts))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
                key: (attempt.id),
                ...{ class: ({ current: attempt.isCurrent }) },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
                ...{ class: "attempt-head" },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (attempt.operationId || `GD-${attempt.id}`);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: (['status-pill', __VLS_ctx.attemptTone(attempt)]) },
            });
            (attempt.isCurrent ? `当前 · ${attempt.stage}` : attempt.stage);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dl, __VLS_intrinsicElements.dl)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (attempt.c5OrderId || "未生成");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.attemptPrice(attempt));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (__VLS_ctx.formatLocal(attempt.c5OrderSubmittedAt));
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dt, __VLS_intrinsicElements.dt)({});
            (attempt.failureReason ? "失败时间" : "替换上限");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.dd, __VLS_intrinsicElements.dd)({});
            (attempt.failureReason ? __VLS_ctx.formatLocal(attempt.failureAt) : __VLS_ctx.money(attempt.replacementMaxPrice));
            if (attempt.failureReason) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                    ...{ class: "attempt-error" },
                });
                __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
                (attempt.failureReason);
                if (attempt.failureCode) {
                    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
                    (attempt.failureCode);
                }
                if (attempt.replacementOperationId) {
                    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
                    (attempt.replacementOperationId);
                }
            }
            else if (attempt.replacementForOperationId) {
                __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
                    ...{ class: "attempt-link" },
                });
                (attempt.replacementForOperationId);
                (__VLS_ctx.money(attempt.replacementMaxPrice));
            }
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "timeline" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    if (__VLS_ctx.selected.timeline?.length) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        for (const [event, index] of __VLS_getVForSourceType((__VLS_ctx.selected.timeline))) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
                key: (`${event.at}-${index}`),
                ...{ class: (event.status) },
            });
            __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
            __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
            (event.label || "状态更新");
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (event.detail);
            __VLS_asFunctionalElement(__VLS_intrinsicElements.time, __VLS_intrinsicElements.time)({});
            (__VLS_ctx.formatLocal(event.at));
        }
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    const __VLS_7 = {}.RouterLink;
    /** @type {[typeof __VLS_components.RouterLink, typeof __VLS_components.RouterLink, ]} */ ;
    // @ts-ignore
    const __VLS_8 = __VLS_asFunctionalComponent(__VLS_7, new __VLS_7({
        ...{ class: "related-log-link" },
        to: (__VLS_ctx.relatedLogsTo(__VLS_ctx.selected)),
    }));
    const __VLS_9 = __VLS_8({
        ...{ class: "related-log-link" },
        to: (__VLS_ctx.relatedLogsTo(__VLS_ctx.selected)),
    }, ...__VLS_functionalComponentArgsRest(__VLS_8));
    __VLS_10.slots.default;
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_11 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "clock",
        size: (13),
    }));
    const __VLS_12 = __VLS_11({
        name: "clock",
        size: (13),
    }, ...__VLS_functionalComponentArgsRest(__VLS_11));
    var __VLS_10;
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "next-task" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.selected.nextTaskLabel || "尚未安排");
    (__VLS_ctx.formatCountdown(__VLS_ctx.selected.nextAttemptAt));
    if (__VLS_ctx.selected.nextTaskReason) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
        (__VLS_ctx.selected.nextTaskReason);
    }
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty-state" },
    });
}
if (__VLS_ctx.batchDialog) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.batchDialog))
                    return;
                __VLS_ctx.batchDialog = null;
            } },
        ...{ class: "modal-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "batch-modal panel" },
        role: "dialog",
        'aria-modal': "true",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "eyebrow" },
    });
    (__VLS_ctx.batchDialog === 'refreeze' ? 'Refreeze & Retry' : 'Manual External Completion');
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    (__VLS_ctx.batchDialog === 'refreeze' ? '批量重设补仓价并执行' : '批量手动完结');
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.selectedOperations.length);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.batchDialog))
                    return;
                __VLS_ctx.batchDialog = null;
            } },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "batch-modal-summary" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.selectedOperations[0]?.displayName || __VLS_ctx.selectedOperations[0]?.marketHashName);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.money(__VLS_ctx.selectedSteamNetTotal));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.batchDialog === 'refreeze' ? '新的冻结补仓单价' : '其他平台实际补仓单价');
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        type: "number",
        min: "0.01",
        step: "0.01",
        placeholder: "0.00",
    });
    (__VLS_ctx.batchPrice);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "ratio-comparison" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.pct(__VLS_ctx.selectedOperations[0]?.currentRebuyRatio ?? __VLS_ctx.selectedOperations[0]?.maxRebuyRatioAtOpen));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.i, __VLS_intrinsicElements.i)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.batchDialog === 'refreeze' ? '新冻结比例' : '实际闭环比例');
    __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
    (__VLS_ctx.ratioPreviewRange);
    if (__VLS_ctx.batchDialog === 'refreeze') {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
            ...{ class: "toggle-line" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            type: "checkbox",
        });
        (__VLS_ctx.batchExecuteNow);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "batch-warning" },
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_14 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "warning",
            size: (16),
        }));
        const __VLS_15 = __VLS_14({
            name: "warning",
            size: (16),
        }, ...__VLS_functionalComponentArgsRest(__VLS_14));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    else {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            placeholder: "其他平台",
        });
        (__VLS_ctx.manualSource);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            type: "datetime-local",
            step: "1",
        });
        (__VLS_ctx.manualCompletedAt);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
            placeholder: "与备注至少填写一项",
        });
        (__VLS_ctx.manualExternalRef);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.textarea, __VLS_intrinsicElements.textarea)({
            value: (__VLS_ctx.manualMemo),
            rows: "3",
            placeholder: "购买平台、成交方式或其他可核对信息",
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "batch-safe-note" },
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_17 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: "shield",
            size: (16),
        }));
        const __VLS_18 = __VLS_17({
            name: "shield",
            size: (16),
        }, ...__VLS_functionalComponentArgsRest(__VLS_17));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({});
    (__VLS_ctx.batchReason);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.label, __VLS_intrinsicElements.label)({
        ...{ class: "confirm-line" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.input)({
        type: "checkbox",
    });
    (__VLS_ctx.batchConfirmed);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.batchDialog === 'refreeze' ? `我确认按新价格和新比例重新冻结 ${__VLS_ctx.selectedOperations.length} 笔流水` : `我确认这些物品已在其他平台真实完成补仓`);
    if (__VLS_ctx.batchError) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
            ...{ class: "api-error" },
        });
        (__VLS_ctx.batchError);
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (...[$event]) => {
                if (!(__VLS_ctx.batchDialog))
                    return;
                __VLS_ctx.batchDialog = null;
            } },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.submitBatch) },
        ...{ class: "primary-button" },
        type: "button",
        disabled: (__VLS_ctx.batchSubmitting || !__VLS_ctx.batchConfirmed || !(Number(__VLS_ctx.batchPrice) > 0) || (__VLS_ctx.batchDialog === 'manual' && (!__VLS_ctx.manualSource.trim() || (!__VLS_ctx.manualMemo.trim() && !__VLS_ctx.manualExternalRef.trim())))),
    });
    (__VLS_ctx.batchSubmitting ? '提交中…' : (__VLS_ctx.batchDialog === 'refreeze' ? `确认重设 ${__VLS_ctx.selectedOperations.length} 笔` : `确认手动完结 ${__VLS_ctx.selectedOperations.length} 笔`));
}
if (__VLS_ctx.batchResponse) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (__VLS_ctx.closeBatchResult) },
        ...{ class: "modal-backdrop" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.section, __VLS_intrinsicElements.section)({
        ...{ class: "batch-modal result-modal panel" },
        role: "dialog",
        'aria-modal': "true",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.header, __VLS_intrinsicElements.header)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "eyebrow" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.h2, __VLS_intrinsicElements.h2)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
    (__VLS_ctx.batchResponse.batchId);
    (__VLS_ctx.batchResponse.successCount || 0);
    (__VLS_ctx.batchResponse.failedCount || 0);
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.closeBatchResult) },
        type: "button",
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "batch-results" },
    });
    for (const [result] of __VLS_getVForSourceType((__VLS_ctx.batchResponse.results))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.article, __VLS_intrinsicElements.article)({
            key: (`${result.operationId}-${result.code}`),
            ...{ class: (result.ok ? 'success' : 'failed') },
        });
        /** @type {[typeof FolioIcon, ]} */ ;
        // @ts-ignore
        const __VLS_20 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
            name: (result.ok ? 'success' : 'warning'),
            size: (16),
        }));
        const __VLS_21 = __VLS_20({
            name: (result.ok ? 'success' : 'warning'),
            size: (16),
        }, ...__VLS_functionalComponentArgsRest(__VLS_20));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({});
        __VLS_asFunctionalElement(__VLS_intrinsicElements.strong, __VLS_intrinsicElements.strong)({});
        (result.tradeNo || `GD-${result.operationId}`);
        (result.message);
        if (result.newFrozenRebuyPrice) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.money(result.newFrozenRebuyPrice));
            (__VLS_ctx.pct(result.newFrozenRebuyRatio));
        }
        else if (result.actualRebuyPrice) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
            (__VLS_ctx.money(result.actualRebuyPrice));
            (__VLS_ctx.pct(result.actualRebuyRatio));
        }
        if (result.idempotentReplay) {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.small, __VLS_intrinsicElements.small)({});
        }
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "batch-safe-note" },
    });
    /** @type {[typeof FolioIcon, ]} */ ;
    // @ts-ignore
    const __VLS_23 = __VLS_asFunctionalComponent(FolioIcon, new FolioIcon({
        name: "clock",
        size: (16),
    }));
    const __VLS_24 = __VLS_23({
        name: "clock",
        size: (16),
    }, ...__VLS_functionalComponentArgsRest(__VLS_23));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.footer, __VLS_intrinsicElements.footer)({});
    __VLS_asFunctionalElement(__VLS_intrinsicElements.button, __VLS_intrinsicElements.button)({
        ...{ onClick: (__VLS_ctx.closeBatchResult) },
        ...{ class: "primary-button" },
        type: "button",
    });
}
/** @type {__VLS_StyleScopedClasses['page']} */ ;
/** @type {__VLS_StyleScopedClasses['operations-page']} */ ;
/** @type {__VLS_StyleScopedClasses['page-title-row']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['runtime-link']} */ ;
/** @type {__VLS_StyleScopedClasses['api-error']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-metrics']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['filters']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-keyword']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-account']} */ ;
/** @type {__VLS_StyleScopedClasses['item-filter']} */ ;
/** @type {__VLS_StyleScopedClasses['item-select-wrap']} */ ;
/** @type {__VLS_StyleScopedClasses['item-select']} */ ;
/** @type {__VLS_StyleScopedClasses['item-menu']} */ ;
/** @type {__VLS_StyleScopedClasses['item-options']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-status']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-date']} */ ;
/** @type {__VLS_StyleScopedClasses['date-range']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['filter-submit']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-toolbar']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-select-all']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['primary']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-button']} */ ;
/** @type {__VLS_StyleScopedClasses['warning']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-cancel']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-workbench']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-list']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-title']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-values']} */ ;
/** @type {__VLS_StyleScopedClasses['rebuy-failure-banner']} */ ;
/** @type {__VLS_StyleScopedClasses['stepper']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['pagination']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['operation-detail']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-head']} */ ;
/** @type {__VLS_StyleScopedClasses['mono']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-detail-tag']} */ ;
/** @type {__VLS_StyleScopedClasses['detail-grid']} */ ;
/** @type {__VLS_StyleScopedClasses['rebuy-attempt-history']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-list']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-head']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-error']} */ ;
/** @type {__VLS_StyleScopedClasses['attempt-link']} */ ;
/** @type {__VLS_StyleScopedClasses['timeline']} */ ;
/** @type {__VLS_StyleScopedClasses['related-log-link']} */ ;
/** @type {__VLS_StyleScopedClasses['next-task']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-state']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal-summary']} */ ;
/** @type {__VLS_StyleScopedClasses['ratio-comparison']} */ ;
/** @type {__VLS_StyleScopedClasses['toggle-line']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-warning']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-safe-note']} */ ;
/** @type {__VLS_StyleScopedClasses['confirm-line']} */ ;
/** @type {__VLS_StyleScopedClasses['api-error']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
/** @type {__VLS_StyleScopedClasses['modal-backdrop']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['result-modal']} */ ;
/** @type {__VLS_StyleScopedClasses['panel']} */ ;
/** @type {__VLS_StyleScopedClasses['eyebrow']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-results']} */ ;
/** @type {__VLS_StyleScopedClasses['batch-safe-note']} */ ;
/** @type {__VLS_StyleScopedClasses['primary-button']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            RouterLink: RouterLink,
            FolioIcon: FolioIcon,
            formatCountdown: formatCountdown,
            formatLocal: formatLocal,
            steps: steps,
            operations: operations,
            total: total,
            selectedId: selectedId,
            loading: loading,
            error: error,
            keyword: keyword,
            account: account,
            marketHashName: marketHashName,
            itemSearch: itemSearch,
            itemMenuOpen: itemMenuOpen,
            itemFilterRoot: itemFilterRoot,
            status: status,
            startAt: startAt,
            endAt: endAt,
            page: page,
            pageSize: pageSize,
            batchDialog: batchDialog,
            batchSubmitting: batchSubmitting,
            batchError: batchError,
            batchResponse: batchResponse,
            batchPrice: batchPrice,
            batchExecuteNow: batchExecuteNow,
            batchConfirmed: batchConfirmed,
            batchReason: batchReason,
            manualSource: manualSource,
            manualMemo: manualMemo,
            manualExternalRef: manualExternalRef,
            manualCompletedAt: manualCompletedAt,
            selected: selected,
            pageCount: pageCount,
            accounts: accounts,
            selectedItem: selectedItem,
            itemOptionTotal: itemOptionTotal,
            filteredItemOptions: filteredItemOptions,
            batchModeEnabled: batchModeEnabled,
            selectedOperations: selectedOperations,
            selectablePageOperations: selectablePageOperations,
            batchSameItem: batchSameItem,
            selectedSteamNetTotal: selectedSteamNetTotal,
            allSelectableSelected: allSelectableSelected,
            ratioPreviewRange: ratioPreviewRange,
            metricCards: metricCards,
            pct: pct,
            money: money,
            stageTone: stageTone,
            showRebuyFailureHistory: showRebuyFailureHistory,
            attemptTone: attemptTone,
            attemptPrice: attemptPrice,
            relatedLogsTo: relatedLogsTo,
            itemLabel: itemLabel,
            selectItem: selectItem,
            isSelected: isSelected,
            toggleOperation: toggleOperation,
            togglePageSelection: togglePageSelection,
            clearBatchSelection: clearBatchSelection,
            openBatch: openBatch,
            submitBatch: submitBatch,
            closeBatchResult: closeBatchResult,
            runtimeText: runtimeText,
            refresh: refresh,
            query: query,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
